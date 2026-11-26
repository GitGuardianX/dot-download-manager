"""Download, verify and unpack the helper tools the installer bundles (yt-dlp, Deno, FFmpeg).

Each pinned release in installer/tools.json is downloaded once into build/tools/cache and checked
against the publisher's own SHA-256 file. The verified hash is recorded in installer/tools.lock.json;
later builds refuse a download that differs from it. Only the needed files are unpacked into
build/tools/bin, with license notices in build/tools/licenses.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "installer" / "tools.json"
LOCK = ROOT / "installer" / "tools.lock.json"
OUT = ROOT / "build" / "tools"
AGENT = {"User-Agent": "DOT-build/1.0"}


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=AGENT), timeout=120) as response:
        return response.read()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def published_hash(checksums: str, filename: str) -> str:
    """The publisher's hash for filename, from a 'HASH  name' list or a single-hash file."""
    hashes = []
    for line in checksums.splitlines():
        found = re.findall(r"\b[0-9a-fA-F]{64}\b", line)
        if found and filename in line:
            return found[0].lower()
        hashes += found
    if len(hashes) == 1:
        return hashes[0].lower()
    raise SystemExit(f"No published SHA-256 for {filename}")


def download(name: str, spec: dict, lock: dict) -> Path:
    filename = spec["url"].rsplit("/", 1)[-1]
    target = OUT / "cache" / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    expected = lock.get(name, {}).get("sha256") if lock.get(name, {}).get("file") == filename else None
    if not (target.exists() and expected and sha256(target) == expected):
        print(f"Downloading {name} {spec['version']} ...", flush=True)
        partial = target.with_suffix(target.suffix + ".part")
        partial.write_bytes(fetch(spec["url"]))
        actual = sha256(partial)
        publisher = published_hash(fetch(spec["checksums"]).decode("utf-8", "replace"), filename)
        if actual != publisher:
            partial.unlink()
            raise SystemExit(f"{name}: SHA-256 {actual} does not match the publisher's {publisher}")
        if expected and actual != expected:
            partial.unlink()
            raise SystemExit(f"{name}: SHA-256 {actual} differs from the locked {expected}")
        partial.replace(target)
        lock[name] = {"file": filename, "version": spec["version"], "sha256": actual}
    print(f"  {name}: {filename} verified ({lock[name]['sha256'][:16]}...)", flush=True)
    return target


def unpack(name: str, archive: Path, bin_dir: Path, licenses: Path):
    if name == "yt-dlp":
        shutil.copy2(archive, bin_dir / "yt-dlp.exe")
        return
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            path = member.filename
            base = path.rsplit("/", 1)[-1]
            if name == "deno" and base == "deno.exe":
                wanted = bin_dir / base
            elif name == "ffmpeg" and "/bin/" in path and (base in ("ffmpeg.exe", "ffprobe.exe") or base.endswith(".dll")):
                wanted = bin_dir / base
            elif name == "ffmpeg" and base == "LICENSE.txt" and path.count("/") == 1:
                wanted = licenses / "FFmpeg-LICENSE.txt"
            else:
                continue
            with bundle.open(member) as source, wanted.open("wb") as output:
                shutil.copyfileobj(source, output)


def main():
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    lock = json.loads(LOCK.read_text(encoding="utf-8")) if LOCK.exists() else {}
    bin_dir, licenses = OUT / "bin", OUT / "licenses"
    for folder in (bin_dir, licenses):  # Rebuilt each time from verified archives.
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
    for name, item in spec.items():
        unpack(name, download(name, item, lock), bin_dir, licenses)
    LOCK.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    notices = ["DOT Download Manager bundles these third-party programs, unchanged:", ""]
    for name, item in spec.items():
        notices.append(f"{name} {item['version']}: {item['license']}")
    notices += ["", "DOT.exe also contains Python (PSF License, https://docs.python.org/3/license.html)",
                "and Tcl/Tk (BSD-style license, https://www.tcl.tk/software/tcltk/license.html)."]
    (licenses / "THIRD-PARTY.txt").write_text("\n".join(notices) + "\n", encoding="utf-8")
    required = ["yt-dlp.exe", "deno.exe", "ffmpeg.exe", "ffprobe.exe"]
    missing = [name for name in required if not (bin_dir / name).is_file()]
    if missing:
        raise SystemExit(f"Missing after unpacking: {missing}")
    total = sum(p.stat().st_size for p in bin_dir.iterdir()) / 1048576
    print(f"Tools ready in {bin_dir}: {len(list(bin_dir.iterdir()))} files, {total:.0f} MiB", flush=True)


if __name__ == "__main__":
    sys.exit(main())
