"use strict";
/* Talks to Siphon on the loopback. Every call goes through here rather than
   from the popup, because the extension holds the host permission and so is
   not subject to CORS; the popup only asks this script for things. */

const ext = globalThis.browser || globalThis.chrome;
const FIRST_PORT = 8790, PORT_COUNT = 20;
const FR = (ext.i18n?.getUILanguage?.() || navigator.language || "en").toLowerCase().startsWith("fr");

const T = FR ? {
  page: "Télécharger cette page avec Siphon",
  link: "Télécharger ce lien avec Siphon",
  media: "Télécharger ce média avec Siphon",
  started: "Ajouté aux téléchargements",
  offline: "Siphon n’est pas lancé",
  offlineBody: "Lance Siphon, puis réessaie.",
  failed: "Échec de l’envoi",
} : {
  page: "Download this page with Siphon",
  link: "Download this link with Siphon",
  media: "Download this media with Siphon",
  started: "Added to downloads",
  offline: "Siphon is not running",
  offlineBody: "Start Siphon, then try again.",
  failed: "Could not send to Siphon",
};

/* Siphon takes the first free port from 8790, so its address is not known in
   advance. The last one that answered is tried first; the rest are probed at
   once, since twenty loopback connections cost nothing. */
let base = null;

async function ping(url, ms) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), ms);
  try {
    const r = await fetch(`${url}/api/ping`, { signal: ctl.signal, cache: "no-store" });
    return r.ok && (await r.json()).app === "Siphon";
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

async function findSiphon() {
  if (base && await ping(base, 900)) return base;
  const ports = Array.from({ length: PORT_COUNT }, (_, i) => FIRST_PORT + i);
  const hits = await Promise.all(ports.map(async p => (await ping(`http://127.0.0.1:${p}`, 700)) ? p : 0));
  const port = hits.find(Boolean);
  base = port ? `http://127.0.0.1:${port}` : null;
  return base;
}

class Offline extends Error {
  constructor() { super("offline"); this.code = "offline"; }
}

async function api(path, body) {
  const url = await findSiphon();
  if (!url) throw new Offline();
  const opts = body === undefined ? { cache: "no-store" } : {
    method: "POST", cache: "no-store", body: JSON.stringify(body),
    headers: { "Content-Type": "application/json", "X-Siphon": "1" }
  };
  let res;
  try {
    res = await fetch(url + path, opts);
  } catch {
    base = null;                       // it moved or died; next call rescans
    throw new Offline();
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || "server"), { code: data.error || "server" });
  return data;
}

/* ── badge ── */
function setBadge(text, colour) {
  try {
    ext.action.setBadgeText({ text });
    if (colour) ext.action.setBadgeBackgroundColor({ color: colour });
  } catch {}
}

let watchTimer = 0;
async function watchJobs() {
  clearTimeout(watchTimer);
  try {
    const d = await api("/api/jobs");
    const busy = (d.jobs || []).filter(j => j.status === "running" || j.status === "queued").length;
    setBadge(busy ? String(busy) : "", "#F4B740");
    // only while something is running: polling forever would also keep Siphon
    // awake forever, which is the user's choice to make, not ours
    if (busy) watchTimer = setTimeout(watchJobs, 1500);
  } catch {
    setBadge("", "");
  }
}

function notify(title, message) {
  try {
    ext.notifications?.create({ type: "basic", iconUrl: ext.runtime.getURL("icon.svg"), title, message });
  } catch {}
}

async function send(url, destination) {
  try {
    const d = await api("/api/download", destination ? { url, destination } : { url });
    setBadge("1", "#F4B740");
    watchJobs();
    return d.job;
  } catch (e) {
    if (e.code === "offline") notify(T.offline, T.offlineBody);
    else notify(T.failed, String(e.code || e.message || ""));
    throw e;
  }
}

/* ── context menus ── */
ext.runtime.onInstalled.addListener(() => {
  try { ext.contextMenus.removeAll(); } catch {}
  ext.contextMenus.create({ id: "siphon-link", title: T.link, contexts: ["link"] });
  ext.contextMenus.create({ id: "siphon-media", title: T.media, contexts: ["video", "audio"] });
  ext.contextMenus.create({ id: "siphon-page", title: T.page, contexts: ["page"] });
});

ext.contextMenus.onClicked.addListener((info, tab) => {
  const url = info.menuItemId === "siphon-link" ? info.linkUrl
            : info.menuItemId === "siphon-media" ? (info.srcUrl || info.pageUrl)
            : (info.pageUrl || tab?.url);
  // a blob: or data: src is the player's internal stream, not something yt-dlp
  // can fetch; the page address is what it actually knows how to resolve
  const usable = url && /^https?:\/\//i.test(url) ? url : (info.pageUrl || tab?.url);
  if (usable) send(usable).catch(() => {});
});

/* ── popup bridge ── */
ext.runtime.onMessage.addListener(msg => {
  if (!msg || typeof msg !== "object") return;
  if (msg.type === "state") {
    return api("/api/state").then(
      d => ({ ok: true, state: d }),
      e => ({ ok: false, code: e.code || "server" })
    );
  }
  if (msg.type === "download") {
    return send(msg.url, msg.destination).then(
      job => ({ ok: true, job }),
      e => ({ ok: false, code: e.code || "server" })
    );
  }
  if (msg.type === "jobs") {
    return api("/api/jobs").then(
      d => ({ ok: true, jobs: d.jobs || [] }),
      e => ({ ok: false, code: e.code || "server" })
    );
  }
  if (msg.type === "open") {
    return findSiphon().then(url => {
      if (url) ext.tabs.create({ url: url + "/" });
      return { ok: !!url };
    });
  }
});
