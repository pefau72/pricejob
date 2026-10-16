--
-- Weather schema — weather-delen af pricejob-databasen
--
-- Matcher mønstret fra prices: naturlig nøgle med ts_dk, fetched_at,
-- idempotent skrivning, ingen run-logik i datatabellen.
--
-- Kør:  psql -h localhost -U postgres -d Regenda -f weather-schema.sql
--
-- Filer der hører sammen med denne:
--   weather.py            dagligt job — henter efterslæb + nu
--   weather-backfill.py   engangsjob — henter historik måned for måned
--
-- Begge filer forudsætter PRÆCIS dette skema. Ændrer du her, ret dem også.
--

-- ---------------------------------------------------------------------------
-- weather_sources — hvilke kilder findes der
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.weather_sources (
    source_id    text PRIMARY KEY,           -- 'dmi', 'dwd', 'smard'
    display_name text NOT NULL,
    kind         text NOT NULL,              -- 'forecast' | 'observation' | 'market'
    base_url     text,
    enabled      boolean NOT NULL DEFAULT true
);

ALTER TABLE public.weather_sources OWNER TO postgres;

INSERT INTO public.weather_sources (source_id, display_name, kind, base_url)
VALUES
    ('dmi',  'Danmarks Meteorologiske Institut', 'observation',
     'https://opendataapi.dmi.dk/v2/metObs/collections/observation/items'),
    ('dwd',  'Deutscher Wetterdienst',           'forecast', NULL),
    ('smard', 'SMARD / Bundesnetzagentur',       'market',   NULL)
ON CONFLICT (source_id) DO NOTHING;


-- ---------------------------------------------------------------------------
-- weather_locations — hvilke steder
--
-- location_id ER DMI's stationstal for DMI-stationer. Det er en identifikator,
-- ikke et tal: den ledende nul betyder, at kolonnen skal være text. Som integer
-- ville '06031' blive 6031 og ikke matche DMI's svar.
--
-- DMI's stationstal er det gamle WMO blok+station-nummer (06 = WMO-område
-- Europa, 031 = stationen), så det er unikt på tværs af lande. DWD's stationer
-- ligger i samme nummerserie (fx 10382 for Berlin).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.weather_locations (
    location_id  text PRIMARY KEY,           -- '06031', 'dk-tylstrup' ...
    display_name text NOT NULL,              -- 'Tylstrup'
    country      char(2) NOT NULL,           -- ISO-3166 alpha-2
    lat          numeric(8,5),
    lon          numeric(8,5),
    tz           text NOT NULL DEFAULT 'Europe/Copenhagen'
);

ALTER TABLE public.weather_locations OWNER TO postgres;

INSERT INTO public.weather_locations (location_id, display_name, country, tz)
VALUES
    ('06031', 'Tylstrup',  'DK', 'Europe/Copenhagen'),
    ('06065', 'Års Syd',   'DK', 'Europe/Copenhagen'),
    ('06109', 'Askov',     'DK', 'Europe/Copenhagen'),
    ('06141', 'Abed',      'DK', 'Europe/Copenhagen'),
    ('06188', 'Sjælsmark', 'DK', 'Europe/Copenhagen'),
    ('06197', 'Nexø Vest', 'DK', 'Europe/Copenhagen')
ON CONFLICT (location_id) DO NOTHING;


-- ---------------------------------------------------------------------------
-- weather_source_locations — hvilken kilde kan levere hvilket sted
--
-- external_id er kildens eget id for stedet. For DMI er det identisk med
-- location_id, men tabellen har sin rolle for kilder hvor de ikke matcher
-- (fx DWD eller SMARD), og den gør mappingen til en kendsgerning i databasen
-- i stedet for en Python-dict, der forsvinder med scriptet.
--
-- weather.py og weather-backfill.py kalder ensure_source_locations(), som
-- seeder begge tabeller ved kørsel. Denne fil er sandheden; koden holder den.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.weather_source_locations (
    source_id   text NOT NULL REFERENCES public.weather_sources(source_id),
    location_id text NOT NULL REFERENCES public.weather_locations(location_id),
    external_id text,
    PRIMARY KEY (source_id, location_id)
);

ALTER TABLE public.weather_source_locations OWNER TO postgres;

INSERT INTO public.weather_source_locations (source_id, location_id, external_id)
VALUES
    ('dmi', '06031', '06031'),
    ('dmi', '06065', '06065'),
    ('dmi', '06109', '06109'),
    ('dmi', '06141', '06141'),
    ('dmi', '06188', '06188'),
    ('dmi', '06197', '06197')
ON CONFLICT (source_id, location_id) DO NOTHING;


-- ---------------------------------------------------------------------------
-- weather — rå observationer/prognoser
--
-- Nøglen indeholder issued_at med vilje. En prognose udstedt i går og en
-- udstedt i dag for SAMME ts_dk er to forskellige fakta. For observationer
-- sættes issued_at = ts_dk, så nøglen virker ens for begge slags og der
-- aldrig er NULL i en PRIMARY KEY.
--
-- DMI's granularitet er blandet: temp_mean_past1h og precip_past1h er
-- timeaggregater, mens vind, luftfugtighed og stråling kommer hvert 10. minut.
-- Derfor har kun hver sjette række en temperatur. Det er kildens form, ikke
-- en fejl. Aggregeringer hører i views (weather_daily, weather_climatology).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.weather (
    source_id         text NOT NULL REFERENCES public.weather_sources(source_id),
    location_id       text NOT NULL REFERENCES public.weather_locations(location_id),
    kind              text NOT NULL,             -- 'forecast' | 'observation'
    ts_dk             timestamp with time zone NOT NULL,   -- gælder for
    issued_at         timestamp with time zone NOT NULL,   -- udstedt/observeret

    -- Proveniens: hvilken DMI-station rækken kom fra.
    source_station_id text,

    -- Kvalitet — til prognoser og interpolerede værdier senere.
    quality           text,                      -- 'ok' | 'estimated' | 'interpolated'

    temperature_c     numeric(5,2),              -- DMI temp_mean_past1h
    wind_ms           numeric(5,2),              -- DMI wind_speed
    wind_dir_deg      smallint,                  -- DMI wind_dir
    humidity_pct      smallint,                  -- DMI humidity
    precip_mm         numeric(6,2),              -- DMI precip_past1h
    cloud_pct         smallint,                  -- DMI cloud_cover
    pressure_hpa      numeric(6,1),
    radiation_wm2     numeric(7,2),              -- DMI radia_glob

    fetched_at        timestamp with time zone DEFAULT now() NOT NULL,

    CONSTRAINT weather_pkey
        PRIMARY KEY (source_id, location_id, kind, ts_dk, issued_at),

    CONSTRAINT weather_kind_check
        CHECK (kind IN ('forecast', 'observation'))
);

ALTER TABLE public.weather OWNER TO postgres;


-- ---------------------------------------------------------------------------
-- Indekser — delvise, fordi prognoser og observationer er to datasæt i én
-- tabel med vidt forskellige forespørgselsmønstre og størrelser. Et fælles
-- indeks ville indeholde begge slags blandet og skulle filtrere kind bagefter.
-- ---------------------------------------------------------------------------

-- Observationer: den varme vej. "Vis vejr for dette sted, denne periode."
CREATE INDEX IF NOT EXISTS weather_obs_idx
    ON public.weather (location_id, ts_dk DESC)
    WHERE kind = 'observation';

-- Prognoser: min/max over de seneste N kørsler.
-- row_number() OVER (PARTITION BY ..., ts_dk ORDER BY issued_at DESC)
CREATE INDEX IF NOT EXISTS weather_forecast_idx
    ON public.weather (location_id, ts_dk, issued_at DESC)
    WHERE kind = 'forecast';


-- ---------------------------------------------------------------------------
-- backfill_progress — fremskridtsspor for weather-backfill.py
--
-- Gør et flertimers backfill genoptageligt: scriptet springer de bidder over,
-- der allerede står her. En afbrudt kørsel er bare at starte igen.
--
-- Vil du hente alt forfra, ryd sporet:
--   DELETE FROM public.backfill_progress WHERE source_id = 'dmi';
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.backfill_progress (
    source_id     text NOT NULL,
    station_id    text NOT NULL,
    parameter_id  text NOT NULL,
    period_from   date NOT NULL,
    period_to     date NOT NULL,
    rows_written  int NOT NULL DEFAULT 0,
    done_at       timestamp with time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (source_id, station_id, parameter_id, period_from, period_to)
);

ALTER TABLE public.backfill_progress OWNER TO postgres;


-- ---------------------------------------------------------------------------
-- weather_daily — aggregat pr. døgn. Materialiseret view, ikke en tabel.
--
-- Aggregeringen sker HER, ikke ved indlæsning. Rå værdier bevares i weather,
-- så viewet altid kan genberegnes — og så et nyt spørgsmål ("frostnætter i
-- december") kan besvares uden at hente DMI igen.
--
-- issued_at er med i GROUP BY: et døgnmiddel af prognosen udstedt i går er
-- en anden størrelse end af den udstedt i dag.
-- ---------------------------------------------------------------------------

DROP MATERIALIZED VIEW IF EXISTS public.weather_daily;

CREATE MATERIALIZED VIEW public.weather_daily AS
SELECT source_id,
       location_id,
       kind,
       issued_at,
       (ts_dk AT TIME ZONE 'Europe/Copenhagen')::date AS day,
       count(*)              AS n_obs,
       min(temperature_c)    AS t_min,
       max(temperature_c)    AS t_max,
       avg(temperature_c)    AS t_avg,
       avg(wind_ms)          AS wind_avg,
       max(wind_ms)          AS wind_max,
       sum(precip_mm)        AS precip_mm,
       avg(cloud_pct)        AS cloud_avg
  FROM public.weather
 GROUP BY source_id, location_id, kind, issued_at,
          (ts_dk AT TIME ZONE 'Europe/Copenhagen')::date;

ALTER MATERIALIZED VIEW public.weather_daily OWNER TO postgres;

-- Unikt indeks er påkrævet for REFRESH ... CONCURRENTLY.
CREATE UNIQUE INDEX IF NOT EXISTS weather_daily_key
    ON public.weather_daily (source_id, location_id, kind, issued_at, day);


-- ---------------------------------------------------------------------------
-- weather_climatology — min/max/middel pr. måned over hele historikken.
--
-- Det er dette view, der svarer på "den koldeste februar nogensinde" og
-- giver referencebåndet på grafer. 10 års data ligger bag tallene.
--
-- Ændrer sig kun når en måned er færdig — næsten aldrig nødvendigt at refreshe.
-- ---------------------------------------------------------------------------

DROP MATERIALIZED VIEW IF EXISTS public.weather_climatology;

CREATE MATERIALIZED VIEW public.weather_climatology AS
SELECT location_id,
       extract(month FROM ts_dk AT TIME ZONE 'Europe/Copenhagen')::int AS month,
       count(*)           AS n_obs,
       min(temperature_c) AS t_min,
       max(temperature_c) AS t_max,
       avg(temperature_c) AS t_avg,
       avg(wind_ms)       AS wind_avg,
       sum(precip_mm)     AS precip_mm
  FROM public.weather
 WHERE kind = 'observation'
 GROUP BY location_id,
          extract(month FROM ts_dk AT TIME ZONE 'Europe/Copenhagen')::int;

ALTER MATERIALIZED VIEW public.weather_climatology OWNER TO postgres;

CREATE UNIQUE INDEX IF NOT EXISTS weather_climatology_key
    ON public.weather_climatology (location_id, month);


-- ---------------------------------------------------------------------------
-- Efter hver collect-kørsel:
--   REFRESH MATERIALIZED VIEW CONCURRENTLY public.weather_daily;
--
-- Climatology ændrer sig kun ved månedsskifte:
--   REFRESH MATERIALIZED VIEW CONCURRENTLY public.weather_climatology;
--
-- Verificér dækning (kør efter backfill):
--   SELECT location_id, count(*), min(ts_dk)::date, max(ts_dk)::date
--     FROM weather WHERE source_id = 'dmi'
--    GROUP BY location_id ORDER BY location_id;
--
-- Find lækkede kørsler (jobbet blev dræbt før finish_run):
--   SELECT run_id, started_at FROM sync_runs
--    WHERE status = 'running' AND started_at < now() - interval '1 hour';
-- ---------------------------------------------------------------------------

--
-- PostgreSQL database dump complete
--
