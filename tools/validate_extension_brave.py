"""Validate DOT's browser extension in a real, resource-capped, headless Brave.

Part A, start-up cost: restore the same session of heavy pages (iframes, many <video>
elements, constant DOM churn, background XHR) with no extension, with the withdrawn 0.2
extension when a local copy exists (native messaging stripped so it cannot launch anything;
it is not published), and with the current extension. Skip Part A with --functional-only.
Part B, function: interception, fail-open, burst limits, restart with download history,
and the video button, against a real DOT bridge running in this process.

Safety: every browser runs in a temporary profile inside a Windows job with hard CPU,
memory, process and wall-clock limits (see tools/limited_brave.py). Fixtures are served
over HTTPS on 127.0.0.1 only, so an installed IDM cannot see or capture them.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import ssl
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotdm.bridge import BrowserBridge  # noqa: E402
from dotdm.engine import DownloadManager  # noqa: E402
from tools.limited_brave import LimitedBrave, https_server, wait_for  # noqa: E402

# DOT's pre-accept check must trust the throwaway fixture certificate (this process only).
ssl._create_default_https_context = ssl._create_unverified_context

NEW_EXTENSION = ROOT / "browser-extension"
OLD_EXTENSION = ROOT / "artifacts/browser-incident-20261126T092219Z/source"  # Optional, local only.
SAMPLE = (ROOT / "tests/fixtures/sample.mp4").read_bytes()  # 2-second synthetic clip.
ARCHIVE = b"PK\x05\x06" + b"\x00" * 18 + b"DOT validation archive " * 2400
EXTENSION_ORIGIN = "chrome-extension://hpdhbnmbbjmgnccfbgffanhcfdkhalpn"
HEAVY_TABS = 6
BENCH = dict(cpu_percent=20, commit_bytes=2 * 1024**3, process_limit=16, time_limit=110)
FUNCTIONAL = dict(cpu_percent=15, commit_bytes=1536 * 1024**2, process_limit=16, time_limit=110)


def heavy_page(n: int) -> str:
    rows = "".join(f'<div class="row"><span>Item {i}</span> <a href="#r{i}">open</a> <em>{i * 7}</em></div>' for i in range(600))
    videos = "".join('<video width="320" height="180" muted preload="none"></video>' for _ in range(20))
    frames = "".join(f'<iframe src="/frame/{n}/{i}" width="320" height="200"></iframe>' for i in range(3))
    return (f'<!doctype html><meta charset="utf-8"><title>Heavy {n}</title><div id="feed"></div>{frames}{videos}{rows}'
            "<script>let t=0;const feed=document.getElementById('feed');"
            "const tick=setInterval(()=>{const d=document.createElement('div');d.textContent='update '+(++t);"
            "feed.prepend(d);if(feed.children.length>40)feed.lastChild.remove();if(t>200)clearInterval(tick);},150);"
            "const poll=setInterval(()=>fetch('/api/feed?'+t).catch(()=>{}),1000);setTimeout(()=>clearInterval(poll),30000);"
            "</script>")


def frame_page() -> str:
    rows = "".join(f"<p>Frame row {i} <b>{i}</b></p>" for i in range(250))
    videos = "".join('<video width="160" height="90" muted preload="none"></video>' for _ in range(5))
    return ('<!doctype html><meta charset="utf-8"><div id="f"></div>' + videos + rows +
            "<script>let t=0;const f=document.getElementById('f');const k=setInterval(()=>{const e=document.createElement('i');"
            "e.textContent=++t;f.append(e);if(f.children.length>30)f.firstChild.remove();if(t>150)clearInterval(k);},200);</script>")


VIDEO_PAGE = ('<!doctype html><meta charset="utf-8"><title>Video</title><h1>Player</h1>'
              '<video id="v" src="/media/sample.mp4" width="640" height="360" autoplay muted loop playsinline></video>')
# Plays through a blob: address like YouTube's player: no plain video URL is ever exposed.
BLOB_VIDEO_PAGE = ('<!doctype html><meta charset="utf-8"><title>Blob video</title><h1>Streaming player</h1>'
                   '<video id="v" width="640" height="360" muted loop playsinline></video><script>'
                   "fetch('/media/sample.mp4').then(r => r.blob()).then(b => { const v = document.getElementById('v');"
                   " v.src = URL.createObjectURL(b); v.play();"
                   # Like YouTube: move to the video's address without reloading the page.
                   " history.pushState({}, '', '/watch?v=spa123'); });</script>")


# A news page embedding another site's player, whose own page title only names the player.
EMBED_TOP_PAGE = ('<!doctype html><meta charset="utf-8"><title>Real article title</title><h1>Article</h1>'
                  '<iframe src="/embedplayer" width="680" height="400" allow="autoplay" style="border:0"></iframe>')
EMBED_PLAYER_PAGE = ('<!doctype html><meta charset="utf-8"><title>Example Video Player</title><body style="margin:0">'
                     '<video src="/media/sample.mp4" width="640" height="360" autoplay muted loop playsinline></video>')


def shadow_player_page(mode: str) -> str:
    """A player that keeps its <video> in a shadow root under a controls overlay, like Reddit's
    <shreddit-player>: the video's "playing" event never reaches the document."""
    return ('<!doctype html><meta charset="utf-8"><title>Shadow player</title><h1>Shadow player</h1>'
            '<div post-title="Shadow test clip">'  # Like Reddit's post around its player: names the file.
            f'<{mode}-player id="p" style="display:block;width:640px;height:360px"></{mode}-player></div><script>'
            f"customElements.define('{mode}-player', class extends HTMLElement {{ constructor() {{ super();"
            f" this.attachShadow({{mode: '{mode}'}}).innerHTML = '<div style=\"position:relative;width:640px;height:360px\">"
            '<video src="/media/sample.mp4" width="640" height="360" autoplay muted loop playsinline></video>'
            '<div style="position:absolute;inset:0"></div></div>\'; } });</script>')


class Fixtures(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    files_served: list[str] = []

    def log_message(self, *_):
        pass

    def send(self, status, body: bytes, content_type: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?")[0]
        if m := re.fullmatch(r"/heavy/(\d+)", path):
            self.send(200, heavy_page(int(m.group(1))).encode(), "text/html; charset=utf-8")
        elif re.fullmatch(r"/frame/\d+/\d+", path):
            self.send(200, frame_page().encode(), "text/html; charset=utf-8")
        elif path == "/api/feed":
            self.send(200, b'{"items":[]}', "application/json")
        elif path == "/video":
            self.send(200, VIDEO_PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/blobvideo":
            self.send(200, BLOB_VIDEO_PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/embedtop":
            self.send(200, EMBED_TOP_PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/embedplayer":
            self.send(200, EMBED_PLAYER_PAGE.encode(), "text/html; charset=utf-8")
        elif m := re.fullmatch(r"/(open|closed)player", path):
            self.send(200, shadow_player_page(m.group(1)).encode(), "text/html; charset=utf-8")
        elif path == "/media/sample.mp4":
            self.ranged(SAMPLE, "video/mp4")
        elif m := re.fullmatch(r"/files/([\w-]+\.zip)", path):
            Fixtures.files_served.append(m.group(1))
            self.ranged(ARCHIVE, "application/zip", {"Content-Disposition": f'attachment; filename="{m.group(1)}"', "ETag": '"v1"'})
        else:
            self.send(404, b"not found", "text/plain")

    def ranged(self, data: bytes, content_type: str, extra: dict | None = None):
        extra = dict(extra or {}, **{"Accept-Ranges": "bytes"})
        match = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        if not match:
            self.send(200, data, content_type, extra)
            return
        start = int(match.group(1))
        end = min(int(match.group(2) or len(data) - 1), len(data) - 1)
        extra["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
        self.send(206, data[start:end + 1], content_type, extra)


def copy_extension(source: Path, target: Path, strip_native=False) -> Path:
    shutil.copytree(source, target)
    if strip_native:
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        manifest["permissions"] = [p for p in manifest["permissions"] if p != "nativeMessaging"]
        (target / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        shutil.copy(NEW_EXTENSION / "icon.png", target / "icon.png")
    return target


def benchmark(folder: Path, base: str, variant: str, extension: Path | None) -> dict:
    profile = folder / f"profile-{variant}"
    extra = [f"--load-extension={extension}"] if extension else []
    urls = [f"{base}/heavy/{i}" for i in range(HEAVY_TABS)]
    with LimitedBrave(profile, *extra, "about:blank", **BENCH) as first:
        for url in urls:
            first.call("Target.createTarget", {"url": url})
        wait_for(lambda: sum(t["type"] == "page" and t["title"].startswith("Heavy") for t in first.targets()) == HEAVY_TABS,
                 "heavy tabs to load", 40, 0.5)
        time.sleep(2)
    with LimitedBrave(profile, *extra, "--restore-last-session", **BENCH) as browser:
        wait_for(lambda: sum(t["type"] == "page" and t["url"].startswith(base + "/heavy/") for t in browser.targets()) >= HEAVY_TABS,
                 "session restore", 40, 0.5)
        restored_at = time.monotonic()
        time.sleep(30)
        snapshot = browser.job_snapshot()
        snapshot["seconds_measured"] = round(time.monotonic() - restored_at, 1)
        # Prove the extension was really active in the restored tabs.
        page = next(t for t in browser.targets() if t["type"] == "page" and t["url"].startswith(base + "/heavy/"))
        session = browser.attach(page["targetId"])
        snapshot["evidence"] = {
            "0.2_overlays_in_page": browser.evaluate(session, "document.querySelectorAll('.dot-download-overlay').length"),
            "0.3_buttons_in_page": browser.evaluate(session, "document.querySelectorAll('dot-video-button').length"),
            "extension_worker_running": any(t["type"] == "service_worker" and t["url"].startswith("chrome-extension://")
                                            for t in browser.targets())}
    snapshot.update(first_launch_clean=first.clean_exit, restart_clean=browser.clean_exit,
                    watchdog_triggered=first.timed_out or browser.timed_out)
    return snapshot


def next_offer(bridge: BrowserBridge, seconds: float):
    try:
        return wait_for(lambda: None if bridge.pending.empty() else bridge.pending.get_nowait(), "an offer", seconds, 0.1)
    except AssertionError:
        return None


def drain(bridge: BrowserBridge) -> list[dict]:
    offers = []
    while not bridge.pending.empty():
        offer = bridge.pending.get_nowait()
        bridge.done(offer["id"])
        offers.append(offer)
    return offers


def seed_download_folder(profile: Path, downloads: Path):
    """Point the new profile's downloads at a temp folder. The DevTools download override
    (Browser.setDownloadBehavior) bypasses extension hooks, so it cannot be used here."""
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Preferences").write_text(json.dumps({
        "download": {"default_directory": str(downloads), "prompt_for_download": False, "directory_upgrade": True},
        "savefile": {"default_directory": str(downloads)}}), encoding="utf-8")


def assert_downloads_stay_in(browser: LimitedBrave, downloads: Path):
    target, session = browser.open_page(f"{EXTENSION_ORIGIN}/popup.html")
    paths = browser.evaluate(session, "chrome.downloads.search({}).then(items => items.map(d => d.filename).filter(Boolean))")
    browser.call("Target.closeTarget", {"targetId": target})
    outside = [p for p in paths if Path(p).resolve().parent != downloads.resolve()]
    assert not outside, f"browser saved outside the temporary folder: {outside}"
    return len(paths)


def configure_extension(browser: LimitedBrave, port: int):
    target, session = browser.open_page(f"{EXTENSION_ORIGIN}/popup.html")
    browser.evaluate(session, f"chrome.storage.local.set({{port: {port}}}).then(() => true)")
    browser.call("Target.closeTarget", {"targetId": target})


def functional(folder: Path, base: str) -> dict:
    report = {}
    os.environ["DOT_YTDLP"] = str(ROOT / "tests" / "fake_ytdlp.py")  # The sandbox has no internet.
    profile, downloads = folder / "profile-functional", folder / "browser-downloads"
    downloads.mkdir()
    seed_download_folder(profile, downloads)
    extension = copy_extension(NEW_EXTENSION, folder / "ext-functional")
    manager = DownloadManager(folder / "dot-state", folder / "dot-files")
    bridge = BrowserBridge(manager, {EXTENSION_ORIGIN})
    port = None
    try:
        from tests.test_bridge import free_port
        port = free_port()
        bridge.start(port)
        assert not bridge.error, bridge.error
        args = (f"--load-extension={extension}", "--autoplay-policy=no-user-gesture-required", "about:blank")
        with LimitedBrave(profile, *args, **FUNCTIONAL) as browser:
            wait_for(lambda: any(t["type"] == "service_worker" and t["url"].startswith(EXTENSION_ORIGIN) for t in browser.targets()),
                     "extension worker", 20, 0.2)
            configure_extension(browser, port)
            target, session = browser.open_page("about:blank")

            # 1. A browser download goes to DOT and disappears from the browser.
            browser.call("Page.navigate", {"url": f"{base}/files/report-1.zip"}, session)
            offer = next_offer(bridge, 15)
            assert offer and offer["filename"] == "report-1.zip" and offer["total"] == len(ARCHIVE), offer
            assert offer["source"] == "download" and offer["headers"].get("User-Agent"), offer
            bridge.done(offer["id"])
            time.sleep(1.5)
            assert not (downloads / "report-1.zip").exists(), "the browser must not also save the file"
            report["intercepted"] = {"filename": offer["filename"], "size_checked_by_dot": offer["total"]}

            # 2. DOT unavailable: the browser keeps and finishes the download.
            bridge.stop()
            browser.call("Page.navigate", {"url": f"{base}/files/report-2.zip"}, session)
            wait_for(lambda: (downloads / "report-2.zip").exists(), "browser to save report-2.zip", 20, 0.2)
            assert bridge.pending.empty()
            assert_downloads_stay_in(browser, downloads)
            report["fail_open"] = "browser saved report-2.zip while DOT was unavailable"

            # 3. A burst of 12 simultaneous downloads stays bounded and nothing hangs.
            bridge.start(port)
            assert not bridge.error, bridge.error
            time.sleep(3.5)  # Let the extension's 3 s "is DOT running" cache expire.
            names = [f"burst-{i:02d}.zip" for i in range(12)]
            for name in names:
                browser.call("Target.createTarget", {"url": f"{base}/files/{name}"})
            burst = []

            def accounted():
                burst.extend(drain(bridge))  # Closing each window at once, like a quick user.
                handled = {o["filename"] for o in burst} | {n for n in names if (downloads / n).exists()}
                return handled >= set(names)
            wait_for(accounted, "all 12 burst downloads to be handled", 30, 0.3)
            assert_downloads_stay_in(browser, downloads)
            saved = [n for n in names if (downloads / n).exists()]
            assert len(burst) <= 4, f"at most 4 hand-offs in a burst, saw {len(burst)}"
            report["burst"] = {"downloads": 12, "sent_to_dot": len(burst), "kept_by_browser": len(saved)}

            # 4. The video button sends the playing video's file.
            video_target, video = browser.open_page(f"{base}/video")
            wait_for(lambda: browser.evaluate(video, "!document.getElementById('v').paused"), "video playback", 15, 0.2)
            box = wait_for(lambda: browser.evaluate(video, """(() => {
                const host = document.querySelector('dot-video-button');
                if (!host || getComputedStyle(host).display === 'none') return null;
                const r = host.getBoundingClientRect();
                return r.width > 20 ? {x: r.left + 40, y: r.top + r.height / 2} : null; })()"""), "video button", 15, 0.2)
            for kind in ("mousePressed", "mouseReleased"):
                browser.call("Input.dispatchMouseEvent", {"type": kind, **box, "button": "left", "clickCount": 1}, video)
            offer = next_offer(bridge, 15)
            assert offer and offer["url"].endswith("/media/sample.mp4") and offer["source"] == "media", offer
            bridge.done(offer["id"])
            assert browser.evaluate(video, "document.querySelectorAll('dot-video-button').length") == 1
            report["video_button"] = {"sent": offer["url"].rsplit("/", 1)[-1], "buttons_in_page": 1}
            browser.call("Target.closeTarget", {"targetId": video_target})

            # 4b. A YouTube-style player (blob: source, nothing detectable) sends the page for yt-dlp.
            # No address wait: the page moves itself to /watch?v=spa123; the next wait covers it.
            blob_target, blob = browser.open_page(f"{base}/blobvideo", wait=False)
            wait_for(lambda: browser.evaluate(blob, "(() => { const v = document.getElementById('v');"
                                                    " return v.currentSrc.startsWith('blob:') && !v.paused"
                                                    " && location.pathname === '/watch'; })()"),
                     "blob video playback after an in-page navigation", 15, 0.2)
            box = wait_for(lambda: browser.evaluate(blob, """(() => {
                const host = document.querySelector('dot-video-button');
                if (!host || getComputedStyle(host).display === 'none') return null;
                const r = host.getBoundingClientRect();
                return r.width > 20 ? {x: r.left + 40, y: r.top + r.height / 2} : null; })()"""), "blob video button", 15, 0.2)
            for kind in ("mousePressed", "mouseReleased"):
                browser.call("Input.dispatchMouseEvent", {"type": kind, **box, "button": "left", "clickCount": 1}, blob)
            offer = next_offer(bridge, 15)
            if not offer:
                target, session = browser.open_page(f"{EXTENSION_ORIGIN}/popup.html")
                status = browser.evaluate(session, "chrome.storage.session.get('status')")
                raise AssertionError(f"no page hand-off; extension status: {status}; open offers: {bridge.open_offers}")
            assert offer["site"] and offer["source"] == "page", offer
            assert offer["url"] == f"{base}/watch?v=spa123", f"sent {offer['url']} instead of the current address"
            bridge.done(offer["id"])
            report["page_video"] = {"player_source": "blob:", "sent_to_dot_as": "page for yt-dlp",
                                    "address_after_in_page_navigation": "/watch?v=spa123 (current, not first-loaded)"}
            browser.call("Target.closeTarget", {"targetId": blob_target})

            # 4c. Shadow-root players (Reddit): no button while it just plays; it appears when the
            # pointer moves onto the player, and sends the video from the open shadow root.
            visible_button = """(() => {
                const host = document.querySelector('dot-video-button');
                if (!host || getComputedStyle(host).display === 'none') return null;
                const r = host.getBoundingClientRect();
                return r.width > 20 ? {x: r.left + 40, y: r.top + r.height / 2} : null; })()"""
            report["shadow_players"] = {}
            for mode in ("open", "closed"):
                shadow_target, shadow = browser.open_page(f"{base}/{mode}player")
                box = wait_for(lambda: browser.evaluate(shadow, """(() => { const p = document.getElementById('p');
                    const r = p && p.getBoundingClientRect(); return r && r.width > 0 ? {x: r.left + r.width / 2, y: r.top + r.height / 2} : null; })()"""),
                    f"{mode} shadow player", 15, 0.2)
                time.sleep(2)  # Playing, but its "playing" event stays inside the shadow root.
                before = browser.evaluate(shadow, visible_button)
                assert before is None, f"{mode}: the button appeared without hovering (the test needs a hidden video)"
                browser.call("Input.dispatchMouseEvent", {"type": "mouseMoved", **box}, shadow)
                button = wait_for(lambda: browser.evaluate(shadow, visible_button), f"{mode} shadow player button", 10, 0.2)
                result = {"button_before_hover": False, "button_on_hover": True}
                if mode == "open":
                    for kind in ("mousePressed", "mouseReleased"):
                        browser.call("Input.dispatchMouseEvent", {"type": kind, **button, "button": "left", "clickCount": 1}, shadow)
                    offer = next_offer(bridge, 15)
                    assert offer and offer["url"].endswith("/media/sample.mp4"), offer
                    assert offer["filename"] == "Shadow test clip.mp4", f"named {offer['filename']!r}, not after the video"
                    bridge.done(offer["id"])
                    result["sent"] = "sample.mp4"
                    result["saved_as"] = offer["filename"]
                report["shadow_players"][mode] = result
                browser.call("Target.closeTarget", {"targetId": shadow_target})

            # 4d. An embedded player whose page title only names the player ("... Video Player"):
            # the file is named after the page around it, as in IDM.
            embed_target, embed = browser.open_page(f"{base}/embedtop")
            frame_button = """(() => { const f = document.querySelector('iframe'); const d = f && f.contentDocument;
                const host = d && d.querySelector('dot-video-button');
                if (!host || d.defaultView.getComputedStyle(host).display === 'none') return null;
                const fr = f.getBoundingClientRect(), r = host.getBoundingClientRect();
                return r.width > 20 ? {x: fr.left + r.left + 40, y: fr.top + r.top + r.height / 2} : null; })()"""
            box = wait_for(lambda: browser.evaluate(embed, frame_button), "embedded player button", 20, 0.2)
            for kind in ("mousePressed", "mouseReleased"):
                browser.call("Input.dispatchMouseEvent", {"type": kind, **box, "button": "left", "clickCount": 1}, embed)
            offer = next_offer(bridge, 15)
            assert offer and offer["filename"] == "Real article title.mp4", f"named {offer and offer['filename']!r}"
            bridge.done(offer["id"])
            report["embedded_player"] = {"player_page_title": "Example Video Player", "saved_as": offer["filename"]}
            browser.call("Target.closeTarget", {"targetId": embed_target})
            report["first_session_job"] = browser.job_snapshot()

        # 5. Restart with download history in the profile: nothing is sent at start-up.
        drain(bridge)
        served_before = len(Fixtures.files_served)
        with LimitedBrave(profile, *args, **FUNCTIONAL) as browser:
            time.sleep(12)
            history = None
            target, session = browser.open_page(f"{EXTENSION_ORIGIN}/popup.html")
            history = browser.evaluate(session, "chrome.downloads.search({}).then(items => items.length)")
            report["restart"] = {"history_items": history, "offers_at_startup": len(drain(bridge)),
                                 "file_requests_at_startup": len(Fixtures.files_served) - served_before,
                                 "job": browser.job_snapshot()}
            assert report["restart"]["offers_at_startup"] == 0 and report["restart"]["file_requests_at_startup"] == 0
        report["restart"]["clean_exit"] = browser.clean_exit
    finally:
        bridge.stop()
        manager.close()
    return report


def main():
    result = {"browser": None, "limits": {"benchmark": BENCH, "functional": FUNCTIONAL}}
    with tempfile.TemporaryDirectory(prefix="dot-validate-") as temp, contextlib.ExitStack() as stack:
        folder = Path(temp)
        server = https_server(Fixtures, folder)
        stack.callback(server.server_close)
        stack.callback(server.shutdown)
        base = f"https://127.0.0.1:{server.server_port}"
        with LimitedBrave(folder / "profile-version", "about:blank", time_limit=30) as probe:
            result["browser"] = probe.call("Browser.getVersion")["product"]
        if "--functional-only" not in sys.argv:
            variants = [("no extension", None)]
            if OLD_EXTENSION.is_dir():
                variants.append(("withdrawn 0.2", copy_extension(OLD_EXTENSION, folder / "ext-0.2", strip_native=True)))
            else:
                print("Benchmark: withdrawn 0.2 skipped (no local copy of that extension)", flush=True)
            variants.append(("DOT current", copy_extension(NEW_EXTENSION, folder / "ext-current")))
            result["startup_benchmark"] = {}
            for variant, extension in variants:
                print(f"Benchmark: {variant}", flush=True)
                result["startup_benchmark"][variant] = benchmark(folder, base, variant.replace(" ", "-"), extension)
                print("  ", json.dumps(result["startup_benchmark"][variant]), flush=True)
        print("Functional checks: DOT current", flush=True)
        result["functional"] = functional(folder, base)
    out = ROOT / "artifacts/extension-validation"
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
