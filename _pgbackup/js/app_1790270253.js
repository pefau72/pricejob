let swup;

// -------------------------
// Language and partials
// -------------------------

async function loadPartials() {
    console.log(window.location.pathname);
    console.log(window.location.pathname.split("/").pop());
    // Language comes from the URL prefix: "/en/..." or "/de/..."
    const lang = window.location.pathname.split("/")[1] || "en";

    const [headerResponse, footerResponse] = await Promise.all([
        fetch("header.html"),
        fetch("footer.html")
    ]);

    document.getElementById("header-container").innerHTML = await headerResponse.text();
    document.getElementById("footer-container").innerHTML = await footerResponse.text();

    // Mark the active language now that the buttons exist in the DOM
    document.querySelectorAll(".language-switcher button").forEach(btn => {
        const active = btn.dataset.language === lang;
        btn.setAttribute("aria-current", active ? "true" : "false");
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
    const current = window.location.pathname .split("/") .pop() || "index.html";
    document .querySelectorAll(".nav-links a") .forEach(link => {
        link.classList.remove("active");
        if (link.getAttribute("href")
        === current
        ) {
        link.classList.add("active");
        }
    });
}

// -------------------------
// Startup
// -------------------------

document.addEventListener(
    "DOMContentLoaded",
    async () => {
        await loadPartials();
        updateActiveNav();
        activateLanguageButtons();
        swup = new Swup();
        swup.on("contentReplaced", () => {updateActiveNav();document.activeElement.blur();});
        enableKeyboardNavigation();
    }
);


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


