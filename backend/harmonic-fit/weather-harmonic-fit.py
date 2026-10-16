#!/usr/bin/env python3
"""weather-harmonic-fit.py — sæsonfit for vejr-parametre, på LANDENIVEAU.

Fitter to harmoniske mod MEDIANEN af de ugentlige værdier i weather_national.
Skriver resultatet til weather_season_fit.

    f(t) = c0
         + a1·sin(2πt) + b1·cos(2πt)
         + a2·sin(4πt) + b2·cos(4πt)

    hvor t = (uge - 1) / 52,  og uge ∈ 1..52

DER MANGLEDE ET KONSTANTLED — den fejl filen er rettet for:
    Den første version havde ingen c0, med begrundelsen at "niveauet ligger
    i cos(0)-leddet". Det er forkert: cos(2πt) er ikke konstant, den
    svinger. Uden c0 kan modellen kun udtrykke signaler omkring NUL, ikke
    omkring fx 8 °C. Et kendt syntetisk facit (8 - 9·cos(2πt)) gav R² =
    -0.5802 med fire koefficienter og ~1.0 med fem. På rigtige data
    fejlede det mest for parametre med lille variation relativt til deres
    niveau — vind (middel ~5 m/s, variation ~2) fik R² = -36.9.

    Læren: havde den syntetiske test været skrevet FØRST, var fejlen
    fanget før funktionen nogensinde så en database. Derfor ligger
    --selftest nu i filen.

PERIODICITET — kravet om glat kurve hen over uge 52/1:
    Alle led har heltallig frekvens, så for ethvert helt k:

        sin(2πk·(t+k)) = sin(2πk·t),   cos(2πk·(t+k)) = cos(2πk·t)

    Og konstantleddet er periodisk med enhver periode. Derfor er BÅDE
    værdi og differentialkvotient identiske i t = 0 og t = 1:

        f(0) = f(1),   f'(0) = f'(1)

    Kurven kan derfor tegnes fra et vilkårligt sted i et år til et
    vilkårligt sted i det næste uden hak — man indsætter bare t uden for
    [0,1), fx uge 45..67, og trigonometrien gentager sig selv.

    Det er derfor en Fourier-basis bruges og ikke et polynomium: et
    polynomium ville give et hak ved 52/1, og hakket ville blive STØRRE
    jo højere grad man valgte.

HVORFOR MEDIANEN OG IKKE BÅNDET:
    Med elleve år er både w_min og w_max sat af ét enkelt års vejr — de
    er rekordholderfunktioner, ikke signaler. En sinus fittet til dem
    beskriver "hvornår rekorder blev sat". Medianen er et ægte
    sæson-signal. Fit IKKE mod båndet.

Kør:
    python3 weather-harmonic-fit.py --selftest    # ingen DB. Kør altid først.
    python3 weather-harmonic-fit.py --dry-run     # vis R², skriv intet
    python3 weather-harmonic-fit.py               # skriv til weather_season_fit
    python3 weather-harmonic-fit.py --parameter wind_ms
"""

from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import psycopg
from dotenv import load_dotenv

load_dotenv("/home/peterfausboll/code/pricejob/backend/.env")

CPH = ZoneInfo("Europe/Copenhagen")

DSN = psycopg.conninfo.make_conninfo(
    host=os.environ["PGHOST"],
    port=os.environ.get("PGPORT", "41326"),
    dbname=os.environ.get("PGDATABASE", "Regenda"),
    user=os.environ["PGUSER"],
    password=os.environ["PGPASSWORD"],
)

N_HARMONICS = 2

# Parametre på landeniveau, fra weather_national.
#
# cloud_pct er UDELADT: kun to af seks stationer rapporterer skydække
# (06141 Abed, 06188 Sjælsmark), begge i samme landsdel — et "dansk"
# skydække bygget på dem ville være et sjællandsk skydække.
#
# precip_mm er UDELADT: nedbør i Danmark er bygedrevet og har ikke en
# sæson, der ligner de andres. Kør den gerne alligevel — R² forventes
# under 0.15, og det er svaret, ikke en fejl.
FITTABLE = ("temperature_c", "radiation_wm2", "wind_ms")

NATIONAL = "DK"   # landet er én serie


def log(msg: str) -> None:
    print(f"{datetime.now(CPH):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# =========================================================== model og fit

def design_matrix(weeks: np.ndarray, n_harmonics: int = N_HARMONICS) -> np.ndarray:
    """Bygger [1, sin(ωt), cos(ωt), sin(2ωt), cos(2ωt), ...] for hver uge.

    FØRSTE KOLONNE ER ET KONSTANTLED (ettaller). Den er ikke valgfri:
    uden den kan modellen ikke udtrykke et niveau, kun en svingning omkring
    nul — og dansk temperatur ligger omkring 8 °C, ikke omkring 0. Se
    docstringen i toppen af filen.

    t = (uge - 1) / 52, så uge 1 -> t = 0 og uge 52 -> t = 51/52.
    """
    t = (weeks.astype(float) - 1.0) / 52.0
    cols = [np.ones_like(t)]                     # c0 — niveauet
    for h in range(1, n_harmonics + 1):
        cols.append(np.sin(2 * math.pi * h * t))
        cols.append(np.cos(2 * math.pi * h * t))
    return np.column_stack(cols)


def n_coefficients(n_harmonics: int = N_HARMONICS) -> int:
    """1 konstantled + 2 pr. harmonisk."""
    return 1 + 2 * n_harmonics


def eval_fit(coef: np.ndarray, weeks: np.ndarray,
             n_harmonics: int = N_HARMONICS) -> np.ndarray:
    """Beregner kurven for vilkårlige uger — også uden for 1..52.

    Til at tegne hen over årsskiftet: send uge 45..67 og få en glat kurve,
    fordi alle led er periodiske.
    """
    t = (np.asarray(weeks, dtype=float) - 1.0) / 52.0
    y = np.full_like(t, coef[0])
    i = 1
    for h in range(1, n_harmonics + 1):
        y = y + coef[i] * np.sin(2 * math.pi * h * t)
        y = y + coef[i + 1] * np.cos(2 * math.pi * h * t)
        i += 2
    return y


def fit_series(weeks: np.ndarray, values: np.ndarray,
               n_harmonics: int = N_HARMONICS):
    """Mindste kvadraters fit. Returnerer (koefficienter, r2, fitted).

    weeks SKAL ligge i 1..52. Et wk = 53 (ISO-år med 53 uger) ville
    forskyde fasen med 1/52 og give netop det hak ved 52/1 som
    periodiciteten ellers forhindrer. Viewet slår uge 53 sammen med 52;
    dette er en vagt, hvis det alligevel sker.
    """
    weeks = np.asarray(weeks, dtype=float)
    values = np.asarray(values, dtype=float)

    wmin, wmax = float(np.min(weeks)), float(np.max(weeks))
    if wmin < 1 or wmax > 52:
        raise ValueError(
            f"uger skal ligge i 1..52, fik {wmin:.0f}..{wmax:.0f}. "
            f"Uge 53 skal slås sammen med 52 i viewet (se weather_weekly)."
        )

    X = design_matrix(weeks, n_harmonics)
    coef, *_ = np.linalg.lstsq(X, values, rcond=None)
    fitted = X @ coef

    ss_res = float(np.sum((values - fitted) ** 2))
    ss_tot = float(np.sum((values - values.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return coef, r2, fitted


def verdict_for(r2: float) -> str:
    """R² er ikke et kvalitetsstempel men et svar på hvor meget af
    variationen årstiden forklarer."""
    if r2 >= 0.80:
        return "stærk sæson"
    if r2 >= 0.50:
        return "tydelig sæson"
    if r2 >= 0.25:
        return "svag sæson"
    return "ingen sæson — fit er fladt"


# ================================================================ selftest

def selftest() -> int:
    """Afprøver fit_series mod kurver med KENDT facit. Ingen database.

    Dette er den test, der ville have fanget det manglende konstantled med
    det samme. Kør den altid efter en ændring i design_matrix/fit_series.
    """
    weeks = np.arange(1, 53, dtype=float)
    t = (weeks - 1.0) / 52.0
    ok = True

    # R² er IKKE defineret for en konstant kurve: ss_tot = 0, og spørgsmålet
    # "hvor stor en andel af variansen forklares" giver ikke mening. En
    # tidligere udgave havde "kun niveau, 4.2" som case med forventning
    # 0.999 — den fejlede med R² = 0.0, og det var TESTEN der var forkert,
    # ikke fittet. En selvtest der fejler på noget der ikke er en fejl er
    # værre end ingen selvtest: den lærer én at ignorere resultatet.
    #
    # "kun niveau" testes derfor på koefficienterne i stedet — se nedenfor.
    cases = [
        # (navn, facit, forventet mindste R²)
        ("dansk temperatur  8-9cos", 8 - 9 * np.cos(2 * math.pi * t), 0.999),
        ("to harmoniske", 10 + 3 * np.sin(2 * math.pi * t)
             - 7 * np.cos(2 * math.pi * t)
             + 1.5 * np.sin(4 * math.pi * t)
             + 0.8 * np.cos(4 * math.pi * t), 0.999),
        ("lille variation om 4.2", 4.2 + 0.5 * np.cos(2 * math.pi * t), 0.999),
        ("vind-agtig, svag sæson", 5.0 + 0.4 * np.cos(2 * math.pi * t)
             + 0.3 * np.sin(4 * math.pi * t), 0.999),
    ]

    print(f"{'case':<26}  {'R²':>8}  {'mindste':>8}  dom")
    print("-" * 60)
    for name, truth, minimum in cases:
        coef, r2, _ = fit_series(weeks, truth)
        good = (r2 >= minimum) if minimum > 0 else True
        ok = ok and good
        print(f"{name:<26}  {r2:>8.4f}  {minimum:>8.3f}  "
              f"{'ok' if good else 'FEJL'}")

    # Konstant kurve: R² skal være nan (ss_tot = 0). Det er det RIGTIGE
    # svar, ikke en fejl — og koefficienten c0 skal ramme niveauet.
    const_y = np.full_like(t, 4.2)
    coef_c, r2_c, _ = fit_series(weeks, const_y)
    nan_ok = math.isnan(r2_c)
    level_ok = abs(coef_c[0] - 4.2) < 1e-9 and abs(coef_c[1]) < 1e-9 \
        and abs(coef_c[2]) < 1e-9
    ok = ok and nan_ok and level_ok
    print(f"{'konstant, 4.2 (R²=nan)':<26}  {str(round(r2_c,4) if not nan_ok else 'nan'):>8}  "
          f"{'nan':>8}  {'ok' if nan_ok and level_ok else 'FEJL'}")
    if not level_ok:
        print(f"    koefficienter: {np.round(coef_c, 6)}  (c0 skal være 4.2)")

    # Periodicitet: værdi OG hældning skal mødes i t=0 og t=1.
    coef, _, _ = fit_series(weeks, 8 - 9 * np.cos(2 * math.pi * t))
    eps = 1e-6
    f0, f1 = eval_fit(coef, np.array([1.0]))[0], eval_fit(coef, np.array([53.0]))[0]
    # hældning numerisk omkring de to ender
    d0 = (eval_fit(coef, np.array([1.0 + eps]))[0]
          - eval_fit(coef, np.array([1.0 - eps]))[0]) / (2 * eps)
    d1 = (eval_fit(coef, np.array([52.0 + eps]))[0]
          - eval_fit(coef, np.array([52.0 - eps]))[0]) / (2 * eps)
    # f(uge 53) er samme t som f(uge 1); hældningen ved 52 skal matche ved 0
    d_at_1 = (eval_fit(coef, np.array([53.0 + eps]))[0]
              - eval_fit(coef, np.array([53.0 - eps]))[0]) / (2 * eps)
    per_ok = abs(f0 - f1) < 1e-6 and abs(d0 - d_at_1) < 1e-4
    ok = ok and per_ok
    print("-" * 60)
    print(f"periodicitet f(1)=f(53): {abs(f0 - f1):.2e}   "
          f"hældning ved 1 vs 53: {abs(d0 - d_at_1):.2e}  "
          f"{'ok' if per_ok else 'FEJL'}")

    print("-" * 60)
    print("SELFTEST:", "BESTÅET" if ok else "FEJLEDE")
    return 0 if ok else 1


# ======================================================================= DB

FETCH_SQL = """
    SELECT wk, w_median, w_p10, w_p90, w_min, w_max,
           n_years, min_stations, max_stations
      FROM public.weather_national
     WHERE parameter = %s
       AND w_median IS NOT NULL
     ORDER BY wk
"""


def ensure_table(conn) -> None:
    """Fit-tabellen. Koefficienterne gemmes, så kurven kan tegnes uden at
    genkøre fittet — og så man kan se HVILKEN kurve der blev vist hvornår."""
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS public.weather_season_fit (
                location_id  text         NOT NULL,
                parameter    text         NOT NULL,
                n_harmonics  smallint     NOT NULL,
                n_coef       smallint     NOT NULL DEFAULT {n_coefficients()},
                r2           numeric(6,4) NOT NULL,
                coef         jsonb        NOT NULL,
                curve        jsonb        NOT NULL,
                fitted_at    timestamptz  NOT NULL DEFAULT now(),
                PRIMARY KEY (location_id, parameter, n_harmonics)
            )
        """)
        # Tilføj n_coef hvis tabellen fandtes fra før (den havde kun fire
        # koefficienter dengang — se docstringen om konstantleddet).
        cur.execute("""
            ALTER TABLE public.weather_season_fit
              ADD COLUMN IF NOT EXISTS n_coef smallint NOT NULL DEFAULT 5
        """)
    conn.commit()


def store_fit(conn, location_id, parameter, coef, r2, weeks, fitted) -> None:
    curve = [[int(w), round(float(v), 4)] for w, v in zip(weeks, fitted)]
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO public.weather_season_fit
                   (location_id, parameter, n_harmonics, n_coef,
                    r2, coef, curve, fitted_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (location_id, parameter, n_harmonics) DO UPDATE
               SET n_coef    = EXCLUDED.n_coef,
                   r2        = EXCLUDED.r2,
                   coef      = EXCLUDED.coef,
                   curve     = EXCLUDED.curve,
                   fitted_at = now()
        """, (location_id, parameter, N_HARMONICS, n_coefficients(),
              round(r2, 4),
              json.dumps([round(float(c), 6) for c in coef]),
              json.dumps(curve)))
    conn.commit()


def has_data(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM public.weather_national")
        return cur.fetchone()[0] > 0


# ------------------------------------------------- tegning hen over årsskiftet
#
# coef = json.loads(row["coef"])       # [c0, a1, b1, a2, b2]
# weeks = np.arange(45, 52 + 1 + 15)   # 45..67
# # brug eval_fit, eller skriv den ud:
# t = (weeks - 1) / 52.0
# y = (coef[0]
#      + coef[1]*np.sin(2*np.pi*t) + coef[2]*np.cos(2*np.pi*t)
#      + coef[3]*np.sin(4*np.pi*t) + coef[4]*np.cos(4*np.pi*t))
#
# Intet hak ved uge 52/1: t = 1.0 giver samme værdi og hældning som t = 0.0.


# ======================================================================= main

def parse_args():
    ap = argparse.ArgumentParser(description="Sæsonfit, landeniveau")
    ap.add_argument("--selftest", action="store_true",
                    help="afprøv fittet mod kendte kurver. Ingen database.")
    ap.add_argument("--dry-run", action="store_true",
                    help="vis R² men skriv ikke til DB")
    ap.add_argument("--parameter", default=None,
                    help="kun denne parameter (default: alle fittbare)")
    return ap.parse_args()


def main() -> int:
    args = parse_args()

    if args.selftest:
        return selftest()

    params = (args.parameter,) if args.parameter else FITTABLE

    with psycopg.connect(DSN) as conn:
        if not args.dry_run:
            ensure_table(conn)

        if not has_data(conn):
            log("weather_national er tom — kør weather-schema.sql først")
            return 1

        log(f"{'parameter':<14}  {'n':>3}  {'R²':>7}  {'band':>7}  vurdering")
        log("-" * 68)

        results = []
        for param in params:
            with conn.cursor() as cur:
                cur.execute(FETCH_SQL, (param,))
                rows = cur.fetchall()

            if len(rows) < 20:
                log(f"{param:<14}  {len(rows):>3}  {'—':>7}  "
                    f"{'—':>7}  for få uger, springer over")
                continue

            weeks = np.array([r[0] for r in rows], dtype=float)
            median = np.array([r[1] for r in rows], dtype=float)
            p10 = np.array([r[2] for r in rows], dtype=float)
            p90 = np.array([r[3] for r in rows], dtype=float)

            # n_years og min_stations bæres med, så et svagt fit kan skelnes
            # fra et tynd data-grundlag.
            n_years = int(rows[0][6])
            min_st = min(int(r[7]) for r in rows)
            band = float(np.mean(p90 - p10))

            coef, r2, fitted = fit_series(weeks, median)

            log(f"{param:<14}  {len(rows):>3}  {r2:>7.3f}  "
                f"{band:>7.1f}  {verdict_for(r2)}")
            results.append((param, r2, len(rows), n_years, min_st))

            if not args.dry_run:
                store_fit(conn, NATIONAL, param, coef, r2, weeks, fitted)

        log("-" * 68)
        if not results:
            log("intet at fitte")
            return 1

        weak = [p for p, r2, *_ in results if r2 < 0.25]
        if weak:
            log(f"bemærk: svag eller ingen sæson for {', '.join(weak)} — "
                f"det er svaret, ikke en fejl. Se noten i weather_weekly.")

        if args.dry_run:
            log("DRY RUN: intet skrevet til DB")
        else:
            log(f"skrev {len(results)} fit til weather_season_fit "
                f"(location_id = '{NATIONAL}')")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
