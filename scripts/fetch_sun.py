"""Fetch real SDO images of the Sun for each replay window from the Helioviewer API.

For every window:
  watch_*  AIA 193 (corona) + HMI magnetogram at window start + 6 h: what Grok reviews for the
           day-ahead "Sun watch". A fixed rule, not a hand-picked moment.
  flare_*  AIA 131 at the peak of the window's most likely proton-producing flare (display only).

Images go to data/sun/<window>/ as JPEG (via Pillow if installed, else macOS sips, else PNG).
"""

from __future__ import annotations

import json
import shutil
import ssl
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import certifi

CONTEXT = ssl.create_default_context(cafile=certifi.where())
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "sun"
API = "https://api.helioviewer.org/v2/takeScreenshot/"
LAYERS = {
    "aia193": "[SDO,AIA,AIA,193,1,100]",
    "hmi": "[SDO,HMI,HMI,magnetogram,1,100]",
    "aia131": "[SDO,AIA,AIA,131,1,100]",
}


def screenshot(when: str, layer: str, path: Path) -> Path:
    query = urllib.parse.urlencode(
        {"date": when, "imageScale": 2.5, "layers": LAYERS[layer], "x0": 0, "y0": 0, "width": 1024, "height": 1024, "display": "true", "watermark": "false"}
    )
    png = path.with_suffix(".png")
    request = urllib.request.Request(f"{API}?{query}", headers={"User-Agent": "Lifeline/1.0"})
    png.write_bytes(urllib.request.urlopen(request, timeout=120, context=CONTEXT).read())
    jpg = path.with_suffix(".jpg")
    try:
        from PIL import Image

        Image.open(png).convert("RGB").resize((768, 768)).save(jpg, quality=86)
        png.unlink()
        return jpg
    except ImportError:
        pass
    if shutil.which("sips"):
        subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "86", "-Z", "768", str(png), "--out", str(jpg)], check=True, capture_output=True)
        png.unlink()
        return jpg
    return png


def main() -> None:
    warnings = json.loads((ROOT / "data" / "flares" / "window_warnings.json").read_text())
    index = {}
    for path in sorted((ROOT / "data" / "noaa").glob("*.json")):
        if path.name == "manifest.json":
            continue
        record = json.loads(path.read_text())
        window = record["id"]
        folder = OUT / window
        folder.mkdir(parents=True, exist_ok=True)
        start = datetime.fromisoformat(record["samples"][0]["t"].replace("Z", ""))
        watch = (start + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = {"watchUtc": watch, "watch": {}, "flare": None}
        for layer in ("aia193", "hmi"):
            saved = screenshot(watch, layer, folder / f"watch_{layer}")
            entry["watch"][layer] = f"{window}/{saved.name}"
            print(window, layer, saved.name, saved.stat().st_size)
        flares = [item for item in warnings.get(window, []) if item["warn"]]
        if flares:
            top = max(flares, key=lambda item: (item["sep"], item["p"]))
            saved = screenshot(top["peakUtc"], "aia131", folder / "flare_aia131")
            entry["flare"] = {"utc": top["peakUtc"], "class": top["class"], "lon": top["lon"], "lat": top["lat"], "image": f"{window}/{saved.name}"}
            print(window, "flare", top["class"], saved.name)
        index[window] = entry
    (OUT / "images.json").write_text(json.dumps(index, indent=1))


if __name__ == "__main__":
    main()
