let swup;

// -------------------------
// Language and partials
// -------------------------


async function loadPartials() {
    try {
        const language =localStorage.getItem("language") || "en";
        console.log("Language:", language);
        const headerResponse = await fetch(`partials/${language}/header.html`);
        console.log("Header status:", headerResponse.status);
        const headerHtml = await headerResponse.text();
        console.log("Header length:", headerHtml.length);
        document.getElementById("header-container").innerHTML = headerHtml;
        const footerResponse = await fetch(`partials/${language}/footer.html`);
        console.log("Footer status:", footerResponse.status);
        const footerHtml = await footerResponse.text();
        console.log("Footer length:", footerHtml.length);
        document.getElementById("footer-container").innerHTML = footerHtml;
        console.log("Partials loaded");
    } catch(error) {
        console.error(error);
    }
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
        swup = new swup();
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
