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

    // Language comes from the URL prefix: "/en/..." or "/de/..."
    const lang = currentLang();
    console.log(lang);
    const [headerResponse, footerResponse] = await Promise.all([
        fetch(`/header.html`),
        fetch(`/footer.html`)
    ]);

    document.getElementById("header-container").innerHTML = await headerResponse.text();
    document.getElementById("footer-container").innerHTML = await footerResponse.text();

        // Buttons exist in the DOM only now, so their active state is set here.
    document.querySelectorAll(".language-switcher [data-language]").forEach(btn => {
        btn.setAttribute("aria-current", btn.dataset.language === lang ? "true" : "false");
    });
    // ...and their click handlers have to be (re)bound here too.
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
        "services.html"
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

    swup = new Swup();

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


