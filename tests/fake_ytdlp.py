"""Stand-in for yt-dlp in tests: the same command-line shape, fake media, no network.

URL markers: "unavailable" fails, "nsig" warns about the missing challenge solver,
"slow" writes slowly enough to pause, "playlist" returns a playlist, "warmup" waits
before the first bytes like a real page read.
"""
import json
import sys
import time
from pathlib import Path

args = sys.argv[1:]
url = args[args.index("--") + 1] if "--" in args else args[-1]

if "-J" in args:
    if "unavailable" in url:
        print("ERROR: [youtube] abc123: Video unavailable", file=sys.stderr)
        sys.exit(1)
    if "nsig" in url:
        print("WARNING: [youtube] abc123: n challenge solving failed: Some formats may be missing.", file=sys.stderr)
    if "playlist" in url:
        print(json.dumps({"_type": "playlist", "title": "A playlist", "entries": []}))
        sys.exit(0)
    print(json.dumps({"title": "Fake: Clip/Title?", "duration": 19, "extractor_key": "Youtube", "formats": [
        {"format_id": "18", "ext": "mp4", "height": 360, "vcodec": "avc1", "acodec": "mp4a", "filesize": 900000, "tbr": 500},
        {"format_id": "137", "ext": "mp4", "height": 1080, "vcodec": "avc1", "acodec": "none", "filesize": 5000000, "tbr": 4000},
        {"format_id": "248", "ext": "webm", "height": 1080, "vcodec": "vp9", "acodec": "none", "filesize": 4000000, "tbr": 3000},
        {"format_id": "136", "ext": "mp4", "height": 720, "vcodec": "avc1", "acodec": "none", "filesize_approx": 2500000, "tbr": 2000},
        {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a", "filesize": 300000, "abr": 128},
        {"format_id": "251", "ext": "webm", "vcodec": "none", "acodec": "opus", "filesize": 280000, "abr": 140}]}))
    sys.exit(0)

folder = Path(args[args.index("-P") + 1])
template = args[args.index("-o") + 1]
ext = "m4a" if args[args.index("-f") + 1].startswith("ba") else "mp4"
if "unavailable" in url:
    print("ERROR: [youtube] abc123: Video unavailable", flush=True)
    sys.exit(1)
body = (b"FAKEAUDIO" if ext == "m4a" else b"FAKEVIDEO") * 50000
if "warmup" in url:
    time.sleep(1.5)  # Real yt-dlp reads the page for a few seconds before any bytes arrive.
partial = folder / f"download.f1.{ext}.part"
start = partial.stat().st_size if partial.exists() else 0
with partial.open("ab") as out:
    for position in range(start, len(body), 45000):
        out.write(body[position:position + 45000])
        out.flush()
        print(f"DOT {min(position + 45000, len(body))} {len(body)} NA", flush=True)
        time.sleep(0.25 if "slow" in url else 0)
final = folder / template.replace("%(ext)s", ext)
partial.replace(final)
print(f"DOTFILE {final}", flush=True)
