"""Stage 1 of the early-warning cascade: a high-recall flare screener.

The model comparison (scripts/compare_models.py) picked an L2 logistic regression with strong
regularization (C = 0.01) as the best ranker on held-out data. This script trains it on the
training period only, sets the warning threshold on out-of-fold training predictions so that
>= 90% of training proton storms would have been flagged (false alarms are acceptable here:
Grok verifies each warning in stage 2), and exports everything the app needs as JSON.

Outputs:
  data/models/flare_screen.json    scaler, coefficients, prior-corrected intercept, threshold,
                                   onset delays, held-out metrics
  data/flares/window_screen.json   cross-fitted scores for flares inside each replay window
  data/flares/test_warnings.json   every held-out flare the screener flags (verified in stage 2)
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import train_flare_model as base  # noqa: E402

C = 0.01
RECALL_TARGET = 0.9


def fit(X, y):
    scaler = StandardScaler().fit(X)
    model = LogisticRegression(C=C, class_weight="balanced", max_iter=5000).fit(scaler.transform(X), y)
    # class_weight="balanced" trains on a 50/50 world; shift the intercept to the real base rate.
    intercept = float(model.intercept_[0]) + math.log(y.sum() / (len(y) - y.sum()))
    return {"mean": scaler.mean_.tolist(), "std": scaler.scale_.tolist(), "coef": [intercept] + model.coef_[0].tolist()}


def predict(spec, X):
    Z = (X - np.array(spec["mean"])) / np.array(spec["std"])
    return 1 / (1 + np.exp(-(spec["coef"][0] + Z @ np.array(spec["coef"][1:]))))


def main():
    rows = base.load()
    X, y = base.matrix(rows), np.array([row["y"] for row in rows])
    days = [row["peak_utc"][:10] for row in rows]
    train = np.array([day < base.SPLIT for day in days])
    test = ~train
    blocks = np.array([day[:4] + ("a" if day[5:7] <= "06" else "b") for day in days])

    oof = np.zeros(len(rows))
    for block in sorted(set(blocks[train])):
        hold = train & (blocks == block)
        oof[hold] = predict(fit(X[train & ~hold], y[train & ~hold]), X[hold])
    positives = np.sort(oof[train & (y == 1)])[::-1]
    threshold = float(positives[int(np.ceil(RECALL_TARGET * len(positives))) - 1])

    spec = fit(X[train], y[train])
    p = predict(spec, X)
    warn = p >= threshold
    months = 26.8
    stats = base.scores(y[test], warn[test])

    onsets = [(float(r["onset_hours"]), r["lon_value"]) for r, t in zip(rows, train) if t and r["y"] == 1 and r["onset_hours"]]

    def quantiles(values):
        values = sorted(values) or [2.0]
        return [round(float(np.percentile(values, q)), 2) for q in (10, 50, 90)]

    delays = {
        "west": quantiles([h for h, lon in onsets if lon is not None and lon > 0]),
        "east": quantiles([h for h, lon in onsets if lon is None or lon <= 0]),
        "all": quantiles([h for h, _ in onsets]),
    }
    test_warnings = []
    for i in np.where(test & warn)[0]:
        row = rows[i]
        test_warnings.append(
            {
                "flare_id": int(row["flare_id"]),
                "peakUtc": row["peak_utc"],
                "startUtc": row["start_utc"],
                "class": row["class"],
                "lon": row["lon_value"],
                "lat": float(row["lat"]) if int(row["has_location"]) else None,
                "riseMinutes": round(10 ** row["x"]["log_rise_min"], 1),
                "fluence": round(10 ** row["x"]["log_fluence"], 4),
                "p": round(float(p[i]), 4),
                "sep": int(row["y"]),
                "sepPfu": float(row["sep_pfu"]),
            }
        )
    metrics = {
        "trainFlares": int(train.sum()), "trainStorms": int(y[train].sum()),
        "testFlares": int(test.sum()), "testStorms": int(y[test].sum()),
        "testAuc": round(float(base.auc(y[test], p[test])), 3),
        "stage1": {**stats, "warnings": int(warn[test].sum()), "warningsPerMonth": round(warn[test].sum() / months, 1)},
        "medianLeadHours": delays["all"][1],
    }
    out = {
        "name": "Flare screener (stage 1 of the early-warning cascade)",
        "target": ">=10 pfu proton event (NOAA SEP list) whose parent is this flare",
        "model": f"L2 logistic regression, C={C}, class-balanced, prior-corrected intercept",
        "features": base.FEATURES,
        "featureNotes": {
            "log_peak": "log10 peak XRS-B flux (W/m2)",
            "log_fluence": "log10 XRS-B flux integrated from flare start to peak (J/m2)",
            "log_rise_min": "log10 rise time in minutes",
            "log_background": "log10 pre-flare background flux",
            "connected": "exp(-((lon-60)/40)^2): magnetic connectivity of the source to Earth",
            "has_location": "1 if GOES-18 located the flare",
            "abs_lat": "|heliographic latitude| / 90",
        },
        **spec,
        "threshold": round(threshold, 5),
        "recallTarget": RECALL_TARGET,
        "onsetDelayHours": delays,
        "split": {"train": f"2022-06-17 to {base.SPLIT}", "test": f"{base.SPLIT} to 2026-09-25"},
        "metrics": metrics,
        "sources": ["GOES-18 xrsf-l2-flsum and xrsf-l2-flloc (NOAA NCEI)", "NOAA Solar Proton Events Affecting the Earth Environment"],
    }
    (ROOT / "data" / "models" / "flare_screen.json").write_text(json.dumps(out, indent=1))
    (ROOT / "data" / "flares" / "test_warnings.json").write_text(json.dumps(test_warnings, indent=1))

    # Training-period flares the screener would have flagged (out-of-fold): the calibration pool for
    # choosing Grok's confirm threshold in stage 2. Test data is never used for that choice.
    train_warnings = []
    for i in np.where(train & (oof >= threshold))[0]:
        row = rows[i]
        train_warnings.append(
            {
                "flare_id": int(row["flare_id"]), "peakUtc": row["peak_utc"], "startUtc": row["start_utc"],
                "class": row["class"], "lon": row["lon_value"], "lat": float(row["lat"]) if int(row["has_location"]) else None,
                "riseMinutes": round(10 ** row["x"]["log_rise_min"], 1), "fluence": round(10 ** row["x"]["log_fluence"], 4),
                "p": round(float(oof[i]), 4), "sep": int(row["y"]), "sepPfu": float(row["sep_pfu"]),
            }
        )
    (ROOT / "data" / "flares" / "train_warnings.json").write_text(json.dumps(train_warnings, indent=1))
    print("training-period flags (out-of-fold):", len(train_warnings), "storms among them:", sum(w["sep"] for w in train_warnings))

    windows = json.loads((ROOT / "data" / "flares" / "windows.json").read_text())
    ids = {int(row["flare_id"]): i for i, row in enumerate(rows)}
    peaks = [datetime.fromisoformat(row["peak_utc"].replace("Z", "")) for row in rows]
    screened = {}
    for window, flares in windows.items():
        items = []
        if flares:
            first = datetime.fromisoformat(flares[0]["peak_utc"].replace("Z", "")) - timedelta(days=60)
            last = datetime.fromisoformat(flares[-1]["peak_utc"].replace("Z", "")) + timedelta(days=60)
            keep = np.array([not (first <= moment <= last) for moment in peaks])
            fitted = fit(X[keep], y[keep])
            for flare in flares:
                i = ids[int(flare["flare_id"])]
                score = float(predict(fitted, X[i : i + 1])[0])
                lon = rows[i]["lon_value"]
                items.append(
                    {
                        "flare_id": int(flare["flare_id"]),
                        "peakUtc": flare["peak_utc"],
                        "startUtc": flare["start_utc"],
                        "class": flare["class"],
                        "lon": lon,
                        "lat": float(flare["lat"]) if int(flare["has_location"]) else None,
                        "riseMinutes": round(10 ** rows[i]["x"]["log_rise_min"], 1),
                        "fluence": round(10 ** rows[i]["x"]["log_fluence"], 4),
                        "p": round(score, 4),
                        "warn": score >= threshold,
                        "onsetHours": delays["west" if lon is not None and lon > 0 else "east"],
                        "sep": int(flare["sep"]),
                    }
                )
        screened[window] = items
        flagged = [f"{item['class']}@{item['peakUtc'][5:16]} p={item['p']:.3f}{' SEP' if item['sep'] else ''}" for item in items if item["warn"]]
        print(window, len(flagged), "flagged:", flagged)
    (ROOT / "data" / "flares" / "window_screen.json").write_text(json.dumps(screened, indent=1))
    print(json.dumps(metrics, indent=1), "threshold", threshold)


if __name__ == "__main__":
    main()
