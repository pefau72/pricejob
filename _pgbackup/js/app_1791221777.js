let swup;

// -------------------------
// Language and partials
// -------------------------

const LANGS = ["en", "de"];

// Language of the page currently loaded: "/de/services.html" -> "de".
// Falls back to the <html lang> attribute, then to "en".
function currentLang() {
    const seg = window.location.pathname.split("/")[1];
    if (LANGS.includes(seg)) return seg;
    return document.documentElement.lang || "en";
}

// Filename of the page currently loaded, never empty.
// "/de/" or "/de/services/" -> "index.html"
function currentPage() {
    const last = window.location.pathname.split("/").pop();
    return last && last.includes(".") ? last : "index.html";
}

// URL for the same page in the other language tree.
// "/de/services.html" + "en" -> "/en/services.html"
function languageUrl(lang) {
    return `/${lang}/${currentPage()}${window.location.search}${window.location.hash}`;
}

async function loadPartials() {
    const lang = currentLang();

    const urls = [`/${lang}/header.html`, `/${lang}/footer.html`];
    const responses = await Promise.all(urls.map(u => fetch(u)));

    const bad = urls.filter((u, i) => !responses[i].ok);
    if (bad.length) {
        console.error("Partials failed to load:", bad, responses.map(r => r.status));
    }

    const [headerHTML, footerHTML] = await Promise.all(responses.map(r => r.text()));
    document.getElementById("header-container").innerHTML = headerHTML;
    document.getElementById("footer-container").innerHTML = footerHTML;

    document.querySelectorAll(".language-switcher [data-language]").forEach(btn => {
        btn.setAttribute("aria-current", btn.dataset.language === lang ? "true" : "false");
    });

    activateLanguageButtons();
}
// -------------------------
// Language switching
// -------------------------

function activateLanguageButtons() {
    document.querySelectorAll("[data-language]").forEach(button => {
        // Injected partials can rebind on every swup navigation; guard instead
        // of stacking duplicate listeners.
        if (button.dataset.bound === "true") return;
        button.dataset.bound = "true";

        button.addEventListener("click", () => {
            const lang = button.dataset.language;
            if (lang === currentLang()) return;          // already on this language
            window.location.href = languageUrl(lang);
        });
    });
}

// -------------------------
// Keyboard navigation
// -------------------------

function enableKeyboardNavigation() {
    document.addEventListener("keydown", (e) => {
        const pages = [
        "index.html",
        "profile.html",
        "services.html",
        "publications.html"
        ];

        const current =
        window.location.pathname
        .split("/")
        .pop() || "index.html";

        const index = pages.indexOf(current);

        if (index === -1) return;

        if (e.key === "ArrowLeft") {
            const prev =
            (index - 1 + pages.length) % pages.length;
            swup.loadPage({url: pages[prev]});
        }

        if (e.key === "ArrowRight") {
            const next = (index + 1) % pages.length;
            swup.loadPage({url: pages[next]});
        }
    });
}

// -------------------------
// Active menu item
// -------------------------

function updateActiveNav() {
    const current = currentPage();
    document.querySelectorAll(".nav-links a").forEach(link => {
        link.classList.remove("active");
        // Compare on the filename so "/de/profile.html" matches "profile.html".
        const target = link.getAttribute("href").split("/").pop();
        if (target === current) link.classList.add("active");
    });
}

// -------------------------
// Startup
// -------------------------

document.addEventListener("DOMContentLoaded", async () => {
    await loadPartials();
    updateActiveNav();
    enableKeyboardNavigation();

    swup = new Swup({linkSelector:'a[href]:not([data-no-swup]):not([href$=".pdf"])'});

    swup.on("contentReplaced", async () => {
        // If the header lives inside the swup container it is swapped out,
        // so the language buttons must be re-fetched and re-bound.
        await loadPartials();
        updateActiveNav();
        document.activeElement.blur();
    });
});


function activateLanguageButtons() {
    document
    .querySelectorAll("[data-language]")
    .forEach(button => {
        button.addEventListener("click", () => {
            const lang = button.dataset.language;
            const page = window.location.pathname
            .split("/")
            .pop();
            window.location.href = `/${lang}/${page}`;
        });
    });
}

// *********************
// graph.js related stuff
// *********************

function getLatestJsonFilename() {
        const today = new Date();
        const yyyy = today.getFullYear();
        const mm = String(today.getMonth() + 1).padStart(2, '0');
        const dd = String(today.getDate()).padStart(2, '0');
        
        // Returnerer dynamisk f.eks. "prices-2026-09-29.json"
        return `prices-${yyyy}-${mm}-${dd}.json`;
    }


