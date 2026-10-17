--
-- schema.sql — komplet skema for datajobbet
--
-- GENDANNELSESFIL. Strukturen er taget ordret fra et pg_dump af den
-- kørende database, så en gendannelse giver en database der matcher
-- produktion. Den er IKKE en installationsfil: der er ingen
-- IF NOT EXISTS, så filen fejler højt hvis noget allerede findes.
-- Det er med vilje — en gendannelse skal ikke kunne køre halvt igennem
-- mod en beskidt database og lade som om alt gik godt.
--
-- HVAD DER ER MED:
--   11 tabeller, 2 views, 6 materialiserede views, 12 indekser, 14 constraints
--   + seed-data for de tre dimensionstabeller (kilder, stationer, mapping)
--
-- HVAD DER IKKE ER MED:
--   DATA. Alle tidsserier kan hentes igen fra kilden:
--     priser   ->  price_job.py
--     vejr     ->  weather_backfill.py --from-year 2016
--     fittet   ->  weather_harmonic_fit.py
--   Det er hele pointen med vandmærke-modellen: kilden er autoriteten,
--   databasen er et spejl med en position. Logtabellerne (sync_runs,
--   push_runs) og tilstandstabellerne (sync_state, pushed_state,
--   backfill_progress) får deres skema med, men ikke deres rækker —
--   de er historik, ikke konfiguration. Går pushed_state tabt, gensender
--   begge push-jobs bare alt næste gang.
--
-- GENDAN:
--   createdb Regenda
--   psql -d Regenda -f schema.sql
--   python3 weather_backfill.py --from-year 2016
--   python3 weather_harmonic_fit.py
--   python3 price_job.py
--
-- VERIFICÉR at filen er tro:
--   createdb Regenda_test && psql -d Regenda_test -f schema.sql
--   diff <(pg_dump --schema-only --no-owner Regenda_test) \
--        <(pg_dump --schema-only --no-owner Regenda)
--

SET statement_timeout = 0;
SET lock_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';
SET default_table_access_method = heap;

-- ===========================================================================
-- 1. ELPRISER
-- ===========================================================================

-- prices — én række pr. (område, time). Naturlig nøgle med ts_dk, ikke en
-- surrogat-id: (area, ts_dk) ER hvad en prisobservation er, og en
-- surrogatnøgle ville lade dubletter ind i stilhed.
CREATE TABLE public.prices (
    area text NOT NULL,
    ts_dk timestamp with time zone NOT NULL,
    dkk_per_kwh numeric(8,4) NOT NULL,
    eur_per_kwh numeric(8,4),
    fetched_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE public.prices OWNER TO postgres;

-- ===========================================================================
-- 2. KONTROL- OG LOGLAG (delt af pris- og vejrjobbet)
-- ===========================================================================

-- sync_runs — én række pr. henteforsøg. Append-only: rækken lukkes,
-- den skrives ikke om. job-kolonnen er den konvention der binder loggen
-- til datalaget — 'dayahead_prices' eller 'weather_dmi'.
CREATE TABLE public.sync_runs (
    run_id bigint NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    status text NOT NULL,
    rows_fetched integer DEFAULT 0 NOT NULL,
    rows_written integer DEFAULT 0 NOT NULL,
    window_from timestamp with time zone,
    window_to timestamp with time zone,
    error text,
    job text DEFAULT 'dayahead_prices'::text NOT NULL,
    http_status integer,
    http_message text,
    error_url text,
    attempts integer,
    elapsed_ms integer,
    error_body jsonb
);

ALTER TABLE public.sync_runs OWNER TO postgres;

-- sync_state — hvor langt hvert collect-job er kommet. Nøglet på job,
-- så de to jobs har hver sit vandmærke og ikke blander dem.
-- Vandmærket flytter sig KUN frem (GREATEST i set_watermark).
CREATE TABLE public.sync_state (
    job text NOT NULL,
    last_run_at timestamp with time zone NOT NULL,
    last_success_at timestamp with time zone,
    watermark_dk timestamp with time zone,
    rows_total bigint DEFAULT 0 NOT NULL
);

ALTER TABLE public.sync_state OWNER TO postgres;

-- push_runs — én række pr. upload-forsøg, fra BEGGE push-jobs.
-- days_planned/days_uploaded/days_skipped er navngivet efter prisjobbet,
-- men vejrjobbet bruger dem som antal filer.
CREATE TABLE public.push_runs (
    run_id bigint NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    status text NOT NULL,
    days_planned integer DEFAULT 0 NOT NULL,
    days_uploaded integer DEFAULT 0 NOT NULL,
    days_skipped integer DEFAULT 0 NOT NULL,
    bytes_uploaded bigint DEFAULT 0 NOT NULL,
    error text,
    job text DEFAULT 'push_prices'::text
);

ALTER TABLE public.push_runs OWNER TO postgres;

-- pushed_state — hvad der ligger ude på webserveren LIGE NU.
--
-- Delt af to jobs med hver sin nøgle: prisjobbet skriver DAGE (day),
-- vejrjobbet skriver FILTYPER (kind = 'normal' | 'season' | 'current').
-- En række har derfor enten day eller kind — aldrig begge.
--
-- content_hash er dedup-nøglen. Den beregnes UDEN tidsstempler, ellers
-- ville hashen være ny ved hver kørsel og 'uændret' kunne aldrig indtræffe.
CREATE TABLE public.pushed_state (
    day date,
    pushed_at timestamp with time zone DEFAULT now() NOT NULL,
    rows_pushed integer NOT NULL,
    content_hash text NOT NULL,
    kind text
);

ALTER TABLE public.pushed_state OWNER TO postgres;

-- ===========================================================================
-- 3. VEJR — dimensioner
-- ===========================================================================

-- weather_sources — hvilke kilder. DMI nu; DWD og SMARD forberedt.
CREATE TABLE public.weather_sources (
    source_id text NOT NULL,
    display_name text NOT NULL,
    kind text NOT NULL,
    base_url text,
    enabled boolean DEFAULT true NOT NULL
);

ALTER TABLE public.weather_sources OWNER TO postgres;

-- weather_locations — hvilke steder.
--
-- location_id ER DMI's stationstal. Det er en IDENTIFIKATOR, ikke et tal:
-- den ledende nul betyder kolonnen skal være TEXT. Som integer ville
-- '06031' blive 6031 og ikke matche DMI's svar.
--
-- Stationstallene er det gamle WMO blok+station-nummer (06 = WMO-område
-- Europa, 031 = stationen), så de er unikke på tværs af lande — DWD's
-- stationer ligger i samme nummerserie (fx 10382 for Berlin).
CREATE TABLE public.weather_locations (
    location_id text NOT NULL,
    wsi text,
    display_name text NOT NULL,
    country character(2) NOT NULL,
    lat numeric(8,5),
    lon numeric(8,5),
    tz text DEFAULT 'Europe/Copenhagen'::text NOT NULL
);

ALTER TABLE public.weather_locations OWNER TO postgres;

-- weather_source_locations — hvilken kilde kan levere hvilket sted.
-- external_id er kildens eget id; for DMI er det identisk med
-- location_id, men for DWD og SMARD vil de ikke matche.
-- Tabellen gør mappingen til en kendsgerning i databasen frem for en
-- Python-dict, der forsvinder med scriptet.
CREATE TABLE public.weather_source_locations (
    source_id text NOT NULL,
    location_id text NOT NULL,
    external_id text
);

ALTER TABLE public.weather_source_locations OWNER TO postgres;

-- ===========================================================================
-- 4. VEJR — rå data
-- ===========================================================================

-- weather — rå observationer og prognoser.
--
-- issued_at ER MED I NØGLEN med vilje. En prognose udstedt i går og en
-- udstedt i dag for SAMME ts_dk er to forskellige fakta. For observationer
-- sættes issued_at = ts_dk, så nøglen virker ens for begge slags og der
-- aldrig er NULL i en PRIMARY KEY.
--
-- DMI's granularitet er BLANDET: temp_mean_past1h og precip_past1h er
-- timeaggregater, mens vind, luftfugtighed og stråling kommer hvert 10.
-- minut. Derfor har kun hver sjette række en temperatur. Det er kildens
-- form, ikke en fejl — aggregeringer hører i views.
--
-- precip_mm: DMI's kode -0,1 betyder 'spor af nedbør, under 0,1 kg/m²'.
-- Det er IKKE en måling, og i et uge-SUM trækker hver -0,1 FRA.
CREATE TABLE public.weather (
    source_id text NOT NULL,
    location_id text NOT NULL,
    kind text NOT NULL,
    ts_dk timestamp with time zone NOT NULL,
    issued_at timestamp with time zone NOT NULL,
    source_run_id text,
    quality text,
    temperature_c numeric(5,2),
    wind_ms numeric(5,2),
    wind_dir_deg smallint,
    humidity_pct smallint,
    precip_mm numeric(6,2),
    cloud_pct smallint,
    pressure_hpa numeric(6,1),
    radiation_wm2 numeric(6,1),
    fetched_at timestamp with time zone DEFAULT now() NOT NULL,
    source_station_id text,
    CONSTRAINT weather_kind_check CHECK ((kind = ANY (ARRAY['forecast'::text, 'observation'::text])))
);

ALTER TABLE public.weather OWNER TO postgres;

-- backfill_progress — fremskridtsspor for weather_backfill.py.
-- Gør et flertimers backfill genoptageligt: scriptet springer de bidder
-- over der allerede står her. En afbrudt kørsel er bare at starte igen.
CREATE TABLE public.backfill_progress (
    source_id text NOT NULL,
    station_id text NOT NULL,
    parameter_id text NOT NULL,
    period_from date NOT NULL,
    period_to date NOT NULL,
    rows_written integer DEFAULT 0 NOT NULL,
    done_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE public.backfill_progress OWNER TO postgres;

-- ===========================================================================
-- 5. KVALITETSLAG
-- ===========================================================================

-- weather_clean — MARKERER, ændrer ikke. Værdien står urørt, så du altid
-- kan se hvad der blev flagget og hvorfor.
--
-- Hvorfor kun stråling? En generel kvalitetskontrol gennem alle elleve år
-- gav NUL fund på alle andre kolonner: humidity, vind og temperatur var
-- rene, og nattestråling fandtes ikke (klokkekurven var ren).
--
-- 1500 W/m² er en FYSISK grænse, ikke en statistisk: solar constant er
-- 1361 W/m², så ingen overflademåling på Jorden kan overstige den.
CREATE VIEW public.weather_clean AS
 SELECT w.source_id,
    w.location_id,
    w.kind,
    w.ts_dk,
    w.issued_at,
    w.source_run_id,
    w.quality,
    w.temperature_c,
    w.wind_ms,
    w.wind_dir_deg,
    w.humidity_pct,
    w.precip_mm,
    w.cloud_pct,
    w.pressure_hpa,
    w.radiation_wm2,
    w.fetched_at,
    w.source_station_id,
        CASE
            WHEN ((w.radiation_wm2 IS NOT NULL) AND (w.radiation_wm2 > (1500)::numeric)) THEN 'radiation_above_solar_constant'::text
            WHEN ((w.radiation_wm2 IS NOT NULL) AND (w.radiation_wm2 < (0)::numeric)) THEN 'radiation_negative'::text
            ELSE NULL::text
        END AS quality_flag
   FROM public.weather w;

ALTER VIEW public.weather_clean OWNER TO postgres;

-- weather_usable — samme data, men flaggede VÆRDIER nulstillet til NULL.
-- Nulstillingen er pr. KOLONNE, ikke pr. række: et spike i radia_glob
-- betyder ikke at vindmålingen samme minut er dårlig.
--
-- TOMMELFINGERREGEL: skriv til weather, læs fra weather_usable.
CREATE VIEW public.weather_usable AS
 SELECT weather_clean.source_id,
    weather_clean.location_id,
    weather_clean.kind,
    weather_clean.ts_dk,
    weather_clean.issued_at,
    weather_clean.source_station_id,
    weather_clean.quality,
    weather_clean.fetched_at,
    weather_clean.temperature_c,
    weather_clean.wind_ms,
    weather_clean.wind_dir_deg,
    weather_clean.humidity_pct,
    weather_clean.precip_mm,
    weather_clean.cloud_pct,
    weather_clean.pressure_hpa,
        CASE
            WHEN (weather_clean.quality_flag = ANY (ARRAY['radiation_above_solar_constant'::text, 'radiation_negative'::text])) THEN NULL::numeric
            ELSE weather_clean.radiation_wm2
        END AS radiation_wm2
   FROM public.weather_clean;

ALTER VIEW public.weather_usable OWNER TO postgres;

-- ===========================================================================
-- 6. AGGREGERINGER
-- ===========================================================================

-- weather_weekly — ugentligt aggregat pr. parameter PR. STATION.
--
-- Rækkefølgen er afgørende: uge-middel først (et lavpasfilter over ~4.500
-- målinger), DEREFTER min/max over årene. Havde min/max været taget
-- direkte fra 10-minuttersdata, ville båndet være sat af enkelte spikes.
--
-- 'agg' er med i nøglen fordi precip_mm er AKKUMULERENDE: et ugentligt
-- middel er 'mm pr. måling', hvilket ingen betydning har. Det rigtige er SUM.
--
-- Uge 53 slås sammen med uge 52 (LEAST): ISO-år har 52 eller 53 uger, og
-- et 3-års max for uge 53 ville være misvisende.
--
-- Nexø (06197) 2017 er udelukket — men KUN for radiation_wm2. De øvrige
-- parametre på stationen var rene, og at fjerne hele året ville bygge
-- 2017's danske tal på fem stationer uden Bornholm.
CREATE MATERIALIZED VIEW public.weather_weekly AS
 WITH src AS (
         SELECT weather_usable.location_id,
            weather_usable.ts_dk,
            unnest(ARRAY['radiation_wm2'::text, 'temperature_c'::text, 'wind_ms'::text, 'cloud_pct'::text, 'precip_mm'::text]) AS parameter,
            unnest(ARRAY[weather_usable.radiation_wm2, weather_usable.temperature_c, weather_usable.wind_ms, (weather_usable.cloud_pct)::numeric, weather_usable.precip_mm]) AS value,
            unnest(ARRAY['mean'::text, 'mean'::text, 'mean'::text, 'mean'::text, 'sum'::text]) AS agg
           FROM public.weather_usable
          WHERE ((weather_usable.source_id = 'dmi'::text) AND (weather_usable.kind = 'observation'::text) AND (NOT ((weather_usable.location_id = '06197'::text) AND (weather_usable.ts_dk >= '2017-01-01 00:00:00+01'::timestamp with time zone) AND (weather_usable.ts_dk < '2018-01-01 00:00:00+01'::timestamp with time zone))))
        ), weekly AS (
         SELECT src.location_id,
            src.parameter,
            src.agg,
            (EXTRACT(isoyear FROM (src.ts_dk AT TIME ZONE 'Europe/Copenhagen'::text)))::integer AS yr,
            LEAST((EXTRACT(week FROM (src.ts_dk AT TIME ZONE 'Europe/Copenhagen'::text)))::integer, 52) AS wk,
            avg(src.value) FILTER (WHERE (src.agg = 'mean'::text)) AS wk_mean,
            sum(src.value) FILTER (WHERE (src.agg = 'sum'::text)) AS wk_sum
           FROM src
          WHERE (src.value IS NOT NULL)
          GROUP BY src.location_id, src.parameter, src.agg, ((EXTRACT(isoyear FROM (src.ts_dk AT TIME ZONE 'Europe/Copenhagen'::text)))::integer), LEAST((EXTRACT(week FROM (src.ts_dk AT TIME ZONE 'Europe/Copenhagen'::text)))::integer, 52)
        ), resolved AS (
         SELECT weekly.location_id,
            weekly.parameter,
            weekly.agg,
            weekly.yr,
            weekly.wk,
            COALESCE(weekly.wk_mean, weekly.wk_sum) AS wk_value
           FROM weekly
        )
 SELECT resolved.location_id,
    resolved.parameter,
    resolved.agg,
    resolved.wk,
    count(*) AS n_years,
    min(resolved.wk_value) AS w_min,
    max(resolved.wk_value) AS w_max,
    percentile_cont((0.10)::double precision) WITHIN GROUP (ORDER BY ((resolved.wk_value)::double precision)) AS w_p10,
    percentile_cont((0.50)::double precision) WITHIN GROUP (ORDER BY ((resolved.wk_value)::double precision)) AS w_median,
    percentile_cont((0.90)::double precision) WITHIN GROUP (ORDER BY ((resolved.wk_value)::double precision)) AS w_p90
   FROM resolved
  GROUP BY resolved.location_id, resolved.parameter, resolved.agg, resolved.wk
  WITH NO DATA;

ALTER MATERIALIZED VIEW public.weather_weekly OWNER TO postgres;

-- weather_national — ugentligt aggregat for HELE LANDET.
--
-- Bygges fra weather_usable direkte, IKKE oven på weather_weekly: den
-- aggregerer over årene og smider årstallet væk. Landesaggregeringen skal
-- midle over stationer pr. uge PR. ÅR, altså bruge de ugentlige værdier
-- FØR årssammenfatningen.
--
-- RÆKKEFØLGEN: (1) uge-middel pr. station pr. år, (2) middel over stationer,
-- (3) min/max/median over årene. Vendte man om, ville hver stations
-- ekstremår påvirke midlet, og svaret ville være 'hvordan var den værste
-- station' — en anden påstand, i en graf der ser identisk ud.
--
-- min_stations eksponeres fordi grundlaget ikke er konstant: 5 for
-- stråling i 2017 (Nexø mangler), 6 for de øvrige.
--
-- cloud_pct er UDELADT: kun to af seks stationer rapporterer skydække,
-- begge i samme landsdel — et 'dansk' skydække ville være sjællandsk.
--
-- DETTE ER VIEWET PROJEKTET RAPPORTERER PÅ.
CREATE MATERIALIZED VIEW public.weather_national AS
 WITH src AS (
         SELECT u.location_id,
            u.ts_dk,
            u.parameter,
            u.value,
            u.agg
           FROM ( SELECT weather_usable.location_id,
                    weather_usable.ts_dk,
                    unnest(ARRAY['radiation_wm2'::text, 'temperature_c'::text, 'wind_ms'::text, 'precip_mm'::text]) AS parameter,
                    unnest(ARRAY[weather_usable.radiation_wm2, weather_usable.temperature_c, weather_usable.wind_ms, weather_usable.precip_mm]) AS value,
                    unnest(ARRAY['mean'::text, 'mean'::text, 'mean'::text, 'sum'::text]) AS agg
                   FROM public.weather_usable
                  WHERE ((weather_usable.source_id = 'dmi'::text) AND (weather_usable.kind = 'observation'::text))) u(location_id, ts_dk, parameter, value, agg)
          WHERE (NOT ((u.location_id = '06197'::text) AND (u.parameter = 'radiation_wm2'::text) AND (u.ts_dk >= '2017-01-01 00:00:00+01'::timestamp with time zone) AND (u.ts_dk < '2018-01-01 00:00:00+01'::timestamp with time zone)))
        ), station_week AS (
         SELECT src.location_id,
            src.parameter,
            src.agg,
            (EXTRACT(isoyear FROM (src.ts_dk AT TIME ZONE 'Europe/Copenhagen'::text)))::integer AS yr,
            LEAST((EXTRACT(week FROM (src.ts_dk AT TIME ZONE 'Europe/Copenhagen'::text)))::integer, 52) AS wk,
            avg(src.value) FILTER (WHERE (src.agg = 'mean'::text)) AS wk_mean,
            sum(src.value) FILTER (WHERE (src.agg = 'sum'::text)) AS wk_sum
           FROM src
          WHERE (src.value IS NOT NULL)
          GROUP BY src.location_id, src.parameter, src.agg, ((EXTRACT(isoyear FROM (src.ts_dk AT TIME ZONE 'Europe/Copenhagen'::text)))::integer), LEAST((EXTRACT(week FROM (src.ts_dk AT TIME ZONE 'Europe/Copenhagen'::text)))::integer, 52)
        ), resolved AS (
         SELECT station_week.location_id,
            station_week.parameter,
            station_week.agg,
            station_week.yr,
            station_week.wk,
            COALESCE(station_week.wk_mean, station_week.wk_sum) AS station_value
           FROM station_week
        ), national_week AS (
         SELECT resolved.parameter,
            resolved.agg,
            resolved.yr,
            resolved.wk,
            avg(resolved.station_value) AS nat_value,
            count(DISTINCT resolved.location_id) AS n_stations
           FROM resolved
          GROUP BY resolved.parameter, resolved.agg, resolved.yr, resolved.wk
        )
 SELECT national_week.parameter,
    national_week.agg,
    national_week.wk,
    count(*) AS n_years,
    min(national_week.nat_value) AS w_min,
    max(national_week.nat_value) AS w_max,
    percentile_cont((0.10)::double precision) WITHIN GROUP (ORDER BY ((national_week.nat_value)::double precision)) AS w_p10,
    percentile_cont((0.50)::double precision) WITHIN GROUP (ORDER BY ((national_week.nat_value)::double precision)) AS w_median,
    percentile_cont((0.90)::double precision) WITHIN GROUP (ORDER BY ((national_week.nat_value)::double precision)) AS w_p90,
    min(national_week.n_stations) AS min_stations,
    max(national_week.n_stations) AS max_stations
   FROM national_week
  GROUP BY national_week.parameter, national_week.agg, national_week.wk
  WITH NO DATA;

ALTER MATERIALIZED VIEW public.weather_national OWNER TO postgres;

-- weather_precip_weekly — nedbør pr. uge. SUM, ikke middel.
--
-- den akkumulerede timesværdi gentaget på 10-minuttersnettet.
CREATE MATERIALIZED VIEW public.weather_precip_weekly AS
 WITH hourly AS (
         SELECT weather_usable.location_id,
            date_trunc('hour'::text, (weather_usable.ts_dk AT TIME ZONE 'Europe/Copenhagen'::text)) AS hr_dk,
            max(weather_usable.precip_mm) AS mm_hour
           FROM public.weather_usable
          WHERE ((weather_usable.source_id = 'dmi'::text) AND (weather_usable.kind = 'observation'::text) AND (weather_usable.precip_mm IS NOT NULL) AND (NOT ((weather_usable.location_id = '06197'::text) AND (weather_usable.ts_dk >= '2017-01-01 00:00:00+01'::timestamp with time zone) AND (weather_usable.ts_dk < '2018-01-01 00:00:00+01'::timestamp with time zone))))
          GROUP BY weather_usable.location_id, (date_trunc('hour'::text, (weather_usable.ts_dk AT TIME ZONE 'Europe/Copenhagen'::text)))
        ), weekly AS (
         SELECT hourly.location_id,
            (EXTRACT(isoyear FROM hourly.hr_dk))::integer AS yr,
            LEAST((EXTRACT(week FROM hourly.hr_dk))::integer, 52) AS wk,
            sum(hourly.mm_hour) AS mm_week
           FROM hourly
          GROUP BY hourly.location_id, ((EXTRACT(isoyear FROM hourly.hr_dk))::integer), LEAST((EXTRACT(week FROM hourly.hr_dk))::integer, 52)
        )
 SELECT weekly.location_id,
    weekly.wk,
    count(*) AS n_years,
    min(weekly.mm_week) AS w_min,
    max(weekly.mm_week) AS w_max,
    percentile_cont((0.10)::double precision) WITHIN GROUP (ORDER BY ((weekly.mm_week)::double precision)) AS w_p10,
    percentile_cont((0.50)::double precision) WITHIN GROUP (ORDER BY ((weekly.mm_week)::double precision)) AS w_median,
    percentile_cont((0.90)::double precision) WITHIN GROUP (ORDER BY ((weekly.mm_week)::double precision)) AS w_p90
   FROM weekly
  GROUP BY weekly.location_id, weekly.wk
  WITH NO DATA;

ALTER MATERIALIZED VIEW public.weather_precip_weekly OWNER TO postgres;

-- weather_windrose — vindhistogram pr. retningssektor. Ikke en tidsserie.
--
-- TO FÆLDER:
-- 1. wind_dir_deg = 0 betyder STILLE, ikke nord. Uden filteret tælles
--    alle stille timer som nordenvind og oppuster nord-sektoren.
-- 2. Retningen er den vinden KOMMER FRA (meteorologisk konvention).
--    Vender du den forkert, peger hele rosen 180° galt uden at nogen
--    tal ser forkerte ud.
--
-- Sektoren forskydes 11,25° så den centreres om sin midte.
-- INGEN Nexø-udelukkelse her: kun strålingssensoren var i stykker.
CREATE MATERIALIZED VIEW public.weather_windrose AS
 WITH v AS (
         SELECT weather_usable.location_id,
            (EXTRACT(month FROM (weather_usable.ts_dk AT TIME ZONE 'Europe/Copenhagen'::text)))::integer AS month,
            (floor(((((weather_usable.wind_dir_deg)::numeric + 11.25) % (360)::numeric) / 22.5)))::integer AS sector,
            weather_usable.wind_ms
           FROM public.weather_usable
          WHERE ((weather_usable.source_id = 'dmi'::text) AND (weather_usable.kind = 'observation'::text) AND (weather_usable.wind_dir_deg IS NOT NULL) AND (weather_usable.wind_dir_deg <> 0) AND (weather_usable.wind_ms IS NOT NULL) AND (NOT ((weather_usable.location_id = '06197'::text) AND (weather_usable.ts_dk >= '2017-01-01 00:00:00+01'::timestamp with time zone) AND (weather_usable.ts_dk < '2018-01-01 00:00:00+01'::timestamp with time zone))))
        )
 SELECT v.location_id,
    v.month,
    v.sector,
    count(*) AS n,
    round(avg(v.wind_ms), 2) AS wind_avg,
    round(max(v.wind_ms), 2) AS wind_max
   FROM v
  GROUP BY v.location_id, v.month, v.sector
  WITH NO DATA;

ALTER MATERIALIZED VIEW public.weather_windrose OWNER TO postgres;

-- weather_daily — aggregat pr. døgn. issued_at med i GROUP BY, fordi et
-- døgnmiddel af prognosen udstedt i går er en anden størrelse end i dag.
CREATE MATERIALIZED VIEW public.weather_daily AS
 SELECT weather.source_id,
    weather.location_id,
    weather.kind,
    weather.issued_at,
    ((weather.ts_dk AT TIME ZONE 'Europe/Copenhagen'::text))::date AS day,
    count(*) AS n_obs,
    min(weather.temperature_c) AS t_min,
    max(weather.temperature_c) AS t_max,
    avg(weather.temperature_c) AS t_avg,
    avg(weather.wind_ms) AS wind_avg,
    max(weather.wind_ms) AS wind_max,
    sum(weather.precip_mm) AS precip_mm,
    avg(weather.cloud_pct) AS cloud_avg
   FROM public.weather
  GROUP BY weather.source_id, weather.location_id, weather.kind, weather.issued_at, (((weather.ts_dk AT TIME ZONE 'Europe/Copenhagen'::text))::date)
  WITH NO DATA;

ALTER MATERIALIZED VIEW public.weather_daily OWNER TO postgres;

-- weather_climatology — min/max/middel pr. måned over hele historikken.
-- Svarer på 'den koldeste februar nogensinde'. Ændrer sig kun ved
-- månedsskifte, så den skal næsten aldrig refreshes.
CREATE MATERIALIZED VIEW public.weather_climatology AS
 SELECT weather.location_id,
    (EXTRACT(month FROM (weather.ts_dk AT TIME ZONE 'Europe/Copenhagen'::text)))::integer AS month,
    count(*) AS n_obs,
    min(weather.temperature_c) AS t_min,
    max(weather.temperature_c) AS t_max,
    avg(weather.temperature_c) AS t_avg,
    avg(weather.wind_ms) AS wind_avg,
    sum(weather.precip_mm) AS precip_mm
   FROM public.weather
  WHERE (weather.kind = 'observation'::text)
  GROUP BY weather.location_id, ((EXTRACT(month FROM (weather.ts_dk AT TIME ZONE 'Europe/Copenhagen'::text)))::integer)
  WITH NO DATA;

ALTER MATERIALIZED VIEW public.weather_climatology OWNER TO postgres;

-- ===========================================================================
-- 7. SÆSONFIT
-- ===========================================================================

-- weather_season_fit — sæsonkurver fittet på weather_national.w_median.
-- Oprettes og vedligeholdes af weather_harmonic_fit.py:
--   python3 weather_harmonic_fit.py --selftest   (ingen DB, kør altid først)
--   python3 weather_harmonic_fit.py --dry-run    (vis R², skriv intet)
--
-- Modellen: c0 + a1·sin(2πt) + b1·cos(2πt) + a2·sin(4πt) + b2·cos(4πt),
-- t = (uge-1)/52, ω fastlåst til ét års cyklus.
--
-- KONSTANTLEDDET ER IKKE VALGFRIT. Uden c0 kan modellen kun udtrykke en
-- svingning omkring nul — og dansk temperatur ligger omkring 8 °C. Uden
-- det gav et kendt syntetisk facit R² = -0.58, og på rigtige data fejlede
-- det mest for parametre med lille variation relativt til deres niveau
-- (vind: R² = -36.9).
--
-- To harmoniske fordi dansk sol og temperatur har en ægte asymmetri:
-- foråret stiger hurtigere end efteråret falder.
--
-- Målte R² efter rettelsen:
--   temperature_c  0.984  |  radiation_wm2  0.989
--   wind_ms        0.681  |  precip_mm      0.442
--
-- Kravet om periodicitet: f(0)=f(1) og f'(0)=f'(1), fordi alle led har
-- heltallig frekvens. Kurven kan derfor tegnes fra et vilkårligt sted i
-- et år til et vilkårligt sted i det næste uden hak ved uge 52/1.
CREATE TABLE public.weather_season_fit (
    location_id text NOT NULL,
    parameter text NOT NULL,
    n_harmonics smallint NOT NULL,
    r2 numeric(6,4) NOT NULL,
    coef jsonb NOT NULL,
    curve jsonb NOT NULL,
    fitted_at timestamp with time zone DEFAULT now() NOT NULL,
    n_coef smallint DEFAULT 5 NOT NULL
);

ALTER TABLE public.weather_season_fit OWNER TO postgres;

-- ===========================================================================
-- 8. SEKVENSR, NØGLER OG INDEKSER
-- ===========================================================================

CREATE SEQUENCE public.sync_runs_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.sync_runs_run_id_seq OWNER TO postgres;
ALTER SEQUENCE public.sync_runs_run_id_seq OWNED BY public.sync_runs.run_id;

CREATE SEQUENCE public.push_runs_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.push_runs_run_id_seq OWNER TO postgres;
ALTER SEQUENCE public.push_runs_run_id_seq OWNED BY public.push_runs.run_id;

ALTER TABLE ONLY public.sync_runs ALTER COLUMN run_id SET DEFAULT nextval('public.sync_runs_run_id_seq'::regclass);
ALTER TABLE ONLY public.push_runs ALTER COLUMN run_id SET DEFAULT nextval('public.push_runs_run_id_seq'::regclass);

-- Primary keys
ALTER TABLE ONLY public.backfill_progress
    ADD CONSTRAINT backfill_progress_pkey PRIMARY KEY (source_id, station_id, parameter_id, period_from, period_to);
ALTER TABLE ONLY public.prices
    ADD CONSTRAINT prices_pkey PRIMARY KEY (area, ts_dk);
ALTER TABLE ONLY public.push_runs
    ADD CONSTRAINT push_runs_pkey PRIMARY KEY (run_id);
ALTER TABLE ONLY public.sync_runs
    ADD CONSTRAINT sync_runs_pkey PRIMARY KEY (run_id);
ALTER TABLE ONLY public.sync_state
    ADD CONSTRAINT sync_state_pkey PRIMARY KEY (job);
ALTER TABLE ONLY public.weather_locations
    ADD CONSTRAINT weather_locations_pkey PRIMARY KEY (location_id);
ALTER TABLE ONLY public.weather
    ADD CONSTRAINT weather_pkey PRIMARY KEY (source_id, location_id, kind, ts_dk, issued_at);
ALTER TABLE ONLY public.weather_season_fit
    ADD CONSTRAINT weather_season_fit_pkey PRIMARY KEY (location_id, parameter, n_harmonics);
ALTER TABLE ONLY public.weather_source_locations
    ADD CONSTRAINT weather_source_locations_pkey PRIMARY KEY (source_id, location_id);
ALTER TABLE ONLY public.weather_sources
    ADD CONSTRAINT weather_sources_pkey PRIMARY KEY (source_id);
--
-- Fremmednøgler. Kun weather-dimensionerne har dem: de beskriver HVAD DER
-- FINDES og ændrer sig sjældent, så en FK koster intet og fanger tastefejl.
-- Logtabellerne (sync_runs, push_runs) har ingen — de beskriver hvad der
-- SKETE og skrives konstant.
--
-- To på weather, to på weather_source_locations. Tabellen SKAL stå eksplicit
-- på hver linje — en hardkodet ALTER TABLE ville lægge constrainten på den
-- forkerte tabel, hvilket er præcis hvad en diff mod produktion fanger.
ALTER TABLE ONLY public.weather
    ADD CONSTRAINT weather_location_id_fkey FOREIGN KEY (location_id)
    REFERENCES public.weather_locations(location_id);

ALTER TABLE ONLY public.weather
    ADD CONSTRAINT weather_source_id_fkey FOREIGN KEY (source_id)
    REFERENCES public.weather_sources(source_id);

ALTER TABLE ONLY public.weather_source_locations
    ADD CONSTRAINT weather_source_locations_location_id_fkey FOREIGN KEY (location_id)
    REFERENCES public.weather_locations(location_id);

ALTER TABLE ONLY public.weather_source_locations
    ADD CONSTRAINT weather_source_locations_source_id_fkey FOREIGN KEY (source_id)
    REFERENCES public.weather_sources(source_id);
ALTER TABLE ONLY public.weather_source_locations
    ADD CONSTRAINT weather_source_locations_location_id_fkey FOREIGN KEY (location_id) REFERENCES public.weather_locations(location_id);
ALTER TABLE ONLY public.weather_source_locations
    ADD CONSTRAINT weather_source_locations_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.weather_sources(source_id);

-- Indekser
CREATE INDEX push_runs_job_idx ON public.push_runs USING btree (job, run_id DESC);
CREATE UNIQUE INDEX pushed_state_day_key ON public.pushed_state USING btree (day) WHERE (day IS NOT NULL);
CREATE UNIQUE INDEX pushed_state_kind_key ON public.pushed_state USING btree (kind) WHERE (kind IS NOT NULL);
CREATE UNIQUE INDEX weather_climatology_key ON public.weather_climatology USING btree (location_id, month);
CREATE UNIQUE INDEX weather_daily_key ON public.weather_daily USING btree (source_id, location_id, kind, issued_at, day);
CREATE INDEX weather_forecast_idx ON public.weather USING btree (location_id, ts_dk, issued_at DESC) WHERE (kind = 'forecast'::text);
CREATE INDEX weather_forecast_window_idx ON public.weather USING btree (location_id, ts_dk, issued_at DESC) WHERE (kind = 'forecast'::text);
CREATE UNIQUE INDEX weather_national_key ON public.weather_national USING btree (parameter, agg, wk);
CREATE INDEX weather_obs_idx ON public.weather USING btree (location_id, ts_dk DESC) WHERE (kind = 'observation'::text);
CREATE UNIQUE INDEX weather_precip_weekly_key ON public.weather_precip_weekly USING btree (location_id, wk);
CREATE UNIQUE INDEX weather_weekly_key ON public.weather_weekly USING btree (location_id, parameter, agg, wk);
CREATE UNIQUE INDEX weather_windrose_key ON public.weather_windrose USING btree (location_id, month, sector);

-- BEMÆRK: weather_forecast_idx og weather_forecast_window_idx er
-- IDENTISKE — samme kolonner, samme prædikat. Det er en ægte dublet fra
-- en omdøbning. Den ene kan droppes:
--   DROP INDEX IF EXISTS public.weather_forecast_window_idx;

-- De to indekser på weather er DELVISE. Prognoser og observationer er to
-- datasæt i én tabel med vidt forskellige forespørgselsmønstre; et fælles
-- indeks ville indeholde begge blandet og filtrere kind bagefter.

-- ===========================================================================
-- 9. SEED-DATA
-- ===========================================================================

-- Dimensionstabellerne er KONFIGURATION, ikke data: hvilke kilder der
-- findes, hvilke stationer vi henter, og hvordan de mapper. Uden dem kan
-- weather ikke tage imod data — der er fremmednøgler til dem.

INSERT INTO public.weather_sources (source_id, display_name, kind, base_url)
VALUES
    ('dmi',  'Danmarks Meteorologiske Institut', 'observation',
     'https://opendataapi.dmi.dk/v2/metObs/collections/observation/items'),
    ('dwd',  'Deutscher Wetterdienst',           'forecast', NULL),
    ('smard', 'SMARD / Bundesnetzagentur',       'market',   NULL)
ON CONFLICT (source_id) DO NOTHING;

INSERT INTO public.weather_locations (location_id, display_name, country, tz)
VALUES
    ('06031', 'Tylstrup',  'DK', 'Europe/Copenhagen'),
    ('06065', 'Års Syd',   'DK', 'Europe/Copenhagen'),
    ('06109', 'Askov',     'DK', 'Europe/Copenhagen'),
    ('06141', 'Abed',      'DK', 'Europe/Copenhagen'),
    ('06188', 'Sjælsmark', 'DK', 'Europe/Copenhagen'),
    ('06197', 'Nexø Vest', 'DK', 'Europe/Copenhagen')
ON CONFLICT (location_id) DO NOTHING;

INSERT INTO public.weather_source_locations (source_id, location_id, external_id)
VALUES
    ('dmi', '06031', '06031'),
    ('dmi', '06065', '06065'),
    ('dmi', '06109', '06109'),
    ('dmi', '06141', '06141'),
    ('dmi', '06188', '06188'),
    ('dmi', '06197', '06197')
ON CONFLICT (source_id, location_id) DO NOTHING;

-- ===========================================================================
-- 10. DRIFTSNOTER
-- ===========================================================================

-- Efter hver collect-kørsel:
--   REFRESH MATERIALIZED VIEW CONCURRENTLY public.weather_daily;
--   REFRESH MATERIALIZED VIEW CONCURRENTLY public.weather_weekly;
--   REFRESH MATERIALIZED VIEW CONCURRENTLY public.weather_national;
--   REFRESH MATERIALIZED VIEW CONCURRENTLY public.weather_precip_weekly;
--   REFRESH MATERIALIZED VIEW CONCURRENTLY public.weather_windrose;
--
-- Climatology ændrer sig kun ved månedsskifte:
--   REFRESH MATERIALIZED VIEW CONCURRENTLY public.weather_climatology;

-- SUNDHEDSTJEK — stråling skal følge en KLOKKEEKURVE:
--   SELECT extract(hour FROM ts_dk)::int AS time_utc, count(*),
--          round(avg(radiation_wm2)::numeric,1), round(max(radiation_wm2)::numeric,1)
--     FROM public.weather WHERE source_id='dmi' AND radiation_wm2 IS NOT NULL
--    GROUP BY time_utc ORDER BY time_utc;
--
--   extract(hour FROM ts_dk) giver timen i SESSIONENS tidszone. Kør
--   'SHOW timezone;' først — er sessionen ikke i UTC, kan 'time 22-04'
--   ramme midt på dansk dagen og give falske fund.
--
-- KENDTE DATAISSUES:
--   Nexø (06197) 2017: strålingssensoren i stykker hele sommeren. 705
--   rækker over 1500 W/m², alle om natten, op til 4088. Udelades HELT for
--   denne station ved aggregering (se weather_weekly/weather_national).
--   Sjælsmark (06188) 2018-19: 3 spikes. Fanget af grænsen.
--   Alle øvrige kolonner er rene gennem 2016-2026.

-- LÆKKEDE KØRSLER (jobbet dræbt før finish_run):
--   SELECT run_id, job, started_at FROM public.sync_runs
--    WHERE status='running' AND started_at < now() - interval '1 hour';

--
-- schema.sql — slut
--
