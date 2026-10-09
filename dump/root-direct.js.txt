// /js/root-redirect.js — sender den bare rod videre til /en/ eller /de/.
// Ligger i egen fil, så Pinegrow ikke følger stien og leder efter en fil
// der ikke findes (frontend/en/en/index.html).
(function () {
    const saved = localStorage.getItem("lang");
    const lang = (saved === "de") ? "de" : "en";
    window.location.replace("/" + lang + "/index.html");
})();
