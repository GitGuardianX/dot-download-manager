// DOT popup. Runs only while the popup is open; it checks once and has no timers.
"use strict";
const $ = id => document.getElementById(id);
let tab = null;

function show(text, problem = false) {
  $("status").textContent = text || "";
  $("status").className = problem ? "problem" : "muted";
}

function describe(item) {
  let name = "";
  try { name = decodeURIComponent(new URL(item.url).pathname.split("/").pop()); } catch { }
  const kind = item.kind === "stream" ? "Stream" : item.kind === "audio" ? "Audio" : "Video";
  const size = item.size ? ` · ${(item.size / 1048576).toFixed(1)} MiB` : "";
  return `${kind}${size} · ${name || "media"}`;
}

async function send(message, button) {
  button.disabled = true;
  try {
    const reply = await chrome.runtime.sendMessage({...message, type: "send",
      referrer: /^https?:/i.test(tab?.url || "") ? tab.url : ""});
    show(reply?.ok ? "Sent to DOT. Choose where to save it in the DOT window." : (reply?.error || "Couldn't reach DOT."), !reply?.ok);
  } catch {
    show("Couldn't reach the extension. Try reopening this popup.", true);
  } finally {
    button.disabled = false;
  }
}

function renderMedia(items) {
  if (!items.length) return;
  $("media").replaceChildren(...items.map(item => {
    const row = document.createElement("div");
    row.className = "row media";
    const name = document.createElement("span");
    name.textContent = describe(item);
    name.title = item.url;
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Download";
    button.addEventListener("click", () => send({url: item.url, kind: item.kind, mime: item.mime}, button));
    row.append(name, button);
    return row;
  }));
}

async function init() {
  [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  const reply = await chrome.runtime.sendMessage({type: "status"});
  $("capture").checked = reply.settings.capture !== false;
  $("buttons").checked = reply.settings.buttons !== false;
  $("port").value = reply.settings.port;
  $("state").textContent = reply.running ? "Connected · downloads go to DOT" : "DOT isn't running";
  $("state").classList.add(reply.running ? "ok" : "off");
  $("offline").hidden = reply.running;
  if (reply.status && Date.now() - reply.status.at < 10 * 60 * 1000) show(reply.status.text, reply.status.problem);
  if (Number.isInteger(tab?.id) && tab.id >= 0) {
    const media = await chrome.runtime.sendMessage({type: "media", tabId: tab.id}).catch(() => null);
    renderMedia(media?.items || []);
  }
}

$("page").addEventListener("click", event => {
  if (!/^https?:\/\//i.test(tab?.url || "")) { show("Open a web page with a video first.", true); return; }
  send({url: tab.url, kind: "page"}, event.target);  // DOT reads the page with yt-dlp.
});
$("capture").addEventListener("change", event => chrome.storage.local.set({capture: event.target.checked}));
$("buttons").addEventListener("change", event => chrome.storage.local.set({buttons: event.target.checked}));
$("send").addEventListener("click", event => {
  const value = $("url").value.trim();
  if (!/^https?:\/\/\S+$/i.test(value)) { show("Enter a full http:// or https:// address.", true); return; }
  send({url: value}, event.target);
});
$("save").addEventListener("click", async () => {
  const port = Number($("port").value);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) { show("Use a port from 1024 to 65535.", true); return; }
  await chrome.storage.local.set({port});
  show("Saved. Reopen this popup to check the connection.");
});
init().catch(error => show(String(error?.message || error), true));
