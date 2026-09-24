let swup;

// -------------------------
// Language and partials
// -------------------------



async function loadPartials() {
    const language =
    localStorage.getItem("language") || "en";
    const headerResponse =
    await fetch(`${language}/header.html`);
    document.getElementById("header-container").innerHTML =
    await headerResponse.text();
    const footerResponse =
    await fetch(`${language}/footer.html`);
    document.getElementById("footer-container").innerHTML =
    await footerResponse.text();
}

let languageLoading = false;




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
        console.log("1");
        await loadPartials();
        console.log("2");
        updateActiveNav();
        console.log("3");
        activateLanguageButtons();
        console.log("4");
        swup = new Swup();
        console.log("5");
        swup.on("contentReplaced", () => {updateActiveNav();});
        console.log("6");
        enableKeyboardNavigation();
    }
);

function activateLanguageButtons() {
    document
        .querySelectorAll("[data-language]")
        .forEach(button => {
            button.addEventListener("click", () => {
            const lang = button.dataset.language;
            const current = localStorage.getItem("language") || "en";
            if (lang === current) {return;}
            localStorage.setItem("language", lang);
            location.reload();
        });
    });
}
