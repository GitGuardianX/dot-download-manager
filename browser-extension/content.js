// DOT video button (content script, runs in every frame).
//
// Event-driven by design: nothing happens in a frame until one of its videos starts
// playing. There is no MutationObserver, no timer loop and no page scanning. At most
// one button exists per frame, and its scroll/resize listeners are attached only while
// it is showing. Checked by tests/test_extension.py.
(() => {
  "use strict";
  if (globalThis.__dotVideoButton) return;
  globalThis.__dotVideoButton = true;

  const MIN_WIDTH = 240, MIN_HEIGHT = 135, LABEL = "⬇ Download with DOT";
  const CSS = `
    :host { all: initial; }
    .bar { display: flex; gap: 3px; font: 600 12px/1 "Segoe UI", system-ui, sans-serif; filter: drop-shadow(0 2px 8px #0008); }
    button { all: unset; box-sizing: border-box; cursor: pointer; background: #151e30; color: #f1eeff;
             border: 1px solid #a393ff; padding: 7px 11px; border-radius: 6px; white-space: nowrap; }
    button:hover { background: #2b2850; }
    button[disabled] { cursor: progress; opacity: .8; }
    .close { padding: 7px 8px; border-color: #53617a; color: #aab5c8; }
    .menu { margin-top: 4px; background: #151e30; border: 1px solid #53617a; border-radius: 6px; padding: 4px;
            max-width: 340px; font: 12px/1.35 "Segoe UI", system-ui, sans-serif; color: #e7edf8; }
    .menu[hidden] { display: none; }
    .menu button { display: block; width: 100%; border: 0; overflow: hidden; text-overflow: ellipsis; font-weight: 500; }
    .message { padding: 6px 8px; white-space: normal; }`;

  let enabled = true, watching = false, frame = 0, noteTimer = 0;
  let video = null, host = null, mainButton = null, menu = null, sizeWatch = null;
  const dismissed = new WeakSet();

  chrome.storage.local.get({buttons: true}).then(value => setEnabled(value.buttons !== false), () => {});
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes.buttons) setEnabled(changes.buttons.newValue !== false);
  });

  document.addEventListener("playing", event => {
    const target = event.target;
    if (enabled && target instanceof HTMLVideoElement && target !== video && !dismissed.has(target) && large(target)) show(target);
  }, {capture: true, passive: true});

  // A video may already be playing when this script arrives (it loads at document_idle).
  // This single look is bounded and happens once per frame.
  const videos = document.getElementsByTagName("video");
  for (let i = 0; i < videos.length && i < 16; i++) {
    if (!videos[i].paused && large(videos[i])) { show(videos[i]); break; }
  }

  function setEnabled(value) {
    enabled = value;
    if (!enabled) hide();
  }

  function large(element) {
    const box = element.getBoundingClientRect();
    return box.width >= MIN_WIDTH && box.height >= MIN_HEIGHT;
  }

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text) element.textContent = text;
    return element;
  }

  function build() {
    host = document.createElement("dot-video-button");
    host.style.cssText = "all:initial;position:fixed;left:0;top:0;z-index:2147483647;display:none;";
    const shadow = host.attachShadow({mode: "closed"});
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(CSS);
    shadow.adoptedStyleSheets = [sheet];
    const bar = node("div", "bar");
    mainButton = node("button", "go", LABEL);
    mainButton.type = "button";
    const close = node("button", "close", "✕");
    close.type = "button";
    close.setAttribute("aria-label", "Hide the DOT button for this video");
    menu = node("div", "menu");
    menu.hidden = true;
    bar.append(mainButton, close);
    shadow.append(bar, menu);
    // Only real user clicks count: a page cannot trigger DOT with synthetic events.
    mainButton.addEventListener("click", event => { if (event.isTrusted) onDownload(); });
    close.addEventListener("click", event => { if (event.isTrusted) { if (video) dismissed.add(video); hide(); } });
    // Keep clicks on the button from reaching the page's player (which may toggle playback).
    for (const type of ["pointerdown", "mousedown", "mouseup", "click", "dblclick"]) {
      host.addEventListener(type, event => event.stopPropagation());
    }
    sizeWatch = new ResizeObserver(schedule);
    document.documentElement.appendChild(host);
  }

  function show(target) {
    if (!host) build();
    if (video) sizeWatch.unobserve(video);
    video = target;
    sizeWatch.observe(video);
    menu.hidden = true;
    mainButton.textContent = LABEL;
    watch(true);
    schedule();
  }

  function hide() {
    if (video && sizeWatch) sizeWatch.unobserve(video);
    video = null;
    if (host) host.style.display = "none";
    watch(false);
  }

  function watch(on) {
    if (on === watching) return;
    watching = on;
    const method = on ? "addEventListener" : "removeEventListener";
    window[method]("scroll", schedule, {capture: true, passive: true});
    window[method]("resize", schedule, {passive: true});
    document[method]("fullscreenchange", schedule);
  }

  function schedule() {
    if (!frame) frame = requestAnimationFrame(place);
  }

  function place() {
    frame = 0;
    if (!video || !video.isConnected) { hide(); return; }
    // In fullscreen only the fullscreen element's subtree is drawn, so follow it there.
    const full = document.fullscreenElement;
    const parent = full && full !== video && full.contains(video) ? full : document.documentElement;
    if (host.parentNode !== parent) parent.appendChild(host);
    const box = video.getBoundingClientRect();
    const visible = box.width >= MIN_WIDTH && box.height >= MIN_HEIGHT && box.bottom > 0 && box.right > 0
      && box.top < innerHeight && box.left < innerWidth;
    if (!visible) { host.style.display = "none"; return; }
    host.style.display = "block";
    const width = host.offsetWidth || 200;
    const x = Math.max(4, Math.min(box.right - width - 10, innerWidth - width - 4));
    const y = Math.max(4, box.top + 10);
    host.style.transform = `translate(${Math.round(x)}px, ${Math.round(y)}px)`;
  }

  function say(text, problem = false) {
    clearTimeout(noteTimer);
    if (problem || text.length > 32) {
      menu.replaceChildren(node("div", "message", text));
      menu.hidden = false;
      mainButton.textContent = LABEL;
    } else {
      mainButton.textContent = text;
    }
    // One-shot reset after a click; never a repeating timer.
    noteTimer = setTimeout(() => {
      if (!mainButton) return;
      mainButton.textContent = LABEL;
      if (!menu.querySelector("button")) menu.hidden = true;
    }, 5000);
  }

  function describe(choice) {
    if (choice.kind === "page") return "This page's video · via yt-dlp (any quality)";
    let name = "";
    try { name = decodeURIComponent(new URL(choice.url).pathname.split("/").pop()); } catch { }
    const kind = choice.kind === "stream" ? "Stream" : choice.kind === "audio" ? "Audio" : "Video";
    const size = choice.size ? ` · ${(choice.size / 1048576).toFixed(1)} MiB` : "";
    return `${kind}${size} · ${name || "media"}`.slice(0, 120);
  }

  function offerChoices(choices) {
    menu.replaceChildren(...choices.slice(0, 8).map(choice => {
      const button = node("button", "", describe(choice));
      button.type = "button";
      button.title = choice.url;
      button.addEventListener("click", async event => {
        if (!event.isTrusted) return;
        menu.hidden = true;
        let reply = null;
        try { reply = await chrome.runtime.sendMessage({type: "send", url: choice.url, kind: choice.kind, mime: choice.mime}); } catch { }
        say(reply?.ok ? "Sent to DOT ✓" : (reply?.error || "Couldn't reach DOT."), !reply?.ok);
      });
      return button;
    }));
    menu.hidden = false;
  }

  async function onDownload() {
    if (!video || mainButton.disabled) return;
    mainButton.disabled = true;
    mainButton.textContent = "Sending to DOT…";
    try {
      // location.href is the current address; single-page sites (YouTube) change it without reloading.
      const reply = await chrome.runtime.sendMessage({type: "video", src: video.currentSrc || video.src || "", page: location.href});
      if (reply?.choices?.length) {
        mainButton.textContent = LABEL;
        offerChoices(reply.choices);
      } else {
        say(reply?.ok ? "Sent to DOT ✓" : (reply?.error || "Couldn't reach DOT."), !reply?.ok);
      }
    } catch {
      say("Couldn't reach DOT. Reload the page if the extension was just updated.", true);
    } finally {
      mainButton.disabled = false;
    }
  }
})();
