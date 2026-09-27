"""Train the flare early-warning model: will this flare cause a >=10 pfu proton storm?

L2-regularized, class-weighted logistic regression in numpy on features known at the
flare's X-ray peak. Honest protocol:
  * time split: train on flares before 2024-07-01, test on everything after
    (the test period contains the Milton week, Oct 2024, and the Jan 2026 S4 storm)
  * warning threshold chosen on out-of-fold training predictions only
  * replay-window probabilities are cross-fitted: each window's flares are scored by a
    model trained without the 60 days on either side of that window

Writes data/models/flare_sep.json and data/flares/window_warnings.json.
"""

from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SPLIT = "2024-07-01"
L2 = 1.0
FEATURES = ["log_peak", "log_fluence", "log_rise_min", "log_background", "connected", "has_location", "abs_lat"]


def connected(lon: float) -> float:
    """Magnetic connectivity to Earth peaks for western-hemisphere sources (Parker spiral, about W60)."""
    return math.exp(-(((lon - 60.0) / 40.0) ** 2))


def load() -> list[dict]:
    rows = []
    with (ROOT / "data" / "flares" / "training.csv").open() as handle:
        for row in csv.DictReader(handle):
            has = int(row["has_location"])
            lon = float(row["lon"]) if has else 0.0
            lat = float(row["lat"]) if has else 0.0
            rows.append(
                {
                    **row,
                    "x": {
                        "log_peak": float(row["log_peak"]),
                        "log_fluence": float(row["log_fluence"]),
                        "log_rise_min": float(row["log_rise_min"]),
                        "log_background": float(row["log_background"]),
                        "connected": connected(lon) if has else 0.0,
                        "has_location": float(has),
                        "abs_lat": abs(lat) / 90.0 if has else 0.0,
                    },
                    "y": int(row["sep"]),
                    "lon_value": lon if has else None,
                }
            )
    return rows


def matrix(rows: list[dict]) -> np.ndarray:
    return np.array([[row["x"][name] for name in FEATURES] for row in rows], dtype=float)


def fit(X: np.ndarray, y: np.ndarray) -> dict:
    mean, std = X.mean(0), X.std(0) + 1e-9
    Z = (X - mean) / std
    Z1 = np.hstack([np.ones((len(Z), 1)), Z])
    positive = max(1, y.sum())
    weights = np.where(y == 1, len(y) / (2 * positive), len(y) / (2 * (len(y) - positive)))
    beta = np.zeros(Z1.shape[1])
    for _ in range(50):  # Newton / IRLS
        p = 1 / (1 + np.exp(-Z1 @ beta))
        W = weights * p * (1 - p)
        grad = Z1.T @ (weights * (p - y)) + L2 * np.r_[0, beta[1:]]
        hess = (Z1.T * W) @ Z1 + L2 * np.diag(np.r_[0, np.ones(len(beta) - 1)])
        step = np.linalg.solve(hess, grad)
        beta -= step
        if np.abs(step).max() < 1e-8:
            break
    # Class weighting trains on a 50/50 world; shift the intercept back to the true base rate
    # so the output is a calibrated probability (prior correction).
    beta[0] += math.log(positive / (len(y) - positive))
    return {"mean": mean.tolist(), "std": std.tolist(), "coef": beta.tolist()}


def predict(model: dict, X: np.ndarray) -> np.ndarray:
    Z = (X - np.array(model["mean"])) / np.array(model["std"])
    return 1 / (1 + np.exp(-(model["coef"][0] + Z @ np.array(model["coef"][1:]))))


def auc(y: np.ndarray, p: np.ndarray) -> float:
    pos, neg = p[y == 1], p[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    wins = sum((neg < value).sum() + 0.5 * (neg == value).sum() for value in pos)
    return float(wins / (len(pos) * len(neg)))


def scores(y: np.ndarray, warn: np.ndarray) -> dict:
    tp = int((warn & (y == 1)).sum())
    fp = int((warn & (y == 0)).sum())
    fn = int((~warn & (y == 1)).sum())
    tn = int((~warn & (y == 0)).sum())
    pod = tp / (tp + fn) if tp + fn else float("nan")
    pofd = fp / (fp + tn) if fp + tn else 0.0
    far = fp / (tp + fp) if tp + fp else 0.0
    expected = ((tp + fn) * (tp + fp) + (tn + fn) * (tn + fp)) / max(1, tp + fp + fn + tn)
    hss = (tp + tn - expected) / max(1e-9, tp + fp + fn + tn - expected)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "pod": round(pod, 3), "far": round(far, 3), "tss": round(pod - pofd, 3), "hss": round(hss, 3)}


def best_threshold(y: np.ndarray, p: np.ndarray) -> float:
    grid = np.concatenate([np.linspace(0.005, 0.1, 20), np.linspace(0.12, 0.9, 40)])
    return float(max(grid, key=lambda value: (scores(y, p >= value)["hss"], value)))


def main() -> None:
    rows = load()
    X, y = matrix(rows), np.array([row["y"] for row in rows])
    peak_day = [row["peak_utc"][:10] for row in rows]
    train = np.array([day < SPLIT for day in peak_day])
    test = ~train

    # Out-of-fold training predictions by half-year blocks, to choose the threshold.
    blocks = np.array([day[:4] + ("a" if day[5:7] <= "06" else "b") for day in peak_day])
    oof = np.zeros(len(rows))
    for block in sorted(set(blocks[train])):
        hold = train & (blocks == block)
        fitted = fit(X[train & ~hold], y[train & ~hold])
        oof[hold] = predict(fitted, X[hold])
    theta = best_threshold(y[train], oof[train])

    model = fit(X[train], y[train])
    p_test = predict(model, X[test])
    warn = p_test >= theta
    x_class = X[:, FEATURES.index("log_peak")] >= -4.0
    west = np.array([row["lon_value"] is not None and row["lon_value"] > 0 for row in rows])
    brier = float(np.mean((p_test - y[test]) ** 2))
    climatology = float(np.mean((y[train].mean() - y[test]) ** 2))

    onsets = [(float(row["onset_hours"]), row["lon_value"]) for row, is_train in zip(rows, train) if is_train and row["y"] == 1 and row["onset_hours"]]
    def quantiles(values):
        values = sorted(values) or [2.0]
        return [round(float(np.percentile(values, q)), 2) for q in (10, 50, 90)]
    delays = {
        "west": quantiles([h for h, lon in onsets if lon is not None and lon > 0]),
        "east": quantiles([h for h, lon in onsets if lon is None or lon <= 0]),
        "all": quantiles([h for h, _ in onsets]),
    }

    reliability = []
    for lo, hi in ((0, 0.1), (0.1, 0.3), (0.3, 0.6), (0.6, 1.01)):
        mask = (p_test >= lo) & (p_test < hi)
        if mask.any():
            reliability.append({"bin": f"{lo:.1f}-{min(hi, 1):.1f}", "flares": int(mask.sum()), "meanForecast": round(float(p_test[mask].mean()), 3), "observedRate": round(float(y[test][mask].mean()), 3)})

    test_events = [
        {"peakUtc": row["peak_utc"], "class": row["class"], "lon": row["lon_value"], "p": round(float(p), 3), "warned": bool(p >= theta), "sepPfu": float(row["sep_pfu"])}
        for row, p, is_test in zip(rows, predict(model, X), test)
        if is_test and (row["y"] == 1 or p >= theta)
    ]
    metrics = {
        "trainFlares": int(train.sum()),
        "trainStorms": int(y[train].sum()),
        "testFlares": int(test.sum()),
        "testStorms": int(y[test].sum()),
        "testAuc": round(auc(y[test], p_test), 3),
        "testBrier": round(brier, 4),
        "climatologyBrier": round(climatology, 4),
        "threshold": round(theta, 2),
        "model": scores(y[test], warn),
        "baselineXClass": scores(y[test], x_class[test]),
        "baselineXClassWest": scores(y[test], (x_class & west)[test]),
        "reliability": reliability,
        "medianLeadHours": delays["all"][1],
    }
    out = {
        "name": "Flare-to-proton-storm early warning",
        "target": ">=10 pfu proton event (NOAA SEP list) whose parent is this flare",
        "features": FEATURES,
        "featureNotes": {
            "log_peak": "log10 peak XRS-B flux (W/m2)",
            "log_fluence": "log10 XRS-B flux integrated from flare start to peak (J/m2)",
            "log_rise_min": "log10 rise time in minutes",
            "log_background": "log10 pre-flare background flux",
            "connected": "exp(-((lon-60)/40)^2): magnetic connectivity of the source to Earth",
            "has_location": "1 if GOES-18 located the flare",
            "abs_lat": "|heliographic latitude| / 90",
        },
        **model,
        "threshold": round(theta, 2),
        "onsetDelayHours": delays,
        "split": {"train": f"2022-06-17 to {SPLIT}", "test": f"{SPLIT} to 2026-09-25"},
        "metrics": metrics,
        "testEvents": test_events,
        "sources": ["GOES-18 xrsf-l2-flsum and xrsf-l2-flloc (NOAA NCEI)", "NOAA Solar Proton Events Affecting the Earth Environment"],
    }
    (ROOT / "data" / "models").mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "models" / "flare_sep.json").write_text(json.dumps(out, indent=1))

    # Cross-fitted probabilities for flares inside each replay window.
    windows = json.loads((ROOT / "data" / "flares" / "windows.json").read_text())
    ids = {int(row["flare_id"]): i for i, row in enumerate(rows)}
    peaks = [datetime.fromisoformat(row["peak_utc"].replace("Z", "")) for row in rows]
    warnings = {}
    for window, flares in windows.items():
        if not flares:
            warnings[window] = []
            continue
        first = datetime.fromisoformat(flares[0]["peak_utc"].replace("Z", "")) - timedelta(days=60)
        last = datetime.fromisoformat(flares[-1]["peak_utc"].replace("Z", "")) + timedelta(days=60)
        keep = np.array([not (first <= moment <= last) for moment in peaks])
        fitted = fit(X[keep], y[keep])
        items = []
        for flare in flares:
            index = ids[int(flare["flare_id"])]
            p = float(predict(fitted, X[index : index + 1])[0])
            lon = rows[index]["lon_value"]
            delay = delays["west" if lon is not None and lon > 0 else "east"]
            items.append(
                {
                    "peakUtc": flare["peak_utc"],
                    "class": flare["class"],
                    "lon": lon,
                    "lat": float(flare["lat"]) if int(flare["has_location"]) else None,
                    "p": round(p, 3),
                    "warn": p >= theta,
                    "onsetHours": delay,
                    "sep": int(flare["sep"]),
                }
            )
        warnings[window] = items
        print(window, [(item["peakUtc"][5:16], item["class"], item["p"], "WARN" if item["warn"] else "", "SEP" if item["sep"] else "") for item in items if item["warn"] or item["sep"]])
    (ROOT / "data" / "flares" / "window_warnings.json").write_text(json.dumps(warnings, indent=1))
    print(json.dumps(metrics, indent=1))


if __name__ == "__main__":
    main()
