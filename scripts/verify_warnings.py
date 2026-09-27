"""Stage 2 of the early-warning cascade: Grok vision verifies each screened flare.

For every flare the stage-1 screener flags (all held-out test warnings plus the flares flagged
inside each replay window) this script builds one labeled 2x2 panel of real images from the
Helioviewer API and asks Grok to confirm (eruptive, likely proton storm) or reject (confined):

  A  SDO AIA 131 at the flare peak
  B  SDO AIA 193 difference: peak + 30 min minus flare start - 10 min (dimming / EUV wave)
  C  SOHO LASCO C2 at peak + 75 min (coronal mass ejection)
  D  LASCO C2 difference: peak + 75 min minus flare start - 20 min

No dates are shown to Grok (panels carry no timestamps) so it cannot recall famous events.
The verification time in the simulator is peak + 90 min, after the last image exists.

Outputs: data/flares/verifications.json (resumable), data/models/cascade_eval.json,
panels for replay-window flares in data/verify/ (shown in the dashboard).
"""

from __future__ import annotations

import io
import json
import ssl
import sys
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import certifi
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend.app import load_env  # noqa: E402,F401
from backend.grok import verify_warning  # noqa: E402

CONTEXT = ssl.create_default_context(cafile=certifi.where())
API = "https://api.helioviewer.org/v2"
CACHE = Path("/tmp/lifeline-verify")
PANELS = ROOT / "data" / "verify"
RESULTS = ROOT / "data" / "flares" / "verifications.json"
LAYERS = {
    "aia131": ("[SDO,AIA,AIA,131,1,100]", 2.5, 10),
    "aia193": ("[SDO,AIA,AIA,193,1,100]", 2.5, 11),
    "c2": ("[SOHO,LASCO,C2,white-light,1,100]", 11.9, 4),
}
SIZE = 512
lock = threading.Lock()


def get(url: str, timeout: int = 120) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Lifeline/1.0"})
    for attempt in range(3):
        try:
            return urllib.request.urlopen(request, timeout=timeout, context=CONTEXT).read()
        except Exception:  # noqa: BLE001
            if attempt == 2:
                raise
    return b""


def closest_offset_minutes(when: datetime, source_id: int) -> float | None:
    data = json.loads(get(f"{API}/getClosestImage/?date={when:%Y-%m-%dT%H:%M:%SZ}&sourceId={source_id}", 60))
    if "date" not in data:
        return None
    actual = datetime.strptime(data["date"], "%Y-%m-%d %H:%M:%S")
    return abs((actual - when).total_seconds()) / 60


def image(layer: str, when: datetime) -> Image.Image | None:
    spec, scale, source_id = LAYERS[layer]
    path = CACHE / f"{layer}_{when:%Y%m%dT%H%M}.png"
    if not path.exists():
        if layer == "c2":
            offset = closest_offset_minutes(when, source_id)
            if offset is None or offset > 40:
                return None
        query = urllib.parse.urlencode({"date": f"{when:%Y-%m-%dT%H:%M:%SZ}", "imageScale": scale, "layers": spec, "x0": 0, "y0": 0, "width": 1024, "height": 1024, "display": "true", "watermark": "false"})
        path.write_bytes(get(f"{API}/takeScreenshot/?{query}"))
    return Image.open(path).convert("RGB").resize((SIZE, SIZE))


def difference(after: Image.Image | None, before: Image.Image | None) -> Image.Image | None:
    if after is None or before is None:
        return None
    a = np.asarray(after.convert("L"), dtype=float)
    b = np.asarray(before.convert("L"), dtype=float)
    diff = a - b
    scale = max(4.0, 3.0 * float(np.std(diff)))
    out = np.clip(128 + 127 * diff / scale, 0, 255).astype(np.uint8)
    return Image.fromarray(out).convert("RGB")


def panel(images: list[tuple[str, Image.Image | None]]) -> Image.Image:
    canvas = Image.new("RGB", (SIZE * 2, SIZE * 2), (0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    for index, (label, img) in enumerate(images):
        x, y = (index % 2) * SIZE, (index // 2) * SIZE
        if img is not None:
            canvas.paste(img, (x, y))
        else:
            draw.text((x + SIZE // 2 - 60, y + SIZE // 2), "no data", fill=(160, 160, 160), font=font)
        draw.rectangle([x, y, x + SIZE - 1, y + 34], fill=(0, 0, 0))
        draw.text((x + 10, y + 6), label, fill=(255, 255, 255), font=font)
        draw.rectangle([x, y, x + SIZE - 1, y + SIZE - 1], outline=(70, 70, 70), width=2)
    return canvas


def build(flare: dict) -> Path:
    peak = datetime.fromisoformat(flare["peakUtc"].replace("Z", ""))
    start = datetime.fromisoformat(flare["startUtc"].replace("Z", ""))
    out = CACHE / f"panel_{flare['flare_id']}.jpg"
    if out.exists():
        return out
    a = image("aia131", peak)
    b = difference(image("aia193", peak + timedelta(minutes=30)), image("aia193", start - timedelta(minutes=10)))
    c_after = image("c2", peak + timedelta(minutes=75))
    d = difference(c_after, image("c2", start - timedelta(minutes=20)))
    panel([("A  AIA 131 at flare peak", a), ("B  AIA 193 difference", b), ("C  LASCO C2, +75 min", c_after), ("D  LASCO C2 difference", d)]).save(out, quality=88)
    return out


def facts(flare: dict) -> dict:
    lon = flare["lon"]
    return {
        "flareClass": flare["class"],
        "location": None if lon is None else f"{'W' if lon > 0 else 'E'}{abs(round(lon))}, {'N' if (flare['lat'] or 0) >= 0 else 'S'}{abs(round(flare['lat'] or 0))}",
        "riseTimeMinutes": flare["riseMinutes"],
        "xrayFluenceToPeakJm2": flare["fluence"],
        "screenerProbability": flare["p"],
        "baseRate": "about 1 in 80 M-class-or-stronger flares causes a proton storm",
    }


CALIBRATION_NEGATIVES = 28
RECALL_TARGET = 0.9


def main() -> None:
    import random

    CACHE.mkdir(parents=True, exist_ok=True)
    PANELS.mkdir(parents=True, exist_ok=True)
    test = json.loads((ROOT / "data" / "flares" / "test_warnings.json").read_text())
    pool = json.loads((ROOT / "data" / "flares" / "train_warnings.json").read_text())
    rng = random.Random(11)
    calibration = [w for w in pool if w["sep"]] + rng.sample([w for w in pool if not w["sep"]], CALIBRATION_NEGATIVES)
    windows = json.loads((ROOT / "data" / "flares" / "window_screen.json").read_text())
    window_flags = {item["flare_id"]: (window, item) for window, items in windows.items() for item in items if item["warn"]}
    todo = {}
    for group, items in (("calibration", calibration), ("test", test)):
        for item in items:
            todo.setdefault(item["flare_id"], {**item, "group": group})
    for fid, (_window, item) in window_flags.items():
        todo.setdefault(fid, {**item, "group": "window"})
    results = json.loads(RESULTS.read_text()) if RESULTS.exists() else {}
    pending = [item for fid, item in todo.items() if str(fid) not in results]
    pending.sort(key=lambda item: {"calibration": 0, "window": 1, "test": 2}[item["group"]])
    print(f"{len(todo)} flares ({len(calibration)} calibration, {len(test)} test), {len(pending)} pending", flush=True)

    def work(item):
        try:
            path = build(item)
        except Exception as error:  # noqa: BLE001
            print(item["peakUtc"], "images failed:", error, flush=True)
            return
        verdict = verify_warning(path, facts(item))
        if verdict is None or verdict["probability"] is None:
            print(item["peakUtc"], "no Grok result", flush=True)
            return
        if item["flare_id"] in window_flags:
            window = window_flags[item["flare_id"]][0]
            Image.open(path).save(PANELS / f"{window}_{item['flare_id']}.jpg", quality=82)
            verdict["panel"] = f"{window}_{item['flare_id']}.jpg"
        with lock:
            results[str(item["flare_id"])] = {**verdict, "peakUtc": item["peakUtc"], "class": item["class"], "sep": item["sep"], "screen": item["p"]}
            RESULTS.write_text(json.dumps(results, indent=1))
        print(f"[{item['group']:11s}] {item['peakUtc'][:16]} {item['class']:5s} {'STORM' if item['sep'] else 'none '} -> {verdict['probability']:3d}% cme={verdict['cmeVisible']}", flush=True)

    with ThreadPoolExecutor(max_workers=6) as pool_:
        list(pool_.map(work, pending))
    evaluate(calibration, test, results)


def evaluate(calibration: list[dict], test: list[dict], results: dict) -> None:
    def scored(items):
        return [(item, results[str(item["flare_id"])]) for item in items if str(item["flare_id"]) in results]

    cal = scored(calibration)
    storms = sorted([v["probability"] for item, v in cal if item["sep"]], reverse=True)
    keep = int(np.ceil(RECALL_TARGET * len(storms)))
    threshold = storms[keep - 1] if storms else 50
    for record in results.values():
        record["verdict"] = "confirm" if record["probability"] >= threshold else "reject"
        record["threshold"] = threshold
    RESULTS.write_text(json.dumps(results, indent=1))

    months = 26.8
    rows = scored(test)
    storm_rows = [(item, v) for item, v in rows if item["sep"]]
    alarm_rows = [(item, v) for item, v in rows if not item["sep"]]
    confirmed_storms = sum(1 for _, v in storm_rows if v["verdict"] == "confirm")
    confirmed_alarms = sum(1 for _, v in alarm_rows if v["verdict"] == "confirm")
    pos = [v["probability"] for _, v in storm_rows]
    neg = [v["probability"] for _, v in alarm_rows]
    pairs = [(a > b) + 0.5 * (a == b) for a in pos for b in neg]
    cal_pos = [v["probability"] for item, v in cal if item["sep"]]
    cal_neg = [v["probability"] for item, v in cal if not item["sep"]]
    cal_pairs = [(a > b) + 0.5 * (a == b) for a in cal_pos for b in cal_neg]
    summary = {
        "threshold": threshold,
        "thresholdRule": f"highest Grok probability that keeps >= {int(RECALL_TARGET * 100)}% of calibration storms (training period only)",
        "calibration": {"flares": len(cal), "storms": len(cal_pos), "grokAuc": round(sum(cal_pairs) / len(cal_pairs), 3) if cal_pairs else None},
        "test": {"verified": len(rows), "warnings": len(test)},
        "stage1": {"storms": len(storm_rows), "falseAlarms": len(alarm_rows), "warnings": len(rows), "warningsPerMonth": round(len(rows) / months, 1)},
        "cascade": {
            "stormsConfirmed": confirmed_storms,
            "storms": len(storm_rows),
            "falseAlarmsKept": confirmed_alarms,
            "falseAlarmsRemoved": len(alarm_rows) - confirmed_alarms,
            "warnings": confirmed_storms + confirmed_alarms,
            "warningsPerMonth": round((confirmed_storms + confirmed_alarms) / months, 1),
            "precision": round(confirmed_storms / max(1, confirmed_storms + confirmed_alarms), 3),
        },
        "grokAucAmongWarnings": round(sum(pairs) / len(pairs), 3) if pairs else None,
        "meanProbabilityStorms": round(sum(pos) / len(pos), 1) if pos else None,
        "meanProbabilityFalseAlarms": round(sum(neg) / len(neg), 1) if neg else None,
        "storms": [
            {"peakUtc": item["peakUtc"], "class": item["class"], "probability": v["probability"], "verdict": v["verdict"], "cmeVisible": v["cmeVisible"], "cmeExtent": v["cmeExtent"], "reason": v["reason"]}
            for item, v in storm_rows
        ],
    }
    def auc(pos_scores, neg_scores):
        pairs_ = [(a > b) + 0.5 * (a == b) for a in pos_scores for b in neg_scores]
        return round(sum(pairs_) / len(pairs_), 3) if pairs_ else None

    screen_pos = [item["p"] for item, _ in storm_rows]
    screen_neg = [item["p"] for item, _ in alarm_rows]
    everything = [(item["p"], v["probability"]) for item, v in rows]
    order_screen = sorted(x for x, _ in everything)
    order_grok = sorted(y for _, y in everything)

    def rank(value, ordered):
        return sum(1 for other in ordered if other < value) + 0.5 * sum(1 for other in ordered if other == value)

    combined = {id(item): rank(item["p"], order_screen) + rank(v["probability"], order_grok) for item, v in rows}
    summary["informationAmongWarnings"] = {
        "screenerAuc": auc(screen_pos, screen_neg),
        "grokAuc": summary["grokAucAmongWarnings"],
        "combinedRankAuc": auc([combined[id(item)] for item, _ in storm_rows], [combined[id(item)] for item, _ in alarm_rows]),
        "note": "ROC-AUC for separating real storms from false alarms among flares the screener already flagged (held-out test).",
    }
    (ROOT / "data" / "models" / "cascade_eval.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps({k: v for k, v in summary.items() if k != "storms"}, indent=1))
    for item in summary["storms"]:
        print("storm", item["peakUtc"][:16], item["class"], item["probability"], item["verdict"], item["cmeExtent"])


if __name__ == "__main__":
    if sys.argv[1:] == ["--evaluate"]:
        test = json.loads((ROOT / "data" / "flares" / "test_warnings.json").read_text())
        pool = json.loads((ROOT / "data" / "flares" / "train_warnings.json").read_text())
        import random

        rng = random.Random(11)
        calibration = [w for w in pool if w["sep"]] + rng.sample([w for w in pool if not w["sep"]], CALIBRATION_NEGATIVES)
        evaluate(calibration, test, json.loads(RESULTS.read_text()))
    else:
        main()
