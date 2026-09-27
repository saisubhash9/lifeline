"""How good is Grok's Sun watch? Score its 48-hour outlook on sampled days.

Positives: days whose next 48 h contain an X-class flare (GOES-18 flare summary).
Negatives: days whose next 48 h contain no flare of M5 or stronger.
20 of each, sampled with a fixed seed from 2023-01-01..2025-12-31. Images are SDO AIA 193 +
HMI magnetogram at 00:00 UTC (Helioviewer). Writes data/sun/eval.json.
"""

from __future__ import annotations

import csv
import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from backend.app import load_env  # noqa: E402,F401
from backend.grok import sun_watch  # noqa: E402
from fetch_sun import screenshot  # noqa: E402

CACHE = Path("/tmp/lifeline-sun-eval")
SCORE = {"quiet": 0, "watch": 1, "warning": 2}


def main() -> None:
    flares = []
    with (ROOT / "data" / "flares" / "training.csv").open() as handle:
        for row in csv.DictReader(handle):
            flares.append((datetime.fromisoformat(row["peak_utc"].replace("Z", "")), float(row["log_peak"])))
    days = [datetime(2023, 1, 1) + timedelta(days=i) for i in range((datetime(2025, 12, 31) - datetime(2023, 1, 1)).days)]

    def peak_within(day):
        values = [level for when, level in flares if day <= when < day + timedelta(hours=48)]
        return max(values) if values else -9.0

    positives = [day for day in days if peak_within(day) >= -4.0]
    negatives = [day for day in days if peak_within(day) < -4.3]
    rng = random.Random(42)
    sample = [(day, 1) for day in rng.sample(positives, 20)] + [(day, 0) for day in rng.sample(negatives, 20)]
    rng.shuffle(sample)
    CACHE.mkdir(parents=True, exist_ok=True)

    def assess(item):
        day, label = item
        when = day.strftime("%Y-%m-%dT00:00:00Z")
        tag = day.strftime("%Y%m%d")
        try:
            aia = screenshot(when, "aia193", CACHE / f"{tag}_aia193")
            hmi = screenshot(when, "hmi", CACHE / f"{tag}_hmi")
        except Exception as error:  # noqa: BLE001
            print(tag, "image failed", error, flush=True)
            return None
        result = sun_watch([("SDO AIA 193 corona", aia), ("SDO HMI magnetogram", hmi)], when)
        if result is None:
            print(tag, "no Grok result", flush=True)
            return None
        print(when[:10], "X-flare" if label else "quiet  ", "->", result["outlook"], result["flareProbability"], flush=True)
        return {"date": when[:10], "xFlareWithin48h": label, "outlook": result["outlook"], "flareProbability": result["flareProbability"], "earthConnectedRisk": result["earthConnectedRisk"]}

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = [row for row in pool.map(assess, sample) if row]

    pos = [SCORE[r["outlook"]] for r in rows if r["xFlareWithin48h"]]
    neg = [SCORE[r["outlook"]] for r in rows if not r["xFlareWithin48h"]]
    pairs = [(p > n) + 0.5 * (p == n) for p in pos for n in neg]
    prob_pos = [r["flareProbability"] for r in rows if r["xFlareWithin48h"] and r["flareProbability"] is not None]
    prob_neg = [r["flareProbability"] for r in rows if not r["xFlareWithin48h"] and r["flareProbability"] is not None]
    prob_pairs = [(p > n) + 0.5 * (p == n) for p in prob_pos for n in prob_neg]
    summary = {
        "days": len(rows),
        "xFlareDays": len(pos),
        "quietDays": len(neg),
        "auc": round(sum(prob_pairs) / len(prob_pairs), 3) if prob_pairs else None,
        "outlookAuc": round(sum(pairs) / len(pairs), 3) if pairs else None,
        "meanProbabilityXDays": round(sum(prob_pos) / len(prob_pos), 1) if prob_pos else None,
        "meanProbabilityQuietDays": round(sum(prob_neg) / len(prob_neg), 1) if prob_neg else None,
        "warningHitRate": round(sum(1 for s in pos if s == 2) / len(pos), 3) if pos else None,
        "warningFalseAlarmRate": round(sum(1 for s in neg if s == 2) / len(neg), 3) if neg else None,
        "watchOrWarningOnXDays": round(sum(1 for s in pos if s >= 1) / len(pos), 3) if pos else None,
        "quietCalledOnQuietDays": round(sum(1 for s in neg if s == 0) / len(neg), 3) if neg else None,
        "outlookCounts": {
            "xFlareDays": {k: sum(1 for r in rows if r["xFlareWithin48h"] and r["outlook"] == k) for k in SCORE},
            "quietDays": {k: sum(1 for r in rows if not r["xFlareWithin48h"] and r["outlook"] == k) for k in SCORE},
        },
        "method": "20 days followed by an X-class flare within 48 h vs 20 days with no flare >= M5, 2023-2025, fixed seed; images at 00:00 UTC.",
    }
    (ROOT / "data" / "sun" / "eval.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
