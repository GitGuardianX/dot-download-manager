// Offline behavioral tests for browser-extension/worker.js.
// The real worker source runs in a Node VM with mocked extension APIs and a virtual clock,
// so start-up, download bursts, an unreachable or hung DOT, and media floods can be
// simulated without a browser. Exits non-zero on the first failed assertion.
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";

const SOURCE = readFileSync(new URL("../browser-extension/worker.js", import.meta.url), "utf8");
const START = Date.parse("2026-11-26T10:00:00Z");

function world({dot = "up", probeMs = 300, accept = true} = {}) {
  let now = START, timerId = 0;
  const timers = new Map();
  const listeners = {};
  const stats = {fetch: [], handoffBodies: [], cancels: [], erases: [], sessionWrites: 0, localWrites: 0,
                 tabGets: 0, active: 0, maxActive: 0};
  const local = {}, session = {};
  const event = name => ({addListener: fn => (listeners[name] ||= []).push(fn), removeListener() {}, hasListener: () => true});

  const setTimeoutV = (fn, ms = 0) => { const id = ++timerId; timers.set(id, {at: now + Math.max(0, ms), fn}); return id; };
  const clearTimeoutV = id => timers.delete(id);
  const delay = (ms, signal) => new Promise((resolve, reject) => {
    const id = setTimeoutV(resolve, ms);
    signal?.addEventListener("abort", () => { clearTimeoutV(id); reject(signal.reason); }, {once: true});
  });
  class VirtualAbortSignal {
    static timeout(ms) {
      const controller = new AbortController();
      setTimeoutV(() => controller.abort(new DOMException("The operation timed out.", "TimeoutError")), ms);
      return controller.signal;
    }
  }
  class VirtualDate extends Date { static now() { return now; } }
  const reply = (status, body) => ({ok: status >= 200 && status < 300, status, json: async () => body});

  async function fetchMock(url, init) {
    const path = new URL(url).pathname;
    stats.fetch.push({path, at: now});
    assert.equal(new URL(url).hostname, "127.0.0.1", "only loopback may be contacted");
    assert.equal(init.method, "POST");
    assert.equal(init.headers["X-DOT-Client"], "dot-extension/0.4.2", "the extension reports its version");
    if (dot === "down") throw new TypeError("Failed to fetch");
    if (dot === "hang") return delay(10 ** 9, init.signal);
    if (path === "/v1/hello") return reply(200, {app: "DOT"});
    if (path === "/v1/downloads") {
      stats.handoffBodies.push(JSON.parse(init.body));
      stats.active++;
      stats.maxActive = Math.max(stats.maxActive, stats.active);
      try { await delay(probeMs, init.signal); } finally { stats.active--; }
      return accept ? reply(202, {accepted: true, id: "offer"}) : reply(409, {accepted: false, reason: "refused for test"});
    }
    return reply(404, {error: "unknown"});
  }

  const forbidden = name => () => { throw new Error(`${name} must never be used`); };
  const chrome = {
    runtime: {id: "dot-test", onInstalled: event("onInstalled"), onMessage: event("onMessage"),
              getManifest: () => ({version: "0.4.2"}),
              sendNativeMessage: forbidden("sendNativeMessage"), connectNative: forbidden("connectNative")},
    storage: {
      local: {get: async defaults => ({...defaults, ...local}), set: async value => { stats.localWrites++; Object.assign(local, value); }},
      session: {
        get: async key => (typeof key === "string" ? (key in session ? {[key]: structuredClone(session[key])} : {}) : structuredClone(session)),
        set: async value => { stats.sessionWrites++; Object.assign(session, structuredClone(value)); },
        remove: async key => { delete session[key]; }},
      onChanged: event("storage.onChanged")},
    downloads: {onDeterminingFilename: event("onDeterminingFilename"),
                cancel: async id => { stats.cancels.push(id); }, erase: async query => { stats.erases.push(query.id); }},
    cookies: {getAll: async () => [{name: "sid", value: "s3cret"}]},
    contextMenus: {removeAll: callback => callback?.(), create() {}, onClicked: event("contextMenus.onClicked")},
    action: {setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {}},
    tabs: {get: async id => { stats.tabGets++; return {id, url: "https://video.example/watch?v=1"}; },
           onUpdated: event("tabs.onUpdated"), onRemoved: event("tabs.onRemoved")},
    webRequest: {onHeadersReceived: event("webRequest.onHeadersReceived")},
  };
  const context = vm.createContext({chrome, fetch: fetchMock, setTimeout: setTimeoutV, clearTimeout: clearTimeoutV,
    AbortSignal: VirtualAbortSignal, Date: VirtualDate, navigator: {userAgent: "TestBrowser/1.0"}, URL, DOMException,
    structuredClone, console});
  vm.runInContext(SOURCE, context, {filename: "worker.js"});

  const settle = async () => { for (let i = 0; i < 6; i++) await new Promise(resolve => setImmediate(resolve)); };
  async function advance(ms) {
    const end = now + ms;
    for (;;) {
      await settle();
      const next = [...timers.entries()].sort((a, b) => a[1].at - b[1].at)[0];
      if (!next || next[1].at > end) break;
      timers.delete(next[0]);
      now = next[1].at;
      next[1].fn();
    }
    now = end;
    await settle();
  }
  let downloadId = 0;
  const item = overrides => ({id: ++downloadId, url: "https://files.example/a.zip", finalUrl: "https://files.example/a.zip",
    filename: "C:\\Users\\me\\Downloads\\a.zip", mime: "application/zip", state: "in_progress", incognito: false,
    startTime: new Date(now).toISOString(), totalBytes: 4096, referrer: "https://files.example/", ...overrides});
  function download(overrides = {}) {
    const record = {suggestedAt: null, item: item(overrides)};
    const results = listeners.onDeterminingFilename.map(fn => fn(record.item, () => {
      assert.equal(record.suggestedAt, null, "suggest() must be called exactly once");
      record.suggestedAt = now;
    }));
    record.async = results[0] === true;
    return record;
  }
  const media = (url, mime, type = "xmlhttprequest", tabId = 7, initiator = "https://video.example") =>
    listeners["webRequest.onHeadersReceived"].forEach(fn => fn({tabId, frameId: 0, type, url, initiator, statusCode: 200,
                                                               responseHeaders: [{name: "Content-Type", value: mime}]}));
  const message = (body, sender = {id: "dot-test", tab: {id: 7, url: "https://video.example/watch?v=1"}, frameId: 0}) =>
    new Promise(resolve => listeners.onMessage.forEach(fn => fn(body, sender, resolve)));
  return {stats, timers, listeners, advance, download, media, message, now: () => now, local};
}

const results = {};
async function check(name, run) {
  await run();
  results[name] = "passed";
}

await check("start-up does nothing", async () => {
  const w = world();
  await w.advance(10 * 60 * 1000);
  assert.equal(w.stats.fetch.length, 0, "no requests at start-up");
  assert.equal(w.timers.size, 0, "no timers at start-up");
  assert.equal(w.stats.sessionWrites + w.stats.localWrites, 0, "no storage writes at start-up");
  assert.deepEqual(Object.keys(w.listeners).filter(name => name.includes("Created")), [], "downloads.onCreated is not used");
});

await check("history, old, foreign and excluded items are released at once", async () => {
  const w = world();
  const cases = [{state: "complete"}, {state: "interrupted"}, {startTime: new Date(START - 10 * 60 * 1000).toISOString()},
    {url: "blob:https://x/1", finalUrl: "blob:https://x/1"}, {byExtensionId: "other"}, {incognito: true},
    {filename: "C:\\d\\notes.txt", mime: "text/plain"}, {filename: "C:\\d\\page.html", mime: "text/html"},
    {filename: "C:\\d\\data.csv", mime: "application/octet-stream"}];
  const records = cases.map(c => w.download(c));
  for (const record of records) assert.equal(record.suggestedAt, START, `released synchronously: ${JSON.stringify(record.item)}`);
  await w.advance(30000);
  assert.equal(w.stats.fetch.length, 0);
});

await check("DOT not running: one shared check, downloads stay in the browser", async () => {
  const w = world({dot: "down"});
  const records = Array.from({length: 50}, () => w.download());
  await w.advance(2000);
  assert.equal(w.stats.fetch.filter(f => f.path === "/v1/hello").length, 1, "50 downloads share one check");
  assert.ok(records.every(r => r.suggestedAt !== null && r.suggestedAt - START < 1000), "all released promptly");
  assert.equal(w.stats.cancels.length, 0);
  assert.equal(w.stats.fetch.filter(f => f.path === "/v1/downloads").length, 0);
});

await check("accepted download is cancelled in the browser and carries the session", async () => {
  const w = world();
  const record = w.download();
  await w.advance(3000);
  assert.deepEqual(w.stats.cancels, [record.item.id]);
  assert.deepEqual(w.stats.erases, [record.item.id]);
  const body = w.stats.handoffBodies[0];
  assert.equal(body.url, "https://files.example/a.zip");
  assert.equal(body.cookies, "sid=s3cret");
  assert.equal(body.userAgent, "TestBrowser/1.0");
  assert.equal(body.source, "download");
});

await check("a burst of 100 downloads is bounded and never hangs", async () => {
  const w = world({probeMs: 1500});
  const records = Array.from({length: 100}, () => w.download());
  await w.advance(13000);
  const handoffs = w.stats.fetch.filter(f => f.path === "/v1/downloads").length;
  assert.ok(handoffs <= 3, `at most one running and two queued hand-offs, saw ${handoffs}`);
  assert.equal(w.stats.maxActive, 1, "hand-offs never overlap");
  assert.ok(records.every(r => r.suggestedAt !== null && r.suggestedAt - START <= 12000), "every download released within 12 s");
  assert.equal(w.stats.fetch.filter(f => f.path === "/v1/hello").length, 1);
  // Skipped downloads must not use up the minute's budget: an explicit click still works.
  const sending = w.message({type: "video", src: "https://cdn.example/clip.mp4"});
  await w.advance(3000);
  assert.equal((await sending).ok, true, "explicit send after a burst");  // Reply comes from the VM realm.
});

await check("per-minute budget", async () => {
  const w = world({probeMs: 10});
  for (let i = 0; i < 20; i++) { w.download(); await w.advance(1500); }
  assert.equal(w.stats.fetch.filter(f => f.path === "/v1/downloads").length, 8, "eight hand-offs per minute");
  // The budget limits automatic captures only; a click still goes through.
  const sending = w.message({type: "video", src: "https://cdn.example/clip.mp4"});
  await w.advance(2000);
  assert.equal((await sending).ok, true, "explicit send after the automatic budget is spent");
});

await check("DOT declines: the browser keeps the download", async () => {
  const w = world({accept: false});
  const record = w.download();
  await w.advance(3000);
  assert.notEqual(record.suggestedAt, null);
  assert.equal(w.stats.cancels.length, 0);
});

await check("hung DOT: download released by the 12 s guard, never cancelled later", async () => {
  const w = world({dot: "hang"});
  const record = w.download();
  await w.advance(60000);
  assert.ok(record.suggestedAt !== null && record.suggestedAt - START <= 12000, `released at ${record.suggestedAt - START} ms`);
  assert.equal(w.stats.cancels.length, 0);
  assert.equal(w.timers.size, 0, "no timers left behind");
});

await check("media detection writes only for new streams and stays bounded", async () => {
  const w = world();
  for (let i = 0; i < 1000; i++) {
    w.media(`https://cdn.example/seg-${i}.ts`, "video/mp2t");
    w.media(`https://cdn.example/chunk-${i}.m4s`, "video/iso.segment");
    w.media(`https://cdn.example/frag-${i}`, "video/mp4");               // XHR video bytes are segments
    w.media(`https://api.example/feed-${i}.json`, "application/json");
  }
  await w.advance(1000);
  assert.equal(w.stats.sessionWrites, 0, "segments and API traffic cause no writes");
  for (let i = 0; i < 500; i++) w.media("https://cdn.example/master.m3u8", "application/vnd.apple.mpegurl");
  await w.advance(1000);
  assert.equal(w.stats.sessionWrites, 1, "a repeated manifest is stored once");
  for (let i = 0; i < 30; i++) { w.media(`https://cdn.example/v${i}.m3u8`, "application/x-mpegurl"); await w.advance(5); }
  const list = await w.message({type: "media", tabId: 7}, {id: "dot-test"});
  assert.equal(list.items.length, 12, "at most twelve items per tab");
  assert.ok(list.items.every(item => item.kind === "stream"));
  assert.ok(list.items[0].url.endsWith("/v18.m3u8"), "newest twelve kept, oldest of those first");
});

await check("video button prefers the master playlist and sends it", async () => {
  const w = world();
  w.media("https://cdn.example/master.m3u8", "application/vnd.apple.mpegurl");
  await w.advance(100);
  w.media("https://cdn.example/720p.m3u8", "application/vnd.apple.mpegurl");
  await w.advance(100);
  const reply = await w.message({type: "video", src: "blob:https://video.example/abc"});
  assert.equal(reply.ok, true);
  assert.ok(Array.isArray(reply.choices) && reply.choices[0].url.endsWith("/master.m3u8"), "master playlist first");
  assert.equal(reply.choices.at(-1).kind, "page", "the page itself is always offered (yt-dlp)");
  const sending = w.message({type: "send", url: reply.choices[0].url, kind: "stream", mime: "application/vnd.apple.mpegurl"});
  await w.advance(1000);  // The hand-off waits on the virtual clock.
  const sent = await sending;
  assert.equal(sent.ok, true);
  assert.equal(w.stats.handoffBodies.at(-1).source, "media");
  assert.equal(w.stats.handoffBodies.at(-1).referrer, "https://video.example/watch?v=1");
});

await check("streams carry the player's referrer, origin and the tab title; variants are one entry", async () => {
  const w = world();
  const sender = {id: "dot-test", frameId: 0, tab: {id: 7, url: "https://video.example/watch?v=1", title: "A clip - Video"}};
  // An embedded player from another site loads the stream.
  w.media("https://cdn.example/v/1080/master.m3u8", "application/vnd.apple.mpegurl", "xmlhttprequest", 7, "https://player.example");
  await w.advance(100);
  w.media("https://cdn.example/v/1080/index-v1-a1.m3u8", "application/vnd.apple.mpegurl", "xmlhttprequest", 7, "https://player.example");
  await w.advance(100);
  const reply = await w.message({type: "video", src: "blob:https://player.example/x"}, sender);
  assert.equal(reply.choices.map(c => c.kind).join(","), "stream,page", "the variant playlist is not listed again");
  assert.ok(reply.choices[0].url.endsWith("/master.m3u8"));
  let sending = w.message({type: "send", url: reply.choices[0].url, kind: "stream", mime: "application/vnd.apple.mpegurl"}, sender);
  await w.advance(1000);
  assert.equal((await sending).ok, true);
  let body = w.stats.handoffBodies.at(-1);
  assert.equal(body.referrer, "https://player.example/", "the embedded player's site is the referrer");
  assert.equal(body.origin, "https://player.example");
  assert.equal(body.title, "A clip - Video");
  // The site's own player with a CDN stream: the page stays the referrer, the Origin is added.
  const w2 = world();
  w2.media("https://cdn.example/hls/master.m3u8", "application/vnd.apple.mpegurl");
  await w2.advance(100);
  sending = w2.message({type: "send", url: "https://cdn.example/hls/master.m3u8", kind: "stream", mime: ""}, sender);
  await w2.advance(1000);
  await sending;
  body = w2.stats.handoffBodies.at(-1);
  assert.equal(body.referrer, "https://video.example/watch?v=1");
  assert.equal(body.origin, "https://video.example");
  // The yt-dlp page choice carries no player details.
  sending = w2.message({type: "send", url: "https://video.example/watch?v=1", kind: "page", mime: ""}, sender);
  await w2.advance(1000);
  await sending;
  body = w2.stats.handoffBodies.at(-1);
  assert.equal(body.source, "page");
  assert.equal(body.origin, undefined);
});

await check("empty placeholder playlists are not offered", async () => {
  const w = world();
  w.listeners["webRequest.onHeadersReceived"].forEach(fn => fn({tabId: 7, frameId: 0, type: "xmlhttprequest",
    url: "https://cdn.example/manifest/video/x.m3u8?error=1", initiator: "https://video.example", statusCode: 200,
    responseHeaders: [{name: "Content-Type", value: "application/vnd.apple.mpegurl"}, {name: "Content-Length", value: "0"}]}));
  await w.advance(100);
  assert.equal((await w.message({type: "media", tabId: 7}, {id: "dot-test"})).items.length, 0);
});

await check("a player with no detectable stream (YouTube) sends the page to DOT", async () => {
  const w = world();
  for (let i = 0; i < 50; i++) w.media(`https://rr1.googlevideo.example/videoplayback?seg=${i}`, "application/vnd.yt-ump");
  await w.advance(100);
  const sending = w.message({type: "video", src: "blob:https://www.youtube.com/xyz"});
  await w.advance(1000);
  assert.equal((await sending).ok, true);
  const body = w.stats.handoffBodies.at(-1);
  assert.equal(body.source, "page");
  assert.equal(body.url, "https://video.example/watch?v=1");
});

await check("single-page sites: the current address is sent, not the first-loaded one", async () => {
  const w = world();
  // Chrome keeps sender.url at the frame's first address (YouTube's home page) after pushState.
  const sender = {id: "dot-test", url: "https://video.example/", frameId: 0,
                  tab: {id: 7, url: "https://video.example/watch?v=1"}};
  let sending = w.message({type: "video", src: "blob:https://video.example/b", page: "https://video.example/watch?v=spa"}, sender);
  await w.advance(1000);
  assert.equal((await sending).ok, true);
  assert.equal(w.stats.handoffBodies.at(-1).url, "https://video.example/watch?v=spa", "reported current address");
  sending = w.message({type: "video", src: "blob:https://video.example/b", page: "https://evil.example/watch?v=x"}, sender);
  await w.advance(1000);
  await sending;
  assert.equal(w.stats.handoffBodies.at(-1).url, "https://video.example/watch?v=1", "another site's address is ignored");
});

console.log(JSON.stringify({status: "passed", checks: results}, null, 2));
