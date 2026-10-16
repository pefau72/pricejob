// /js/vejr.js â€” Vejr: tre grafer (temperatur, strÃ¥ling, vind) for Danmark.
//
// Bygget oven pÃ¥ samme struktur som elpriser.js med vilje â€” samme sprogvalg,
// samme opslagsfunktion, samme fetch-mÃ¸nster, samme Chart.js-opbygning. To
// datasÃ¦t, Ã©n struktur at vedligeholde.
//
// Rammen for siden er tre canvasser:
//     <canvas id="tempChart">, <canvas id="radChart">, <canvas id="windChart">
//
// Datastrukturen er delt i tre FILER med hver sin kadence, sÃ¥ de grÃ¥ omrÃ¥der
// ikke sendes hver uge:
//
//     weatherdata/normals/weather-normal.json   bÃ¥nd + median, 52 uger
//     weatherdata/normals/weather-season.json   fit-koefficienter
//     weatherdata/weather-<Ã¥r>.json             Ã¥rets ugemÃ¥linger
//
// Normal-filen Ã¦ndrer sig nÃ¦sten aldrig, current-filen hver uge. Derfor
// caches de to fÃ¸rste aggressivt og den sidste ikke.
//
// Al trigonometri ligger i de udsendte tal: bÃ¥ndet er fÃ¦rdigglattet, og
// sÃ¦sonkurven leveres som koefficienter vi kan udregne billigt i browseren.
import '/js/chart.min.js';   // UMD: definerer window.Chart som bivirkning

const STRINGS = {
    en: {
        title:        'Weather in Denmark',
        subtitle:     'This year measured against the normal range',
        xAxis:        'Week',
        band:         'Normal range',
        thisYear:     'This year',
        season:       'Seasonal curve',
        loading:      'Loading weather data',
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
    de: {
        title:        'Wetter in Dänemark',
        subtitle:     'Dieses Jahr gemessen am Normalbereich',
        xAxis:        'Woche',
        band:         'Normalbereich',
        thisYear:     'Dieses Jahr',
        season:       'Saisonkurve',
        loading:      'Wetterdaten werden geladen',
        errorPrefix:  'Fehler: ',
        chartError:   'Wetterdaten konnten nicht geladen werden',
        fetched:      'abgerufen',
        none:         'Keine Wetterdaten gefunden',
        sourceNote:   'Quelle: DMI',
        locale:       'de-DE',
        params: {
            temperature_c: { label: 'Temperatur',    unit: '°C' },
            radiation_wm2: { label: 'Globale Strahlung', unit: 'W/m²' },
            wind_ms:       { label: 'Windgeschwindigkeit', unit: 'm/s' },
        },
    },
};

const LANGS = Object.keys(STRINGS);

// RÃ¦kkefÃ¸lgen pÃ¥ siden. Hver graf har sin egen canvas og sin egen enhed.
const CHARTS = [
    { param: 'temperature_c', canvas: 'tempChart' },
    { param: 'radiation_wm2', canvas: 'radChart'  },
    { param: 'wind_ms',       canvas: 'windChart' },
];

const CHART_MESSAGE_COLOR = '#6b7280';

// Farver. BÃ¥ndet er grÃ¥t (det er baggrund, ikke en mÃ¥ling), Ã¥rets kurve er
// den varme accent â€” samme princip som i figuren brugeren godkendte.
const COLOR_BAND   = 'rgba(148, 163, 184, 0.45)';
// Farven på ÅRETS kurve, pr. parameter.
//
// temperature_c er sort efter ønske. Bemærk at det bryder sidens øvrige
// farvedoktrin, hvor sort er reserveret til tekst og grå til akser/grid —
// en sort kurve kan læse som en ufærdig draft ved siden af de andre.
// De øvrige parametre bruger accentfarver.
const YEAR_COLOR = {
    temperature_c: '#dc2626',   // rød — stærk accent, nem at skelne
    radiation_wm2: '#f5a623',   // rav — som før
    wind_ms:       '#2563eb',   // blå (efter ønske)
};
const yearColor = param => YEAR_COLOR[param] ?? '#f5a623';
const COLOR_SEASON = '#94a3b8';

let charts = {};        // { temperature_c: Chart, ... }
let isLoading = false;  // forhindrer samtidige kald (Swup fyrer flere events)

// Cache pr. fil. Normals Ã¦ndrer sig nÃ¦sten aldrig; current skal hentes frisk.
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

/** Tekstopslag for det aktive sprog â€” falder tilbage til engelsk. */
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

/** Tegn en kort besked midt pÃ¥ canvas'et (indlÃ¦sning og fejl). */
function setChartMessage(canvas, message) {
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);   // Chart.js deler canvas
    ctx.fillStyle = CHART_MESSAGE_COLOR;
    ctx.font = '16px system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(message, canvas.width / 2, canvas.height / 2);
}


// ------------------------------------------------------------------ indlÃ¦sning

/**
 * Hent Ã©n JSON-fil. `immutable` sÃ¦tter lange cache-headere i brug for de
 * filer der nÃ¦sten aldrig Ã¦ndrer sig (normals), og 'no-store' for Ã¥rets
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
    // PrÃ¸ver indevÃ¦rende Ã¥r; findes filen ikke (fx i januar fÃ¸r fÃ¸rste push)
    // falder vi tilbage til sidste Ã¥r, sÃ¥ siden ikke stÃ¥r tom.
    return await fetchJson(`../weatherdata/weather-${year}.json`)
        ?? await fetchJson(`../weatherdata/weather-${year - 1}.json`);
}


// ------------------------------------------------------------------- beregning

/**
 * SÃ¦sonkurvens vÃ¦rdi i en given uge, ud fra fit-koefficienterne.
 *   c0 + a1Â·sin(2Ï€t) + b1Â·cos(2Ï€t) + a2Â·sin(4Ï€t) + b2Â·cos(4Ï€t)
 *   hvor t = (uge - 1) / 52
 * Bruges til den stiplede referencelinje. Billig nok til at regne i browseren
 * â€” bÃ¥ndet er derimod glattet pÃ¥ serveren og leveres fÃ¦rdigt.
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
 * Bygger Chart.js-konfigurationen for Ã©n parameter.
 *
 * BÃ¥ndet tegnes som et stablet par: den usynlige underkant (band_lo) og
 * tykkelsen (band_thick) ovenpÃ¥. RÃ¦kkefÃ¸lgen er ikke tilfÃ¦ldig â€” den
 * stablede serie skal ligge umiddelbart efter sin base.
 */
/**
 * Tegner normalbÃ¥ndet direkte pÃ¥ canvas som en flade mellem p10 og p90.
 *
 * HVORFOR ET PLUGIN OG IKKE fill:
 *   Fem forsÃ¸g med Chart.js' indbyggede mekanismer fejlede:
 *
 *     fill: { target: '-1' }   fylder til seriens EGEN basislinje (nul)
 *     stack: 'band'            tvinger y-aksen til at inkludere nul
 *     [lo, hi]-par i data      virker kun paa linear/time-akser, ikke category
 *     fill: '+1'               fylder til naeste series LINJE, ikke som flade
 *
 *   Ingen af dem tegner paalideligt et interval paa en kategori-akse. Plugin'et
 *   her goer det direkte: det kender begge seriers punkter, finder deres
 *   pixel-y for hver x, og tegner fladen mellem dem. Ingen afhaengighed af
 *   fillsemantik, ingen tvunget basislinje.
 *
 * Serierne selv tegnes usynligt (borderWidth 0) og bruges kun som databÃ¦rere.
 */
const normalBandPlugin = {
    id: 'normalBand',
    beforeDatasetsDraw(chart, args, opts) {
        if (!opts || !opts.upperKey || !opts.lowerKey) return;
        const { ctx, chartArea } = chart;
        const meta = chart.getDatasetMeta(0);          // nederste serie
        const metaHi = chart.getDatasetMeta(1);        // Ã¸verste serie
        if (!meta?.data?.length || !metaHi?.data?.length) return;

        ctx.save();
        ctx.beginPath();
        // Langs underkanten, venstre -> hÃ¸jre
        meta.data.forEach((pt, i) => {
            if (i === 0) ctx.moveTo(pt.x, pt.y);
            else ctx.lineTo(pt.x, pt.y);
        });
        // Og tilbage langs overkanten, hÃ¸jre -> venstre
        for (let i = metaHi.data.length - 1; i >= 0; i--) {
            const pt = metaHi.data[i];
            ctx.lineTo(pt.x, pt.y);
        }
        ctx.closePath();
        ctx.fillStyle = opts.color || 'rgba(148,163,184,0.45)';
        ctx.fill();

        // Tynnere kantlinjer saa baandet har en afgraensning
        ctx.strokeStyle = 'rgba(148,163,184,0.85)';
        ctx.lineWidth = 1;
        [meta, metaHi].forEach(m => {
            ctx.beginPath();
            m.data.forEach((pt, i) => (i === 0 ? ctx.moveTo(pt.x, pt.y) : ctx.lineTo(pt.x, pt.y)));
            ctx.stroke();
        });
        ctx.restore();
    },
};


function buildChartConfig(param, normals, season, current) {
    const meta = paramLabel(param);
    const weeks = normals?.weeks ?? Array.from({ length: 52 }, (_, i) => i + 1);
    const p = normals?.parameters?.[param] ?? {};
    const cur = current?.parameters?.[param] ?? {};

    const labels = weeks.map(String);
    const bandLo = p.band_lo ?? [];   // p10 â€” baandets underkant
    const bandHi = p.band_hi ?? [];   // p90 â€” baandets overkant

    // Ã…rets mÃ¥linger placeres pÃ¥ de uger vi faktisk har. Resten er null, sÃ¥
    // linjen stopper der i stedet for at fortsÃ¦tte gennem tomme uger.
    const curByWeek = {};
    (cur.weeks ?? []).forEach((w, i) => { curByWeek[w] = cur.values[i]; });
    const yearData = weeks.map(w => (w in curByWeek ? curByWeek[w] : null));

    // BÃ…NDET: to usynlige bÃ¦rerserier + normalBandPlugin ovenfor.
    // Serierne tegnes ikke selv; plugin'et tegner fladen mellem dem.
    const datasets = [
        {
            label: '',
            data: bandLo,           // p10 â€” underkanten
            borderWidth: 0,
            pointRadius: 0,
            backgroundColor: 'rgba(0,0,0,0)',
            fill: false,
        },
        {
            label: t('band'),
            data: bandHi,           // p90 â€” overkanten
            borderWidth: 0,
            pointRadius: 0,
            backgroundColor: 'rgba(0,0,0,0)',
            fill: false,
        },
    ];

    // SÃ¦sonkurven som stiplet reference â€” kun hvis vi har koefficienter.
    const coef = season?.parameters?.[param]?.coef;
    if (coef && coef.length === 5) {
        datasets.push({
            label: t('season'),
            data: weeks.map(w => Number(seasonValue(coef, w).toFixed(1))),
            borderColor: COLOR_SEASON,
            borderWidth: 1.5,
            borderDash: [6, 4],
            pointRadius: 0,
            fill: false,
        });
    }

    datasets.push({
        label: t('thisYear'),
        data: yearData,
        borderColor: yearColor(param),
        borderWidth: 3,
        pointRadius: 0,
        fill: false,
        spanGaps: false,    // uden denne tegnes linjen gennem uger uden data
    });

    return {
        type: 'line',
        data: { labels, datasets },
        plugins: [normalBandPlugin],
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                // Skal ligge i SAMME plugins-objekt som title/legend/tooltip.
                // Laa den i en separat plugins-noegle, vandt den sidste og
                // normalBand forsvandt — plugin'et returnerede straks fordi
                // opts var undefined, uden fejl og uden flade.
                normalBand: { lowerKey: 'band_lo', upperKey: 'band_hi', color: COLOR_BAND },
                title: { display: true, text: meta.label },
                legend: {
                    position: 'top',
                    // Skjul den usynlige base (tom label) â€” ellers tegner
                    // Chart.js en tom firkant i legenden.
                    labels: {
                        filter: item => item.text !== '',
                    },
                },
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
                    // Intet beginAtZero og ingen fast min/max: Chart.js skal
                    // tilpasse aksen til data â€” ogsaa naar baandet gaar under
                    // nul (temperatur om vinteren) eller naar det er hoejt
                    // (straaling om sommeren). Et fast gulv ved nul klipper
                    // bÃ¥ndets underkant for temperatur.
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

        charts[spec.param]?.destroy();      // ryd op fÃ¸r en ny tegnes
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

/** Kaldes af app.js efter hver navigation (samme mÃ¸nster som elpriser.js). */
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

        // Ved fÃ¸rste visning kan canvas'erne mangle (Swup bytter DOM'en ind
        // asynkront). Vent en tick, sÃ¥ de findes.
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
