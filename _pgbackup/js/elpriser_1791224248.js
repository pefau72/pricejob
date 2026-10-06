// /js/elpriser.js — Elpriser: graf over DK1/DK2 spotpriser
// Rammen for siden er en <canvas id="priceChart">; findes den ikke, gør vi ingenting.

let priceChart = null;   // holder på den aktuelle Chart-instans, så vi kan rydde op
let isLoading = false;   // forhindrer samtidige kald (Swup kan fyre flere events pr. navigation)

const CHART_MESSAGE_COLOR = '#6b7280';
const MISSING_DATA_MESSAGE = 'Kunne ikke indlæse elpriser';

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

/** Dags dato som YYYY-MM-DD i lokal tid (bruges til at bygge filnavnet). */
function todayStamp() {
    const d = new Date();
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function buildChartConfig(canvas, data) {
    const labels = data.hours.map(item => {
        const dateObj = new Date(item.ts);
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
            // Canvas har fast højde i inline style — sørg for at Chart.js holder sig til den
            maintainAspectRatio: false,
            plugins: {
                legend: { position: 'top' },
                tooltip: { mode: 'index', intersect: false }
            },
            scales: {
                x: {
                    grid: { display: false },
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

export function initPriceChart() {
    const chartCanvas = document.getElementById('priceChart');
    if (!chartCanvas) return;   // ikke elpris-siden
    if (isLoading) return;

    isLoading = true;

    if (priceChart) { priceChart.destroy(); priceChart = null; }

    setChartMessage(chartCanvas, 'Indlæser elpriser…');
    setText('page-meta', 'Indlæser data...');          // ← var linje 96

    const jsonFile = `../pricedata/prices-${todayStamp()}.json`;

    fetch(jsonFile)
        .then(response => {
            if (!response.ok) throw new Error('Kunne ikke hente JSON-filen: ' + jsonFile);
            return response.json();
        })
        .then(data => {
            // Re-søg elementerne her: Swup kan have udskiftet containeren
            // mellem kaldet og svaret, så en tidligere reference kan være død.
            const canvas = document.getElementById('priceChart');
            if (!canvas) return;                        // siden er navigeret væk

            setText('page-title', `Elpriser — D. ${data.date}`);
            setText('page-meta',
                `Enhed: ${data.unit} | Data genereret: ` +
                new Date(data.generated_utc).toLocaleString('da-DK'));

            priceChart = new Chart(canvas.getContext('2d'), buildChartConfig(canvas, data));
        })
        .catch(error => {
            const canvas = document.getElementById('priceChart');
            if (canvas) setChartMessage(canvas, MISSING_DATA_MESSAGE);
            setText('page-meta', 'Fejl: ' + error.message);
            console.error(error);
        })
        .finally(() => { isLoading = false; });
}

/** Bind init til sideindlæsning og til Swups navigationsevents. */
export function registerPriceChart() {
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initPriceChart);
    } else {
        initPriceChart();
    }

    // Ældre Swup: prefixede document-events. Nyere Swup fyrer på swup-instansen —
    // hvis din version bruger den, så tilføj dem dér i stedet.
    document.addEventListener('swup:contentReplaced', initPriceChart);
    document.addEventListener('swup:pageView', initPriceChart);
}

// Kør automatisk, når modulet indlæses på siden
registerPriceChart();
