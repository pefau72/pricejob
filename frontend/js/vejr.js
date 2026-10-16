// /js/vejr.js — Vejr: tre grafer (temperatur, stråling, vind) for Danmark.
//
// Bygget oven på samme struktur som elpriser.js med vilje — samme sprogvalg,
// samme opslagsfunktion, samme fetch-mønster, samme Chart.js-opbygning. To
// datasæt, én struktur at vedligeholde.
//
// Rammen for siden er tre canvasser:
//     <canvas id="tempChart">, <canvas id="radChart">, <canvas id="windChart">
//
// Datastrukturen er delt i tre FILER med hver sin kadence, så de grå områder
// ikke sendes hver uge:
//
//     weatherdata/normals/weather-normal.json   bånd + median, 52 uger
//     weatherdata/normals/weather-season.json   fit-koefficienter
//     weatherdata/weather-<år>.json             årets ugemålinger
//
// Normal-filen ændrer sig næsten aldrig, current-filen hver uge. Derfor
// caches de to første aggressivt og den sidste ikke.
//
// Al trigonometri ligger i de udsendte tal: båndet er færdigglattet, og
// sæsonkurven leveres som koefficienter vi kan udregne billigt i browseren.
import '/js/chart.min.js';   // UMD: definerer window.Chart som bivirkning

const STRINGS = {
    en: {
        title:        'Weather in Denmark',
        subtitle:     'This year measured against the normal range',
        xAxis:        'Week',
        band:         'Normal range',
        thisYear:     'This year',
        season:       'Seasonal curve',
        loading:      'Loading weather data…',
        errorPrefix:  'Error: ',
        chartError:   'Could not load weather data',
        fetched:      'fetched',
        none:         'No weather data found',
        sourceNote:   'Source: DMI',
        locale:       'en-GB',
        params: {
            temperature_c: { label: 'Temperature',      unit: '°C' },
            radiation_wm2: { label: 'Global radiation', unit: 'W/m²' },
            wind_ms:       { label: 'Wind speed',       unit: 'm/s' },
        },
    },
    da: {
        title:        'Vejret i Danmark',
        subtitle:     'I år målt mod normalområdet',
        xAxis:        'Uge',
        band:         'Normalområde',
        thisYear:     'I år',
        season:       'Sæsonkurve',
        loading:      'Indlæser vejrdata…',
        errorPrefix:  'Fejl: ',
        chartError:   'Kunne ikke indlæse vejrdata',
        fetched:      'hentet',
        none:         'Ingen vejrdata fundet',
        sourceNote:   'Kilde: DMI',
        locale:       'da-DK',
        params: {
            temperature_c: { label: 'Temperatur',    unit: '°C' },
            radiation_wm2: { label: 'Globalstråling', unit: 'W/m²' },
            wind_ms:       { label: 'Vindhastighed', unit: 'm/s' },
        },
    },
};

const LANGS = Object.keys(STRINGS);

// Rækkefølgen på siden. Hver graf har sin egen canvas og sin egen enhed.
const CHARTS = [
    { param: 'temperature_c', canvas: 'tempChart' },
    { param: 'radiation_wm2', canvas: 'radChart'  },
    { param: 'wind_ms',       canvas: 'windChart' },
];

const CHART_MESSAGE_COLOR = '#6b7280';

// Farver. Båndet er gråt (det er baggrund, ikke en måling), årets kurve er
// den varme accent — samme princip som i figuren brugeren godkendte.
const COLOR_BAND   = 'rgba(148, 163, 184, 0.45)';
const COLOR_YEAR   = '#f5a623';
const COLOR_SEASON = '#94a3b8';

let charts = {};        // { temperature_c: Chart, ... }
let isLoading = false;  // forhindrer samtidige kald (Swup fyrer flere events)

// Cache pr. fil. Normals ændrer sig næsten aldrig; current skal hentes frisk.
let cacheNormals = null;
let cacheSeason = null;


/** Sproget for den aktuelle side, udledt af stien: /de/vejr.html -> "de". */
function currentLang() {
    const seg = window.location.pathname.split('/')[1];
    if (LANGS.includes(seg)) return seg;
    const html = (document.documentElement.lang || '').toLowerCase();
    if (LANGS.includes(html)) return html;
    return 'en';
}

/** Tekstopslag for det aktive sprog — falder tilbage til engelsk. */
function t(key) {
    const lang = currentLang();
    return STRINGS[lang]?.[key] ?? STRINGS.en[key] ?? key;
}

/** Parametertekst (navn og enhed) for det aktive sprog. */
function paramLabel(param) {
    const lang = currentLang();
    const table = STRINGS[lang]?.params ?? STRINGS.en.params;
    return table[param] ?? { label: param, unit: '' };
}

/** Skriv til et element, hvis det findes. */
function setText(id, text) {
    const el = document.getElementById(id);
    if (!el) return false;
    el.innerText = text;
    return true;
}

/** Tegn en kort besked midt på canvas'et (indlæsning og fejl). */
function setChartMessage(canvas, message) {
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);   // Chart.js deler canvas
    ctx.fillStyle = CHART_MESSAGE_COLOR;
    ctx.font = '16px system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(message, canvas.width / 2, canvas.height / 2);
}


// ------------------------------------------------------------------ indlæsning

/**
 * Hent én JSON-fil. `immutable` sætter lange cache-headere i brug for de
 * filer der næsten aldrig ændrer sig (normals), og 'no-store' for årets
 * kurve som opdateres ugentligt.
 */
async function fetchJson(url, { immutable = false } = {}) {
    try {
        const res = await fetch(url, { cache: immutable ? 'default' : 'no-store' });
        if (!res.ok) return null;
        return await res.json();
    } catch {
        return null;
    }
}

async function loadNormals() {
    if (cacheNormals) return cacheNormals;
    cacheNormals = await fetchJson('../weatherdata/normals/weather-normal.json',
                                   { immutable: true });
    return cacheNormals;
}

async function loadSeason() {
    if (cacheSeason) return cacheSeason;
    cacheSeason = await fetchJson('../weatherdata/normals/weather-season.json',
                                  { immutable: true });
    return cacheSeason;
}

async function loadCurrent() {
    const year = new Date().getFullYear();
    // Prøver indeværende år; findes filen ikke (fx i januar før første push)
    // falder vi tilbage til sidste år, så siden ikke står tom.
    return await fetchJson(`../weatherdata/weather-${year}.json`)
        ?? await fetchJson(`../weatherdata/weather-${year - 1}.json`);
}


// ------------------------------------------------------------------- beregning

/**
 * Sæsonkurvens værdi i en given uge, ud fra fit-koefficienterne.
 *   c0 + a1·sin(2πt) + b1·cos(2πt) + a2·sin(4πt) + b2·cos(4πt)
 *   hvor t = (uge - 1) / 52
 * Bruges til den stiplede referencelinje. Billig nok til at regne i browseren
 * — båndet er derimod glattet på serveren og leveres færdigt.
 */
function seasonValue(coef, week) {
    const t = (week - 1) / 52;
    let y = coef[0];
    for (let h = 1; h <= 2; h++) {
        y += coef[2 * h - 1] * Math.sin(2 * Math.PI * h * t);
        y += coef[2 * h] * Math.cos(2 * Math.PI * h * t);
    }
    return y;
}


// ----------------------------------------------------------------- opbygning

/**
 * Bygger Chart.js-konfigurationen for én parameter.
 *
 * Båndet tegnes som et stablet par: den usynlige underkant (band_lo) og
 * tykkelsen (band_thick) ovenpå. Rækkefølgen er ikke tilfældig — den
 * stablede serie skal ligge umiddelbart efter sin base.
 */
function buildChartConfig(param, normals, season, current) {
    const meta = paramLabel(param);
    const weeks = normals?.weeks ?? Array.from({ length: 52 }, (_, i) => i + 1);
    const p = normals?.parameters?.[param] ?? {};
    const cur = current?.parameters?.[param] ?? {};

    const labels = weeks.map(String);
    const bandLo = p.band_lo ?? [];
    const bandThick = p.band_thick ?? [];

    // Årets målinger placeres på de uger vi faktisk har. Resten er null, så
    // linjen stopper der i stedet for at fortsætte gennem tomme uger.
    const curByWeek = {};
    (cur.weeks ?? []).forEach((w, i) => { curByWeek[w] = cur.values[i]; });
    const yearData = weeks.map(w => (w in curByWeek ? curByWeek[w] : null));

    const datasets = [
        {
            label: t('band') + ' · ' + t('band').toLowerCase(),
            data: bandLo,
            borderWidth: 0,
            pointRadius: 0,
            fill: false,
        },
        {
            label: t('band'),
            data: bandThick,
            borderWidth: 0,
            pointRadius: 0,
            backgroundColor: COLOR_BAND,
            fill: { target: '-1' },     // '-1' = det foregående datasæt (bandLo)
        },
    ];

    // Sæsonkurven som stiplet reference — kun hvis vi har koefficienter.
    const coef = season?.parameters?.[param]?.coef;
    if (coef && coef.length === 5) {
        datasets.push({
            label: t('season'),
            data: weeks.map(w => Number(seasonValue(coef, w).toFixed(1))),
            borderColor: COLOR_SEASON,
            borderWidth: 2,
            borderDash: [6, 4],
            pointRadius: 0,
            fill: false,
        });
    }

    datasets.push({
        label: t('thisYear'),
        data: yearData,
        borderColor: COLOR_YEAR,
        borderWidth: 3,
        pointRadius: 0,
        fill: false,
        spanGaps: false,    // uden denne tegnes linjen gennem uger uden data
    });

    return {
        type: 'line',
        data: { labels, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                title: { display: true, text: meta.label },
                legend: { position: 'top' },
                tooltip: {
                    mode: 'index',
                    intersect: false,
                    callbacks: {
                        label: ctx =>
                            `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(1)} ${meta.unit}`,
                    },
                },
            },
            scales: {
                x: {
                    grid: { display: false },
                    ticks: { maxTicksLimit: 12, autoSkip: true, maxRotation: 0 },
                    title: { display: true, text: t('xAxis') },
                },
                y: {
                    // beginAtZero for stråling (fysisk gulv ved 0), ikke for
                    // temperatur — en frostkurve skal kunne gå under nul.
                    beginAtZero: param === 'radiation_wm2' || param === 'wind_ms',
                    title: { display: true, text: meta.unit },
                },
            },
        },
    };
}


// ------------------------------------------------------------------ rendering

function render(normals, season, current) {
    for (const spec of CHARTS) {
        const canvas = document.getElementById(spec.canvas);
        if (!canvas) continue;

        const meta = paramLabel(spec.param);
        const hasNormals = !!normals?.parameters?.[spec.param];
        const hasCurrent = !!current?.parameters?.[spec.param];

        if (!hasNormals && !hasCurrent) {
            setChartMessage(canvas, t('none') + ': ' + meta.label);
            continue;
        }

        charts[spec.param]?.destroy();      // ryd op før en ny tegnes
        charts[spec.param] = new Chart(
            canvas.getContext('2d'),
            buildChartConfig(spec.param, normals, season, current)
        );
    }

    const meta = document.getElementById('page-meta');
    if (meta && current?.generated_at) {
        const when = new Intl.DateTimeFormat(t('locale'), {
            dateStyle: 'short', timeStyle: 'short',
            timeZone: 'Europe/Copenhagen',
        }).format(new Date(current.generated_at));
        meta.innerText = `${t('sourceNote')} · ${t('fetched')} ${when}`;
    }
}


// ---------------------------------------------------------------------- init

/** Kaldes af app.js efter hver navigation (samme mønster som elpriser.js). */
export async function initWeatherCharts() {
    if (isLoading) return;
    isLoading = true;

    try {
        for (const spec of CHARTS) {
            const canvas = document.getElementById(spec.canvas);
            if (canvas) setChartMessage(canvas, t('loading'));
        }
        if (document.getElementById('vejr-title')) setText('vejr-title', t('title'));
        if (document.getElementById('vejr-subtitle')) setText('vejr-subtitle', t('subtitle'));

        // Ved første visning kan canvas'erne mangle (Swup bytter DOM'en ind
        // asynkront). Vent en tick, så de findes.
        await new Promise(r => requestAnimationFrame(r));

        const [normals, season, current] = await Promise.all([
            loadNormals(), loadSeason(), loadCurrent(),
        ]);

        if (!normals && !current) {
            for (const spec of CHARTS) {
                const canvas = document.getElementById(spec.canvas);
                if (canvas) setChartMessage(canvas, t('chartError'));
            }
            return;
        }

        render(normals, season, current);
    } catch (err) {
        for (const spec of CHARTS) {
            const canvas = document.getElementById(spec.canvas);
            if (canvas) setChartMessage(canvas, t('errorPrefix') + (err?.message ?? err));
        }
    } finally {
        isLoading = false;
    }
}
