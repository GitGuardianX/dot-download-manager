// DOT browser integration: background service worker (Manifest V3).
//
// Safety contract, checked by tests/test_extension.py and tools/test_extension_worker.mjs:
//  * Nothing runs at browser start-up: no timers, alarms, polling, page scans or requests.
//    Code runs only when the browser reports a new download, a media response, a menu
//    click, or a message from the popup or a page button.
//  * The browser never starts a program: there is no native messaging. DOT must already
//    be running; while it is not, every download simply stays in the browser.
//  * Only brand-new downloads are considered: onDeterminingFilename never fires for
//    restored history, and items are also checked for state and age.
//  * Hand-offs run one at a time, at most HANDOFFS_PER_MINUTE a minute, with at most
//    WAITING_LIMIT queued. Anything beyond that stays in the browser.
//  * A browser download is never held for longer than HOLD_MS.
"use strict";

const CLIENT = "dot-extension/" + chrome.runtime.getManifest().version;  // DOT notices an outdated extension.
const HOLD_MS = 12000;
const HANDOFF_TIMEOUT_MS = 7000;   // DOT checks the file (at most 4 s) before it accepts.
const HELLO_TIMEOUT_MS = 1200;
const HELLO_CACHE_MS = 3000;
const HANDOFFS_PER_MINUTE = 8;
const WAITING_LIMIT = 2;
const MAX_AGE_MS = 30000;
const MEDIA_PER_TAB = 12;
const DEFAULTS = Object.freeze({capture: true, buttons: true, port: 17643});
const CAPTURE_TYPES = new Set(("3gp 7z aac ace aif aiff apk arj asf avi bin bz2 cab deb dmg doc docx epub exe " +
  "flac flv gz gzip img iso jar lz lzh m4a m4b m4v mkv mov mp3 mp4 mpa mpe mpeg mpg msi msix msixbundle msu " +
  "ogg ogv opus pdf pkg ppt pptx qt rar rm rmvb rpm sit sitx tar tbz tbz2 tgz tif tiff txz wav webm wma wmv " +
  "xls xlsx xz z zip zst").split(" "));
const CAPTURE_MIME = /^(video|audio)\/|^application\/(zip|x-zip-compressed|x-7z-compressed|x-rar-compressed|vnd\.rar|x-tar|gzip|x-gzip|x-bzip2|x-xz|zstd|pdf|octet-stream|x-msdownload|x-msi|vnd\.android\.package-archive|x-iso9660-image|x-apple-diskimage)$/i;
// Kept in the browser even when served as generic binary: small, browser-friendly files.
const BROWSER_TYPES = new Set("txt csv tsv json xml html htm ics vcf png jpg jpeg gif webp svg avif css js md log".split(" "));
// Matched against the address's path only: Reddit's MP4 files carry "?m=DASHPlaylist.mpd".
const MANIFEST_PATH = /\.(m3u8|mpd)$/i;
const MANIFEST_MIME = /mpegurl|dash\+xml/i;
const SEGMENT_PATH = /\.(ts|m4s|aac|vtt|webvtt|key)$/i;
const pathOf = url => { try { return new URL(url).pathname; } catch { return ""; } };

const httpUrl = url => typeof url === "string" && url.length <= 8192 && /^https?:\/\//i.test(url);
const basename = path => String(path || "").split(/[\\/]/).pop().slice(0, 255);
const extensionOf = name => (/\.([a-z0-9]{1,12})$/i.exec(name) || [])[1]?.toLowerCase() || "";
const pageKey = url => { try { const u = new URL(url); return u.origin + u.pathname; } catch { return ""; } };
const originOf = url => { try { return new URL(url).origin; } catch { return ""; } };

// ---- settings ---------------------------------------------------------------------------
let cached = null;
async function settings() {
  if (!cached) cached = {...DEFAULTS, ...(await chrome.storage.local.get({...DEFAULTS}))};
  return cached;
}
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (cached) for (const key of Object.keys(DEFAULTS)) if (key in changes) cached[key] = changes[key].newValue ?? DEFAULTS[key];
  if ("port" in changes) hello.at = 0;
});

async function setStatus(text, problem = false) {
  try { await chrome.storage.session.set({status: {text: String(text).slice(0, 300), problem, at: Date.now()}}); } catch { }
}

// ---- talking to DOT (one-way, loopback only) ---------------------------------------------
async function dot(path, body, timeoutMs) {
  const port = Number((await settings()).port);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error("Set a valid DOT port in the extension popup.");
  const response = await fetch(`http://127.0.0.1:${port}${path}`, {
    method: "POST", credentials: "omit", cache: "no-store", redirect: "error",
    headers: {"Content-Type": "application/json", "X-DOT-Client": CLIENT},
    body: JSON.stringify(body), signal: AbortSignal.timeout(Math.max(1, timeoutMs))
  });
  let data = {};
  try { data = await response.json(); } catch { }
  if (!response.ok && response.status !== 409 && response.status !== 429) {
    throw new Error(data.error || `DOT replied with HTTP ${response.status}.`);
  }
  return data;  // 409 and 429 carry {accepted: false, reason}: the browser keeps the download.
}

// One shared, briefly cached check, however many downloads arrive at once.
const hello = {at: 0, ok: false, pending: null};
function dotRunning() {
  if (hello.pending) return hello.pending;
  if (Date.now() - hello.at < HELLO_CACHE_MS) return Promise.resolve(hello.ok);
  hello.pending = dot("/v1/hello", {}, HELLO_TIMEOUT_MS)
    .then(reply => reply.app === "DOT", () => false)
    .then(ok => { hello.at = Date.now(); hello.ok = ok; hello.pending = null; return ok; });
  return hello.pending;
}

// Budget is charged only when a hand-off actually runs, so skipped items never use it up.
const recentHandoffs = [];
function budgetLeft() {
  const now = Date.now();
  while (recentHandoffs.length && now - recentHandoffs[0] >= 60000) recentHandoffs.shift();
  return recentHandoffs.length < HANDOFFS_PER_MINUTE;
}

const BUSY = "Several downloads arrived at once; the extra ones stayed in the browser.";
const SPENT = "Many downloads went to DOT in the last minute; this one stayed in the browser.";
const skipped = reason => ({accepted: false, skipped: true, reason});

// Hand-offs run strictly one at a time. Resolves to a "skipped" reply without running when
// the queue is full, the minute's budget is spent, or the item's deadline is too close.
// The per-minute budget limits automatic captures; clicks are human-paced and exempt
// (they still queue one at a time, and DOT applies its own limits).
let tail = Promise.resolve();
let waiting = 0;
function oneAtATime(deadline, task, budgeted = true) {
  if (waiting >= WAITING_LIMIT) return Promise.resolve(skipped(BUSY));
  if (budgeted && !budgetLeft()) return Promise.resolve(skipped(SPENT));
  waiting++;
  const run = tail.then(() => {
    waiting--;
    const left = deadline - Date.now();
    if (left <= 2500) return skipped(BUSY);
    if (budgeted) {
      if (!budgetLeft()) return skipped(SPENT);
      recentHandoffs.push(Date.now());
    }
    return task(Math.min(HANDOFF_TIMEOUT_MS, left - 500));
  });
  tail = run.catch(() => null);
  return run;
}

async function cookieHeader(url) {
  try {
    const cookies = await chrome.cookies.getAll({url});
    return cookies.slice(0, 180).map(c => `${c.name}=${c.value}`).join("; ").slice(0, 16000);
  } catch { return ""; }
}

async function handOff(offer, timeoutMs) {
  return dot("/v1/downloads", {...offer, cookies: await cookieHeader(offer.url), userAgent: navigator.userAgent}, timeoutMs);
}

async function sendExplicit(offer) {
  const fail = async error => { await setStatus(error, true); return {ok: false, error}; };
  if (!httpUrl(offer.url)) return fail("That isn't a downloadable web address.");
  if (!(await dotRunning())) return fail("DOT isn't running. Open DOT, then try again.");
  const reply = await oneAtATime(Date.now() + HOLD_MS, timeoutMs => handOff(offer, timeoutMs), false)
    .catch(error => ({accepted: false, reason: error.message}));
  if (!reply?.accepted) {
    return fail((reply?.skipped ? "DOT is busy with other downloads. Try again in a moment." : reply?.reason)
                || "DOT declined this download.");
  }
  await setStatus("Sent to DOT.");
  return {ok: true};
}

// ---- browser downloads ---------------------------------------------------------------------
function eligible(item) {
  if (item.state !== "in_progress" || item.byExtensionId || item.incognito) return false;
  if (!httpUrl(item.finalUrl || item.url)) return false;
  const started = Date.parse(item.startTime);
  if (!Number.isFinite(started) || Math.abs(Date.now() - started) > MAX_AGE_MS) return false;
  const type = extensionOf(basename(item.filename));
  if (BROWSER_TYPES.has(type)) return false;
  return CAPTURE_TYPES.has(type) || CAPTURE_MIME.test(item.mime || "");
}

const seenDownloads = new Set();
function firstSight(id) {
  if (seenDownloads.has(id)) return false;
  seenDownloads.add(id);
  if (seenDownloads.size > 256) seenDownloads.delete(seenDownloads.values().next().value);
  return true;
}

chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  if (!eligible(item) || !firstSight(item.id)) { suggest(); return; }
  let released = false;
  const release = () => { if (!released) { released = true; try { suggest(); } catch { } } };
  const guard = setTimeout(release, HOLD_MS);
  intercept(item, Date.now() + HOLD_MS, () => released)
    .catch(() => false)
    .finally(() => { clearTimeout(guard); release(); });
  return true;  // suggest() is always called asynchronously within HOLD_MS.
});

async function intercept(item, deadline, isReleased) {
  if (!(await settings()).capture || !(await dotRunning())) return false;
  const url = item.finalUrl || item.url;
  const reply = await oneAtATime(deadline, timeoutMs => handOff({
    source: "download", url, referrer: httpUrl(item.referrer) ? item.referrer : "",
    filename: basename(item.filename), mime: String(item.mime || "").slice(0, 200),
    size: item.totalBytes > 0 ? item.totalBytes : null}, timeoutMs)).catch(() => null);
  if (!reply?.accepted || isReleased()) {
    if (reply && !reply.accepted) await setStatus(reply.reason || "DOT declined this download; the browser kept it.", true);
    return false;
  }
  try { await chrome.downloads.cancel(item.id); } catch { }
  try { await chrome.downloads.erase({id: item.id}); } catch { }
  await setStatus(`Sent to DOT: ${basename(item.filename) || url}`);
  return true;
}

// ---- context menu ----------------------------------------------------------------------------
chrome.runtime.onInstalled.addListener(() => {
  chrome.action.setBadgeBackgroundColor({color: "#5b4fd6"}).catch(() => {});
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({id: "dot-link", title: "Download with DOT", contexts: ["link"],
                                targetUrlPatterns: ["http://*/*", "https://*/*"]});
    chrome.contextMenus.create({id: "dot-media", title: "Download this media with DOT", contexts: ["video", "audio"]});
  });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  const referrer = httpUrl(info.pageUrl) ? info.pageUrl : "";
  let job = null;
  if (info.menuItemId === "dot-link") job = sendExplicit({source: "link", url: info.linkUrl, referrer});
  else if (info.menuItemId === "dot-media" && httpUrl(info.srcUrl)) job = sendExplicit({source: "media", url: info.srcUrl, referrer});
  else if (info.menuItemId === "dot-media" && tab) {
    const page = httpUrl(info.frameUrl) ? info.frameUrl : referrer;
    job = currentMedia(tab.id, tab.url).then(items => items.length
      ? sendExplicit({source: "media", url: items[0].url, kind: items[0].kind, mime: items[0].mime, referrer})
      : sendExplicit({source: "page", url: page, kind: "page", referrer}));
  }
  job?.then(result => result.ok || setStatus(result.error, true));
});

// ---- media detection: one cheap check per media/XHR response, nothing more -------------------
const mediaLoads = new Map();  // tabId -> Promise<{page, items}>
function tabMedia(tabId) {
  if (!mediaLoads.has(tabId)) {
    const key = `media:${tabId}`;
    mediaLoads.set(tabId, chrome.storage.session.get(key)
      .then(stored => stored[key] || {page: "", items: []}, () => ({page: "", items: []})));
  }
  return mediaLoads.get(tabId);
}

function forget(tabId) {
  mediaLoads.delete(tabId);
  chrome.storage.session.remove(`media:${tabId}`).catch(() => {});
  chrome.action.setBadgeText({tabId, text: ""}).catch(() => {});
}

async function remember(tabId, item) {
  const entry = await tabMedia(tabId);
  // Identity ignores the query: players re-request one playlist with changing parameters.
  const key = pageKey(item.url);
  if (entry.items.some(known => known.key === key)) return;  // Already known: no work, no write.
  // The check above and this insert run without an await in between, so concurrent
  // responses for the same media cannot both be added.
  const first = entry.items.length === 0;
  entry.items.unshift({...item, key, at: Date.now()});
  if (entry.items.length > MEDIA_PER_TAB) entry.items.length = MEDIA_PER_TAB;
  if (first && !entry.page) {
    try { entry.page = pageKey((await chrome.tabs.get(tabId)).url); } catch { }
  }
  chrome.storage.session.set({[`media:${tabId}`]: entry}).catch(() => {});
  chrome.action.setBadgeText({tabId, text: String(entry.items.length)}).catch(() => {});
}

// Streams first (oldest first, so an HLS master playlist precedes its variants), then files.
function ranked(items) {
  const streams = items.filter(i => i.kind === "stream").sort((a, b) => a.at - b.at);
  return [...streams, ...items.filter(i => i.kind !== "stream")];
}

// One menu entry per stream: an HLS master playlist's variant playlists sit in its folder.
function distinct(items) {
  const folders = new Set();
  return items.filter(item => {
    if (item.kind !== "stream") return true;
    const folder = item.key.slice(0, item.key.lastIndexOf("/") + 1);
    if (folders.has(folder)) return false;
    folders.add(folder);
    return true;
  });
}

// Media that belongs to the clicked video: items sharing a distinctive part of their address
// (an ID such as Reddit's "a1b2c3d4e5f6g") with the addresses its player names. When nothing
// matches, every item is offered, as before.
function forVideo(items, hints) {
  const ids = new Set();
  for (const hint of Array.isArray(hints) ? hints.slice(0, 8) : []) {
    try {
      for (const part of new URL(hint).pathname.split("/")) {
        if (part.length >= 6 && /\d/.test(part) && !/\.\w{2,4}$/.test(part)) ids.add(part);
      }
    } catch { }
  }
  if (!ids.size) return items;
  const own = items.filter(item => { try { return new URL(item.url).pathname.split("/").some(part => ids.has(part)); } catch { return false; } });
  return own.length ? own : items;
}

const PAGE_LABEL = "This page's video · via yt-dlp (any quality)";
const fileName = url => { try { return decodeURIComponent(new URL(url).pathname.split("/").pop()); } catch { return ""; } };

// The telling part of a stream's address, so two streams are told apart: "h264_aac_hq" in
// .../h264_aac_hq/1/manifest.m3u8. Generic playlist names and plain numbers are skipped.
function streamName(url) {
  let parts = [];
  try { parts = new URL(url).pathname.split("/").map(part => decodeURIComponent(part)); } catch { return ""; }
  const generic = /^(master|manifest|index|playlist|hlsplaylist|dashplaylist|prog_index|chunklist|media|stream)\b[^/]*\.(m3u8|mpd)$/i;
  for (let i = parts.length - 1; i >= 0; i--) {
    const part = parts[i];
    if (!part || /^\d+$/.test(part) || (i === parts.length - 1 && generic.test(part))) continue;
    return part.slice(0, 40);
  }
  return "";
}

// Quality named in an address part: "m2-res_1920p", "1080P_4000K", "DASH_720", "CMAF_96", or a
// Dailymotion-style rendition ("h264_aac_fhd" is 1080p).
const RENDITIONS = {ld: 240, sd: 380, hq: 480, hd: 720, fhd: 1080, qhd: 1440, uhd: 2160};
function heightIn(text) {
  const match = /(?:^|[^a-z0-9])(\d{3,4})p(?![a-z])/i.exec(text) || /(?:DASH|CMAF)_(\d{2,4})(?!\d)/i.exec(text);
  if (match) return Number(match[1]);
  const rendition = /(?:^|[_-])(ld|sd|hq|hd|fhd|qhd|uhd)$/i.exec(text);
  return rendition ? RENDITIONS[rendition[1].toLowerCase()] : 0;
}

// IDM-style menu entries, short and best first: "1920p HD · MP4 · 12.3 MiB"; a stream whose
// qualities DOT lists ("HLS stream · pick quality in DOT", usually the master playlist), then
// one rendition each ("1080p HD · HLS stream"); then audio. The quality also names the file.
function labelled(items) {
  const hd = height => `${height}p${height >= 720 ? " HD" : ""}`;
  return items.map(item => {
    const name = fileName(item.url);
    const size = item.size ? ` · ${(item.size / 1048576).toFixed(1)} MiB` : "";
    const ext = ((/\.(\w{2,4})$/.exec(name) || [])[1] || (item.mime || "").split("/")[1] || "").toUpperCase().slice(0, 5);
    if (item.kind === "stream") {
      const type = /\.mpd$/i.test(name) ? "DASH" : "HLS";
      const height = heightIn(streamName(item.url)) || heightIn(name.replace(/\.\w+$/, ""));
      return height ? {...item, rank: 2, height, quality: `${height}p`, label: `${hd(height)} · ${type} stream`}
                    : {...item, rank: 1, height: 0, quality: "", label: `${type} stream · pick quality in DOT`};
    }
    if (item.kind === "audio" || /audio/i.test(name)) {
      return {...item, rank: 3, height: 0, quality: "audio", label: `Audio only${ext ? ` · ${ext}` : ""}${size}`};
    }
    const height = heightIn(name);
    const label = height ? `${hd(height)}${ext ? ` · ${ext}` : ""}${size}`
                         : `Video${ext ? ` · ${ext}` : ""}${size} · ${name.slice(0, 30) || "media"}`;
    return {...item, rank: 0, height, quality: height ? `${height}p` : "", label};
  }).sort((a, b) => a.rank - b.rank || b.height - a.height);
}

// Replay the request the page's player made: stream hosts often accept only requests with the
// player's site as referrer and, from another site, an Origin header. The video's own title
// (from the page), else the tab's, names the file.
async function playerContext(tab, url, title) {
  const context = {title: String(title || tab?.title || "").replace(/\s+/g, " ").trim().slice(0, 200)};
  if (!tab) return context;
  const known = (await currentMedia(tab.id, tab.url)).find(item => item.key === pageKey(url));
  const initiator = /^https?:\/\/[^/]+$/.test(known?.initiator || "") ? known.initiator : "";
  if (initiator && initiator !== originOf(tab.url)) context.referrer = initiator + "/";  // An embedded player.
  if (initiator && initiator !== originOf(url)) context.origin = initiator;
  return context;
}

async function currentMedia(tabId, tabUrl) {
  const entry = await tabMedia(tabId);
  if (entry.page && tabUrl && entry.page !== pageKey(tabUrl)) { forget(tabId); return []; }
  return ranked(entry.items);
}

chrome.webRequest.onHeadersReceived.addListener(details => {
  if (details.tabId < 0 || (details.statusCode !== 200 && details.statusCode !== 206)) return;
  if (details.url.length > 8192 || SEGMENT_PATH.test(pathOf(details.url))) return;
  let mime = "", length = "", range = "";
  for (const header of details.responseHeaders || []) {
    const name = header.name.toLowerCase();
    if (name === "content-type") mime = String(header.value || "").split(";")[0].trim().toLowerCase();
    else if (name === "content-length") length = header.value || "";
    else if (name === "content-range") range = header.value || "";
  }
  let kind = "";
  if (MANIFEST_MIME.test(mime) || MANIFEST_PATH.test(pathOf(details.url))) kind = "stream";
  else if (details.type === "media" && /^(video|audio)\//.test(mime) && mime !== "video/mp2t") kind = mime.startsWith("audio/") ? "audio" : "video";
  if (!kind) return;
  if (kind === "stream" && length.trim() === "0") return;  // An empty placeholder playlist: nothing to download.
  const size = Number((/\/(\d+)\s*$/.exec(range) || [])[1] || length) || null;
  // The initiator is the origin of the document whose player made the request (an embedded
  // player's own site, for example); DOT replays it so the stream host accepts DOT's requests.
  const initiator = typeof details.initiator === "string" ? details.initiator : "";
  remember(details.tabId, {url: details.url, kind, mime, size, frameId: details.frameId, initiator}).catch(() => {});
}, {urls: ["http://*/*", "https://*/*"], types: ["media", "xmlhttprequest"]}, ["responseHeaders"]);

chrome.tabs.onUpdated.addListener((tabId, change) => {
  if (change.url && mediaLoads.has(tabId)) {
    mediaLoads.get(tabId).then(entry => { if (entry.page && entry.page !== pageKey(change.url)) forget(tabId); });
  }
});
chrome.tabs.onRemoved.addListener(tabId => forget(tabId));

// The address a click came from. Single-page sites (YouTube) change the address without
// reloading, while sender.url keeps the address the frame was first loaded with, so the
// content script reports location.href. It is accepted only from the sender's own origin.
function currentPage(reported, sender, tab) {
  if (httpUrl(reported) && originOf(reported) === originOf(sender.url || tab?.url)) return reported;
  if (sender.frameId === 0 && httpUrl(tab?.url)) return tab.url;
  return httpUrl(sender.url) ? sender.url : (httpUrl(tab?.url) ? tab.url : "");
}

// ---- popup and page-button requests ------------------------------------------------------------
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id || !message || typeof message.type !== "string") return;
  respond(message, sender).then(reply, error => reply({ok: false, error: String(error?.message || error)}));
  return true;
});

async function respond(message, sender) {
  const tab = sender.tab;
  const referrer = httpUrl(message.referrer) ? message.referrer : (httpUrl(tab?.url) ? tab.url : "");
  switch (message.type) {
    case "status": {
      const [running, stored] = await Promise.all([dotRunning(), chrome.storage.session.get("status")]);
      return {ok: true, running, status: stored.status || null, settings: await settings()};
    }
    case "media": {
      const target = await chrome.tabs.get(message.tabId);
      return {ok: true, items: (await currentMedia(target.id, target.url)).map(({url, kind, mime, size}) => ({url, kind, mime, size}))};
    }
    case "send": {
      const kind = String(message.kind || "");
      const offer = {source: kind === "page" ? "page" : kind ? "media" : "link", url: message.url, kind,
                     mime: String(message.mime || ""), referrer, quality: String(message.quality || "").slice(0, 20)};
      return sendExplicit(kind && kind !== "page" ? {...offer, ...await playerContext(tab, message.url, message.title)} : offer);
    }
    case "video": {
      if (!tab) return {ok: false, error: "This request did not come from a tab."};
      if (httpUrl(message.src)) {
        return sendExplicit({source: "media", url: message.src, referrer, ...await playerContext(tab, message.src, message.title)});
      }
      // The page (or the embedded player's frame) can be read by yt-dlp in DOT: YouTube and
      // many other sites never expose a plain video address to the browser.
      const page = currentPage(message.page, sender, tab);
      const items = await currentMedia(tab.id, tab.url);
      const sameFrame = items.filter(item => item.frameId === sender.frameId);
      const choices = labelled(distinct(forVideo(sameFrame.length ? sameFrame : items, message.hints))).slice(0, 7)
        .map(({url, kind, mime, size, label, quality}) => ({url, kind, mime, size, label, quality}));
      if (!choices.length) {
        return page ? sendExplicit({source: "page", url: page, kind: "page", referrer})
                    : {ok: false, error: "This player hasn't exposed a downloadable video."};
      }
      if (page) choices.push({url: page, kind: "page", mime: "", size: null, label: PAGE_LABEL, quality: ""});
      return {ok: true, choices};
    }
  }
  return {ok: false, error: "Unknown request."};
}
