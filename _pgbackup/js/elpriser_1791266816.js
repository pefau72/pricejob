// /js/elpriser.js — Elpriser: graf over DK1/DK2 spotpriser
// Rammen for siden er en <canvas id="priceChart">; findes den ikke, gør vi ingenting.

let priceChart = null;   // holder på den aktuelle Chart-instans, så vi kan rydde op
let isLoading = false;   // forhindrer samtidige kald (Swup kan fyre flere events pr. navigation)
let loadedDays = {};        // { "2026-10-06": {...}, "2026-10-07": {...} }
let activeDay = null;       // den dag grafen viser lige nu

const CHART_MESSAGE_COLOR = '#6b7280';
const MISSING_DATA_MESSAGE = 'Kunne ikke indlæse elpriser';
const RELEASE_CUTOFF_MINUTES = 13 * 60 + 15;   // 13:15 dansk tid

/** Skriv til et element, hvis det findes. Returnerer false hvis ikke. */
function setText(id, text) {
    const el = document.getElementById(id);
    if (!el) return false;
    el.innerText = text;
    return true;
}

/** Hent én dag. Returnerer null hvis filen ikke findes. */
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


/** Dansk dato (Europe/Copenhagen) som YYYY-MM-DD, uafhængigt af brugerens tidszone. */
function dateStamp(date = new Date()) {
    // 'sv-SE' formaterer som YYYY-MM-DD og respekterer tidszonen
    return new Intl.DateTimeFormat('sv-SE', { timeZone: 'Europe/Copenhagen' }).format(date);
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
 * Hent den relevante prisfil.
 * Før cutoff: dagens fil. Efter cutoff: morgendagens fil, med fald tilbage til
 * dagens hvis morgendagens endnu ikke er lagt op (priserne kan være forsinkede).
 */
async function loadPrices(now = new Date()) {
    const today = dateStamp(now);
    const tomorrow = dateStamp(new Date(now.getTime() + 86400000));

    const order = afterReleaseCutoff(now) ? [tomorrow, today] : [today];

    for (const stamp of order) {
        const url = `../pricedata/prices-${stamp}.json`;
        try {
            const res = await fetch(url, { cache: 'no-store' });
            if (res.ok) return { data: await res.json(), stamp };
        } catch { /* prøv næste kandidat */ }
    }
    throw new Error(`Ingen prisdata for ${today} eller ${tomorrow}`);
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

function buildChartConfig(data) {
    const labels = data.hours.map(item => {
        const dateObj = new Date(item.ts);   // ts har offset (+02:00), så timerne er dansk tid
        return String(dateObj.getHours()).padStart(2, '0') + ':' +
               String(dateObj.getMinutes()).padStart(2, '0');
    });

    return {
        type: 'line',
        data: {
            labels,
            datasets: [
                {
                    label: 'DK1 (Vestdanmark)',
                    data: data.hours.map(item => item.dk1),
                    borderColor: '#3b82f6',
                    backgroundColor: 'rgba(59, 130, 246, 0.1)',
                    borderWidth: 2,
                    pointRadius: 1,
                    tension: 0.2
                },
                {
                    label: 'DK2 (Østdanmark)',
                    data: data.hours.map(item => item.dk2),
                    borderColor: '#ef4444',
                    backgroundColor: 'rgba(239, 68, 68, 0.1)',
                    borderWidth: 2,
                    pointRadius: 1,
                    tension: 0.2
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { position: 'top' },
                tooltip: { mode: 'index', intersect: false }
            },
            scales: {
                x: {
                    grid: { display: false },
                    ticks: { maxTicksLimit: 8, autoSkip: true, maxRotation: 0 },
                    title: { display: true, text: 'Tidspunkt på dagen' }
                },
                y: {
                    beginAtZero: false,
                    title: { display: true, text: data.unit }
                }
            }
        }
    };
}

export async function initPriceChart() {
    const chartCanvas = document.getElementById('priceChart');
    if (!chartCanvas) return;
    if (isLoading) return;
    isLoading = true;

    try {
        const today = dateStamp();
        const tomorrow = dateStamp(new Date(Date.now() + 86400000));

        const [todayData, tomorrowData] = await Promise.all([
            fetchDay(today), fetchDay(tomorrow)
        ]);

        // Re-søg efter await — Swup kan have skiftet containeren
        const canvas = document.getElementById('priceChart');
        if (!canvas) return;

        // Knapper: dagens priser altid, morgendagens hvis filen findes
        const statusEl = document.getElementById('day-switch');
        if (statusEl) {
            statusEl.innerHTML = '';
            const options = [];
            if (todayData) options.push({ stamp: today, label: 'I dag', data: todayData });
            if (tomorrowData) options.push({ stamp: tomorrow, label: 'I morgen', data: tomorrowData });

            if (!options.length) {
                setChartMessage(canvas, MISSING_DATA_MESSAGE);
                setText('page-meta', 'Ingen prisdata fundet');
                return;
            }

            for (const opt of options) { /* … som ovenfor … */ }
            if (todayData && !tomorrowData) {
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.textContent = 'I morgen';
                btn.disabled = true;
                btn.title = 'Ikke offentliggjort endnu';
                statusEl.appendChild(btn);
            }


            // Standard: i morgen hvis den findes og vi er forbi cutoff, ellers i dag
            const initial = (afterReleaseCutoff() && tomorrowData) ? tomorrow : today;
            showDay(initial, options);
        } else {
            // Ingen knap-container — fald tilbage til automatisk valg
            const pick = tomorrowData && afterReleaseCutoff() ? tomorrowData : todayData;
            if (!pick) { setChartMessage(canvas, MISSING_DATA_MESSAGE); return; }
            render(pick);
        }
    } catch (error) {
        const canvas = document.getElementById('priceChart');
        if (canvas) setChartMessage(canvas, MISSING_DATA_MESSAGE);
        setText('page-meta', 'Fejl: ' + error.message);
        console.error(error);
    } finally {
        isLoading = false;
    }
}

function showDay(stamp, options) {
    const data = loadedDays[stamp];
    if (!data) return;
    activeDay = stamp;

    // Marker den valgte knap
    document.querySelectorAll('#day-switch button').forEach(b => {
        b.setAttribute('aria-pressed', b.dataset.stamp === stamp ? 'true' : 'false');
        b.classList.toggle('is-active', b.dataset.stamp === stamp);
    });

    render(data, stamp);
}

function render(data, stamp) {
    const canvas = document.getElementById('priceChart');
    if (!canvas) return;

    if (priceChart) { priceChart.destroy(); priceChart = null; }

    const today = dateStamp();
    const isTomorrow = stamp > today;

    // Titel: hvilken dag grafen viser
    setText('page-title', 'Elpriser — ' + new Intl.DateTimeFormat('da-DK', {
        weekday: 'long', day: 'numeric', month: 'long', timeZone: 'Europe/Copenhagen'
    }).format(new Date(data.date)));

    setText('page-meta',
        `${isTomorrow ? 'Morgendagens priser' : 'Dagens priser'} · ${data.unit} · hentet ` +
        new Intl.DateTimeFormat('da-DK', {
            day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit',
            timeZone: 'Europe/Copenhagen'
        }).format(new Date(data.generated_utc)));

    priceChart = new Chart(canvas.getContext('2d'), buildChartConfig(data));
}

