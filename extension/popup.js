"use strict";
/* The panel never asks for a link: it already knows which tab you are on.
   One click picks the folder and the format at once, because that is what a
   Siphon destination is. */

const ext = globalThis.browser || globalThis.chrome;
const FR = (ext.i18n?.getUILanguage?.() || navigator.language || "en").toLowerCase().startsWith("fr");

const T = FR ? {
  openApp: "Ouvrir Siphon",
  sendTo: "Envoyer vers",
  offline: "Siphon n’est pas lancé",
  offlineBody: "Lance Siphon depuis le bureau, puis rouvre ce panneau.",
  noUrl: "Cette page n’est pas téléchargeable",
  noUrlBody: "Ouvre la page du morceau ou de la vidéo, puis réessaie.",
  failed: "L’envoi a échoué",
  added: "Ajouté",
  queued: "En attente", running: "Téléchargement", done: "Terminé",
  error: "Échec", canceled: "Annulé", splitting: "Découpage",
} : {
  openApp: "Open Siphon",
  sendTo: "Send to",
  offline: "Siphon is not running",
  offlineBody: "Start Siphon from the desktop, then open this panel again.",
  noUrl: "This page cannot be downloaded",
  noUrlBody: "Open the track or video page, then try again.",
  failed: "Could not send it",
  added: "Added",
  queued: "Waiting", running: "Downloading", done: "Done",
  error: "Failed", canceled: "Canceled", splitting: "Splitting",
};

const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const send = msg => ext.runtime.sendMessage(msg);

let tab = null, pollTimer = 0;

function showMsg(title, body, isError) {
  const box = $("#msg");
  box.hidden = false;
  box.className = `msg${isError ? " err" : ""}`;
  box.innerHTML = `<strong>${esc(title)}</strong>${esc(body || "")}`;
}
function hideMsg() { $("#msg").hidden = true; }

function badge(j) {
  if (j.mode === "audio") return j.format === "mp3" ? `MP3 ${j.quality}` : String(j.format).toUpperCase();
  return j.quality === "best" || !j.quality ? "MP4" : `MP4 ${j.quality}p`;
}

function renderJobs(jobs) {
  const live = jobs
    .filter(j => j.status !== "done" || Date.now() / 1000 - j.created < 90)
    .slice(0, 4);
  $("#jobs").innerHTML = live.map(j => {
    const label = T[j.stage] || T[j.status] || j.status;
    const pct = Math.round(j.percent || 0);
    const active = j.status === "running" || j.status === "queued";
    return `<div class="job ${esc(j.status)}">
      <div class="jt">${esc(j.title || j.url)}</div>
      <div class="js"><span>${esc(label)}</span>${active ? `<span>${pct}%</span>` : ""}<span>${esc(badge(j))}</span></div>
      ${active ? `<div class="bar"><i style="width:${pct}%"></i></div>` : ""}
    </div>`;
  }).join("");
  return live.some(j => j.status === "running" || j.status === "queued");
}

async function pollJobs() {
  clearTimeout(pollTimer);
  const r = await send({ type: "jobs" });
  if (!r?.ok) return;
  if (renderJobs(r.jobs)) pollTimer = setTimeout(pollJobs, 900);
}

async function start(destId, button) {
  if (!tab?.url) return;
  const all = [...document.querySelectorAll(".dest")];
  all.forEach(b => { b.disabled = true; });
  const original = button.innerHTML;
  button.innerHTML = `<span class="n">${esc(T.added)}</span>`;
  const r = await send({ type: "download", url: tab.url, destination: destId });
  if (r?.ok) {
    hideMsg();
    pollJobs();
    setTimeout(() => window.close(), 700);   // the badge carries on from here
  } else {
    button.innerHTML = original;
    all.forEach(b => { b.disabled = false; });
    showMsg(r?.code === "offline" ? T.offline : T.failed,
            r?.code === "offline" ? T.offlineBody : String(r?.code || ""), true);
  }
}

async function init() {
  $("#open").textContent = T.openApp;
  $("#open").addEventListener("click", async () => { await send({ type: "open" }); window.close(); });

  const tabs = await ext.tabs.query({ active: true, currentWindow: true });
  tab = tabs && tabs[0];
  const usable = tab?.url && /^https?:\/\//i.test(tab.url);
  if (tab) {
    $("#target").hidden = false;
    $("#ttitle").textContent = tab.title || tab.url || "";
    try { $("#thost").textContent = new URL(tab.url).hostname.replace(/^www\./, ""); } catch { $("#thost").textContent = ""; }
  }
  if (!usable) { showMsg(T.noUrl, T.noUrlBody); return; }

  const r = await send({ type: "state" });
  if (!r?.ok) { showMsg(T.offline, T.offlineBody, true); return; }

  const dests = r.state?.destinations?.items || [];
  const active = r.state?.destinations?.active;
  $("#destLabel").hidden = false;
  $("#destLabel").textContent = T.sendTo;
  $("#dests").innerHTML = dests.map(d =>
    `<button class="dest" type="button" data-id="${esc(d.id)}"${d.id === active ? ' data-active="1"' : ""}>
       <span class="n">${esc(d.name)}</span><span class="f">${esc(badge(d))}</span>
     </button>`).join("");
  document.querySelectorAll(".dest").forEach(b =>
    b.addEventListener("click", () => start(b.dataset.id, b)));

  renderJobs(r.state?.jobs || []);
  pollJobs();
}

init();
