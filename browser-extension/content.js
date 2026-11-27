// DOT video button (content script, runs in every frame).
//
// Event-driven by design: nothing happens in a frame until one of its videos starts
// playing, or the pointer moves onto a player whose video sits in a shadow root. There is
// no MutationObserver, no timer loop and no page scanning. At most
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

  // Players that keep their <video> inside a shadow root (Reddit's <shreddit-player>) never
  // let "playing" reach the document. The pointer's own event path does cross into shadow
  // roots, so such a player is found when the pointer moves onto it: one short look along
  // that path, as IDM's button appears on hover. Nothing is scanned.
  document.addEventListener("pointerover", event => {
    if (!enabled) return;
    const target = playerUnder(event.composedPath());
    if (target && target !== video && !dismissed.has(target) && large(target)) show(target);
  }, {capture: true, passive: true});

  // A video inside a shadow root on the pointer's path, else a player element whose closed
  // shadow root hides its video (the button then points at the player itself). Videos in the
  // page itself are left to the "playing" listener above.
  function playerUnder(path) {
    for (let i = 0; i < path.length && i < 32; i++) {
      const element = path[i];
      if (element === host) return null;  // The pointer is on DOT's own button.
      if (!(element instanceof Element)) continue;
      if (element instanceof HTMLVideoElement) return element.getRootNode() instanceof ShadowRoot ? element : null;
      const inner = element.shadowRoot?.querySelector("video");
      if (inner) return inner;
      if (element.localName.includes("-") && element.localName.includes("player") && !element.querySelector("video")) {
        return element;
      }
    }
    return null;
  }

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

  // Labels come from the extension's worker ("1920p HD · MP4 · 12.3 MiB"), as in IDM's menu.
  function describe(choice) {
    return String(choice.label || (choice.kind === "page" ? "This page's video · via yt-dlp (any quality)" : "Video")).slice(0, 120);
  }

  // Words that name a player, not a video: "Dailymotion Video Player", "JW Player", "Play".
  const GENERIC_TITLE = /^(video|media|play|pause|embedded video|watch|loading)$|^(?:\S+\s){0,3}(?:video\s+|media\s+)?player$/i;
  function cleanTitle(text) {
    const value = String(text || "").replace(/\s+/g, " ").trim().slice(0, 150);
    return value.length >= 3 && !GENERIC_TITLE.test(value) ? value : "";
  }

  // What identifies the clicked video, read from the video and up to 16 of its ancestors
  // (crossing shadow roots; nothing is scanned): a title, such as Reddit's post-title on the
  // video's post or an embedded player page's own title, which names the file; and the
  // addresses the player itself names, so DOT lists only this video's files.
  function videoContext(target) {
    const hints = [];
    let title = "";
    const hint = value => {
      if (typeof value === "string" && /^https?:\/\//i.test(value) && !hints.includes(value) && hints.length < 8) {
        hints.push(value.slice(0, 2048));
      }
    };
    let element = target;
    for (let depth = 0; element && depth < 16; depth++) {
      if (element instanceof Element) {
        hint(element.getAttribute("src"));
        hint(element.getAttribute("data-src"));
        if (element instanceof HTMLMediaElement) {
          hint(element.currentSrc);
          for (const child of element.children) if (child.localName === "source") hint(child.src);
        }
        title ||= cleanTitle(element.getAttribute("post-title") || element.getAttribute("data-title") ||
                             element.getAttribute("aria-label") || element.getAttribute("title"));
      }
      element = element instanceof ShadowRoot ? element.host : element.parentNode;
    }
    if (!title && window !== window.top) title = cleanTitle(document.title);
    return {title, hints};
  }

  function offerChoices(choices, context) {
    menu.replaceChildren(...choices.slice(0, 8).map(choice => {
      const button = node("button", "", describe(choice));
      button.type = "button";
      button.title = choice.url;
      button.addEventListener("click", async event => {
        if (!event.isTrusted) return;
        menu.hidden = true;
        let reply = null;
        try {
          reply = await chrome.runtime.sendMessage({type: "send", url: choice.url, kind: choice.kind, mime: choice.mime,
                                                    title: context.title, quality: choice.quality || ""});
        } catch { }
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
      const context = videoContext(video);
      const reply = await chrome.runtime.sendMessage({type: "video", src: video.currentSrc || video.src || "", page: location.href,
                                                      title: context.title, hints: context.hints});
      if (reply?.choices?.length) {
        mainButton.textContent = LABEL;
        offerChoices(reply.choices, context);
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
