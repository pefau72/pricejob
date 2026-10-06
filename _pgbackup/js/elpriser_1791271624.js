// /js/elpriser.js — Elpriser: graf over DK1/DK2 (og DE på /de)
// Rammen for siden er en <canvas id="priceChart"> + <div id="day-switch">.
// Begge dage hentes; brugeren kan skifte mellem dem. Er morgendagens fil ikke
// offentliggjort endnu, vises knappen grået.
//
// Sprog: siden ligger i /da/, /en/, /de/ osv. Vi læser sproget fra stien (samme
// regel som app.js' currentLang()). Sproget vælger BÅDE hvilke prisområder der
// tegnes og hvilken valuta der vises:
//
//     da -> DK1 + DK2, DKK
//     en -> DK1 + DK2, DKK
//     de -> DE,        EUR
//
// Datastrukturen i JSON'en er flad: hvert punkt har "dk1", "dk1_eur", "de",
// "de_eur", "no2", "no2_eur", ... så et område i én valuta er ét opslag.

const STRINGS = {
    da: {
        today:        'I dag',
        tomorrow:     'I morgen',
        tomorrowNA:   'Morgendagens priser er ikke offentliggjort endnu',
        titlePrefix:  'Elpriser',
        metaToday:    'Dagens priser',
        metaTomorrow: 'Morgendagens priser',
        fetched:      'hentet',
        xAxis:        'Tidspunkt på dagen',
        loading:      'Indlæser elpriser…',
        none:         'Ingen prisdata fundet',
        errorPrefix:  'Fejl: ',
        chartError:   'Kunne ikke indlæse elpriser',
        locale:       'da-DK'
    },
    en: {
        today:        'Today',
        tomorrow:     'Tomorrow',
        tomorrowNA:   "Tomorrow's prices are not published yet",
        titlePrefix:  'Electricity prices',
        metaToday:    "Today's prices",
        metaTomorrow: "Tomorrow's prices",
        fetched:      'fetched',
        xAxis:        'Time of day',
        loading:      'Loading prices…',
        none:         'No price data found',
        errorPrefix:  'Error: ',
        chartError:   'Could not load prices',
        locale:       'en-GB'
    },
    de: {
        today:        'Heute',
        tomorrow:     'Morgen',
        tomorrowNA:   'Die Preise für morgen sind noch nicht veröffentlicht',
        titlePrefix:  'Strompreise',
        metaToday:    'Heutige Preise',
        metaTomorrow: 'Morgige Preise',
        fetched:      'abgerufen',
        xAxis:        'Tageszeit',
        loading:      'Preise werden geladen…',
        none:         'Keine Preisdaten gefunden',
        errorPrefix:  'Fehler: ',
        chartError:   'Preise konnten nicht geladen werden',
        locale:       'de-DE'
    }
};

const LANGS = Object.keys(STRINGS);

// Hvilke prisområder hver sprogside tegner, og i hvilken valuta.
// Sidens publikum bestemmer, ikke datas oprindelse.
const AREA_SETS = {
    da: { areas: ['dk1', 'dk2'], currency: 'dkk' },
    en: { areas: ['dk1', 'dk2'], currency: 'dkk' },
    de: { areas: ['de'],          currency: 'eur' }
};

// Områdenavne pr. sprog. Nøglen er det flade felt-navn uden valutasuffix.
const AREA_LABEL = {
    dk1: { da: 'DK1 (Vestdanmark)', en: 'DK1 (Western Denmark)', de: 'DK1 (Westdänemark)' },
    dk2: { da: 'DK2 (Østdanmark)',  en: 'DK2 (Eastern Denmark)', de: 'DK2 (Ostdänemark)' },
    de:  { da: 'DE (Tyskland)',     en: 'DE (Germany)',          de: 'DE (Deutschland)' },
    no2: { da: 'NO2 (Norge)',       en: 'NO2 (Norway)',          de: 'NO2 (Norwegen)' },
    se3: { da: 'SE3 (Sverige)',     en: 'SE3 (Sweden)',          de: 'SE3 (Schweden)' },
    se4: { da: 'SE4 (Sverige)',     en: 'SE4 (Sweden)',          de: 'SE4 (Schweden)' }
};

const AREA_COLOR = {
    dk1: '#3b82f6',   // blå
    dk2: '#ef4444',   // rød
    de:  '#10b981',   // grøn
    no2: '#8b5cf6',   // lilla
    se3: '#f59e0b',   // rav
    se4: '#14b8a6'    // teal
};

let priceChart = null;   // holder på den aktuelle Chart-instans, så vi kan rydde op
let isLoading = false;   // forhindrer samtidige kald (Swup kan fyre flere events pr. navigation)

const loadedDays = {};   // { "2026-10-06": {...}, "2026-10-07": {...} }

const CHART_MESSAGE_COLOR = '#6b7280';
const RELEASE_CUTOFF_MINUTES = 13 * 60 + 15;   // 13:15 dansk tid

/** Sproget for den aktuelle side, udledt af stien: /de/services.html -> "de". */
function currentLang() {
    const seg = window.location.pathname.split('/')[1];
    if (LANGS.includes(seg)) return seg;
    const html = (document.documentElement.lang || '').toLowerCase();
    if (LANGS.includes(html)) return html;
    return 'da';   // prisen er dansk; dansk er den rimelige standard
}

/** Tekstopslag for det aktive sprog — falder tilbage til dansk ved manglende nøgle. */
function t(key) {
    const lang = currentLang();
    return STRINGS[lang]?.[key] ?? STRINGS.da[key] ?? key;
}

/** Områdenavn for det aktive sprog. */
function areaLabel(key) {
    const lang = currentLang();
    return AREA_LABEL[key]?.[lang] ?? AREA_LABEL[key]?.da ?? key.toUpperCase();
}

/** Skriv til et element, hvis det findes. Returnerer false hvis ikke. */
function setText(id, text) {
    const el = document.getElementById(id);
    if (!el) return false;
    el.innerText = text;
    return true;
}

/** Dansk dato (Europe/Copenhagen) som YYYY-MM-DD, uafhængigt af brugerens tidszone. */
function dateStamp(date = new Date()) {
    // 'sv-SE' formaterer som YYYY-MM-DD og respekterer tidszonen
    return new Intl.DateTimeFormat('sv-SE', { timeZone: 'Europe/Copenhagen' }).format(date);
}

/** Dansk dato i morgen, som YYYY-MM-DD. */
function tomorrowStamp(now = new Date()) {
    return dateStamp(new Date(now.getTime() + 86400000));
}

/** Er vi forbi prisoffentliggørelsen i dag (dansk tid)? */
function afterReleaseCutoff(now = new Date()) {
    const parts = new Intl.DateTimeFormat('en-GB', {
        timeZone: 'Europe/Copenhagen',
        hour: '2-digit', minute: '2-digit', hour12: false
    }).formatToParts(now);
    const h = Number(parts.find(p => p.type === 'hour').value);
    const m = Number(parts.find(p => p.type === 'minute').value);
    return (h * 60 + m) >= RELEASE_CUTOFF_MINUTES;
}

/**
 * Hent den prisværdi grafen skal bruge for ét punkt.
 * Feltet er fladt: "dk1" for DKK, "dk1_eur" for EUR.
 */
function valueFor(point, area, currency) {
    const key = currency === 'eur' ? `${area}_eur` : area;
    const v = point[key];
    return typeof v === 'number' ? v : null;
}

/** Hent én dags fil. Cacher i loadedDays. Returnerer null hvis filen ikke findes. */
async function fetchDay(stamp) {
    if (loadedDays[stamp]) return loadedDays[stamp];
    try {
        const res = await fetch(`../pricedata/prices-${stamp}.json`, { cache: 'no-store' });
        if (!res.ok) return null;
        const data = await res.json();
        loadedDays[stamp] = data;
        return data;
    } catch {
        return null;
    }
}

/** Tegn en kort besked midt på canvas'et (bruges til indlæsning og fejl). */
function setChartMessage(canvas, message) {
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height); // Chart.js deler canvas — ryd først
    ctx.fillStyle = CHART_MESSAGE_COLOR;
    ctx.font = '16px system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(message, canvas.width / 2, canvas.height / 2);
}

/** Enhedsstreng for den valgte valuta, læst fra filen frem for hardcodet. */
function unitFor(data, currency) {
    return currency === 'eur'
        ? (data.unit_eur || 'EUR_per_kWh')
        : (data.unit || 'DKK_per_kWh');
}

function buildChartConfig(data, { areas, currency }) {
    const locale = t('locale');
    const unit = unitFor(data, currency);

    const labels = data.hours.map(item => {
        const dateObj = new Date(item.ts);   // ts har offset (+02:00), så timerne er dansk tid
        const h = String(dateObj.getHours()).padStart(2, '0');
        const m = dateObj.getMinutes();
        return m === 0 ? `${h}:00` : '';     // label kun på hele timer — autoSkip skjuler resten
    });

    // Tallene formateres med sidens lokale konvention (decimal-komma på da/de).
    const nf = new Intl.NumberFormat(locale, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
    });

    const datasets = areas
        .map(area => {
            const values = data.hours.map(p => valueFor(p, area, currency));
            // Spring et område over, hvis filen slet ikke har det (ældre JSON)
            if (!values.some(v => v !== null)) return null;
            return {
                label: areaLabel(area),
                data: values,
                borderColor: AREA_COLOR[area] || '#888',
                backgroundColor: 'rgba(0,0,0,0.04)',
                borderWidth: 2,
                pointRadius: 1,
                tension: 0.2,
                spanGaps: true
            };
        })
        .filter(Boolean);

    return {
        type: 'line',
        data: { labels, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: { position: 'top' },
                tooltip: {
                    mode: 'index',
                    intersect: false,
                    callbacks: {
                        label: ctx => `${ctx.dataset.label}: ${nf.format(ctx.parsed.y)} ${unit}`
                    }
                }
            },
            scales: {
                x: {
                    grid: { display: false },
                    ticks: { maxTicksLimit: 12, autoSkip: true, maxRotation: 0 },
                    title: { display: true, text: t('xAxis') }
                },
                y: {
                    beginAtZero: false,
                    ticks: { callback: v => nf.format(v) },
                    title: { display: true, text: unit, padding: { bottom: 8 } }
                }
            }
        }
    };
}

/** Tegn grafen for én dags data og opdatér titel/meta. */
function render(data, stamp) {
    const canvas = document.getElementById('priceChart');
    if (!canvas) return;

    if (priceChart) { priceChart.destroy(); priceChart = null; }

    const lang = currentLang();
    const spec = AREA_SETS[lang] ?? AREA_SETS.da;
    const isTomorrow = stamp > dateStamp();

    setText('page-title', t('titlePrefix') + ' — ' + new Intl.DateTimeFormat(t('locale'), {
        weekday: 'long', day: 'numeric', month: 'long',
        timeZone: 'Europe/Copenhagen'
    }).format(new Date(data.date)));

    setText('page-meta',
        `${isTomorrow ? t('metaTomorrow') : t('metaToday')} · ` +
        `${unitFor(data, spec.currency)} · ${t('fetched')} ` +
        new Intl.DateTimeFormat(t('locale'), {
            day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit',
            timeZone: 'Europe/Copenhagen'
        }).format(new Date(data.generated_utc)));

    priceChart = new Chart(canvas.getContext('2d'), buildChartConfig(data, spec));
}

/** Vis en valgt dag: markér knappen og tegn grafen. */
function showDay(stamp) {
    const data = loadedDays[stamp];
    if (!data) return;

    document.querySelectorAll('#day-switch button').forEach(b => {
        const on = b.dataset.stamp === stamp;
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
        b.classList.toggle('is-active', on);
    });

    render(data, stamp);
}

/**
 * Byg de to knapper. Der er ALTID to: i dag og i morgen.
 * Mangler morgendagens fil, er den knap deaktiveret med en forklaring.
 */
function buildDaySwitch(today, tomorrow, todayData, tomorrowData) {
    const host = document.getElementById('day-switch');
    if (!host) return;

    host.innerHTML = '';

    const make = (stamp, label, available, title) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.textContent = label;
        btn.dataset.stamp = stamp;
        btn.disabled = !available;
        if (!available) btn.title = title || t('tomorrowNA');
        if (available) btn.addEventListener('click', () => showDay(stamp));
        host.appendChild(btn);
    };

    make(today, t('today'), !!todayData);
    make(tomorrow, t('tomorrow'), !!tomorrowData, t('tomorrowNA'));
}

export async function initPriceChart() {
    const chartCanvas = document.getElementById('priceChart');
    if (!chartCanvas) return;   // ikke elpris-siden
    if (isLoading) return;

    isLoading = true;

    try {
        const today = dateStamp();
        const tomorrow = tomorrowStamp();

        // Hent begge dage parallelt. Den ene kan mangle uden at det er en fejl.
        const [todayData, tomorrowData] = await Promise.all([
            fetchDay(today),
            fetchDay(tomorrow)
        ]);

        // Swup kan have udskiftet containeren mens vi ventede — re-søg elementerne
        const canvas = document.getElementById('priceChart');
        if (!canvas) return;

        buildDaySwitch(today, tomorrow, todayData, tomorrowData);

        if (!todayData && !tomorrowData) {
            setChartMessage(canvas, t('chartError'));
            setText('page-meta', t('none'));
            return;
        }

        // Standardvalg: i morgen hvis den findes og vi er forbi cutoff, ellers i dag.
        const start = (afterReleaseCutoff() && tomorrowData) ? tomorrow : today;
        showDay(loadedDays[start] ? start : today);
    } catch (error) {
        const canvas = document.getElementById('priceChart');
        if (canvas) setChartMessage(canvas, t('chartError'));
        setText('page-meta', t('errorPrefix') + error.message);
        console.error(error);
    } finally {
        isLoading = false;
    }
}
