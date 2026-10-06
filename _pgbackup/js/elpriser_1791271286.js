// /js/elpriser.js — Elpriser: graf over DK1/DK2 spotpriser
// Rammen for siden er en <canvas id="priceChart"> + <div id="day-switch">.
// Begge dage hentes; brugeren kan skifte mellem dem. Er morgendagens fil ikke
// offentliggjort endnu, vises knappen grået.

let priceChart = null;   // holder på den aktuelle Chart-instans, så vi kan rydde op
let isLoading = false;   // forhindrer samtidige kald (Swup kan fyre flere events pr. navigation)

const loadedDays = {};   // { "2026-10-06": {...}, "2026-10-07": {...} }

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

function buildChartConfig(data) {
    const labels = data.hours.map(item => {
        const dateObj = new Date(item.ts);   // ts har offset (+02:00), så timerne er dansk tid
        const h = String(dateObj.getHours()).padStart(2, '0');
        const m = dateObj.getMinutes();
        return m === 0 ? `${h}:00` : '';     // label kun på hele timer — autoSkip skjuler resten
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
                    ticks: { maxTicksLimit: 12, autoSkip: true, maxRotation: 0 },
                    title: { display: true, text: 'Tidspunkt på dagen' }
                },
                y: {
                    beginAtZero: false,
                    title: { display: true, text: data.unit, padding: { bottom: 8 } }
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

    const isTomorrow = stamp > dateStamp();

    setText('page-title', 'Elpriser — ' + new Intl.DateTimeFormat('da-DK', {
        weekday: 'long', day: 'numeric', month: 'long',
        timeZone: 'Europe/Copenhagen'
    }).format(new Date(data.date)));

    setText('page-meta',
        `${isTomorrow ? 'Morgendagens priser' : 'Dagens priser'} · ` +
        `${data.unit} · hentet ` +
        new Intl.DateTimeFormat('da-DK', {
            day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit',
            timeZone: 'Europe/Copenhagen'
        }).format(new Date(data.generated_utc)));

    priceChart = new Chart(canvas.getContext('2d'), buildChartConfig(data));
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
 * Byg de to knapper. Der er ALTID to: I dag og I morgen.
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
        if (!available) btn.title = title || 'Ikke offentliggjort endnu';
        if (available) btn.addEventListener('click', () => showDay(stamp));
        host.appendChild(btn);
    };

    make(today, 'I dag', !!todayData);
    make(tomorrow, 'I morgen', !!tomorrowData, 'Morgendagens priser er ikke offentliggjort endnu');
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
            setChartMessage(canvas, MISSING_DATA_MESSAGE);
            setText('page-meta', 'Ingen prisdata fundet');
            return;
        }

        // Standardvalg: i morgen hvis den findes og vi er forbi cutoff, ellers i dag.
        const start = (afterReleaseCutoff() && tomorrowData) ? tomorrow : today;
        showDay(loadedDays[start] ? start : today);
    } catch (error) {
        const canvas = document.getElementById('priceChart');
        if (canvas) setChartMessage(canvas, MISSING_DATA_MESSAGE);
        setText('page-meta', 'Fejl: ' + error.message);
        console.error(error);
    } finally {
        isLoading = false;
    }
}
