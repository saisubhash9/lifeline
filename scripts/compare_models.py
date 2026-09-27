"""Compare models for flare -> proton-storm early warning on the same honest protocol.

Data and features: data/flares/training.csv via scripts/train_flare_model.py (7 features known
at the flare's X-ray peak). Split: train on flares before 2024-07-01, test on everything after
(contains the Milton week and the Jan 2026 S4 storm). Hyperparameters and warning thresholds are
chosen only from out-of-fold predictions on the training period (half-year blocks).

Operating points reported on the untouched test period:
  high-recall  threshold catching >= 90% of training storms out-of-fold (cascade stage 1)
  balanced     threshold maximizing the Heidke skill score out-of-fold (current app)
  budget       the model's top 3 flares per month on the test period (ranking quality)

Writes data/models/comparison.json, docs/model_comparison.md, docs/model_comparison.png.
Needs: pip install -r requirements-ml.txt
"""

from __future__ import annotations

import json
import sys
import warnings
from itertools import product
from pathlib import Path

import numpy as np
from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.naive_bayes import GaussianNB
from sklearn.neighbors import KNeighborsClassifier
from sklearn.neural_network import MLPClassifier
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import train_flare_model as base  # noqa: E402

warnings.filterwarnings("ignore")
RECALL_TARGET = 0.9
BUDGET_PER_MONTH = 3
SEED = 7


def oversample(X, y, factor=None):
    """Duplicate positives so unweighted learners see a balanced problem."""
    pos = np.where(y == 1)[0]
    if len(pos) == 0:
        return X, y
    factor = factor or max(1, int((len(y) - len(pos)) / len(pos)))
    idx = np.concatenate([np.arange(len(y)), np.repeat(pos, factor - 1)])
    return X[idx], y[idx]


class Model:
    def __init__(self, name, family, build, grid, oversampled=False, score="proba"):
        self.name, self.family, self.build, self.grid = name, family, build, grid
        self.oversampled, self.score = oversampled, score

    def fit_score(self, params, X_train, y_train, X_eval):
        model = self.build(**params)
        Xf, yf = oversample(X_train, y_train) if self.oversampled else (X_train, y_train)
        model.fit(Xf, yf)
        if self.score == "decision":
            return model.decision_function(X_eval)
        return model.predict_proba(X_eval)[:, 1]


def grid(**options):
    keys = list(options)
    return [dict(zip(keys, values)) for values in product(*options.values())]


MODELS = [
    Model("Logistic regression (L2)", "linear", lambda C: make_pipeline(StandardScaler(), LogisticRegression(C=C, class_weight="balanced", max_iter=5000)), grid(C=[0.01, 0.1, 1, 10])),
    Model("Logistic regression (L1, sparse)", "linear", lambda C: make_pipeline(StandardScaler(), LogisticRegression(C=C, penalty="l1", solver="liblinear", class_weight="balanced", max_iter=5000)), grid(C=[0.01, 0.1, 1, 10])),
    Model("Random forest", "trees", lambda leaf, depth: RandomForestClassifier(n_estimators=500, min_samples_leaf=leaf, max_depth=depth, class_weight="balanced_subsample", random_state=SEED, n_jobs=-1), grid(leaf=[1, 5, 20], depth=[None, 4])),
    Model("Extra trees", "trees", lambda leaf, depth: ExtraTreesClassifier(n_estimators=500, min_samples_leaf=leaf, max_depth=depth, class_weight="balanced_subsample", random_state=SEED, n_jobs=-1), grid(leaf=[1, 5, 20], depth=[None, 4])),
    Model("Gradient boosting (histogram)", "trees", lambda lr, depth, iters: HistGradientBoostingClassifier(learning_rate=lr, max_depth=depth, max_iter=iters, class_weight="balanced", random_state=SEED), grid(lr=[0.03, 0.1], depth=[2, 3], iters=[100, 300])),
    Model("SVM (RBF kernel)", "kernel", lambda C, gamma: make_pipeline(StandardScaler(), SVC(C=C, gamma=gamma, class_weight="balanced")), grid(C=[0.1, 1, 10], gamma=["scale", 0.05]), score="decision"),
    Model("k-nearest neighbors", "instance", lambda k: make_pipeline(StandardScaler(), KNeighborsClassifier(n_neighbors=k, weights="distance")), grid(k=[15, 40, 80]), oversampled=True),
    Model("Gaussian naive Bayes", "probabilistic", lambda: GaussianNB(), grid()),
    Model("Neural network (MLP)", "neural", lambda hidden, alpha: make_pipeline(StandardScaler(), MLPClassifier(hidden_layer_sizes=hidden, alpha=alpha, max_iter=2000, random_state=SEED)), grid(hidden=[(16,), (32, 16)], alpha=[1e-3, 1e-1]), oversampled=True),
]


def hss_threshold(y, s):
    best, best_score = None, -1e9
    for t in np.unique(np.quantile(s, np.linspace(0.5, 0.999, 200))):
        stats = base.scores(y, s >= t)
        if stats["hss"] > best_score:
            best, best_score = t, stats["hss"]
    return best


def recall_threshold(y, s, target):
    pos = np.sort(s[y == 1])[::-1]
    need = int(np.ceil(target * len(pos)))
    return pos[need - 1] if need else pos[0]


def bootstrap_auc(y, s, n=2000):
    rng = np.random.default_rng(SEED)
    pos, neg = np.where(y == 1)[0], np.where(y == 0)[0]
    values = []
    for _ in range(n):
        idx = np.concatenate([rng.choice(pos, len(pos)), rng.choice(neg, len(neg))])
        values.append(roc_auc_score(y[idx], s[idx]))
    return [round(float(np.percentile(values, 2.5)), 3), round(float(np.percentile(values, 97.5)), 3)]


def operating(y, s, threshold, months):
    warn = s >= threshold
    stats = base.scores(y, warn)
    return {
        "stormsCaught": stats["tp"],
        "storms": int(y.sum()),
        "warnings": int(warn.sum()),
        "warningsPerMonth": round(float(warn.sum()) / months, 1),
        "precision": round(stats["tp"] / max(1, warn.sum()), 3),
        "hss": stats["hss"],
        "tss": stats["tss"],
    }


def main():
    rows = base.load()
    X, y = base.matrix(rows), np.array([row["y"] for row in rows])
    days = [row["peak_utc"][:10] for row in rows]
    train = np.array([day < base.SPLIT for day in days])
    test = ~train
    blocks = np.array([day[:4] + ("a" if day[5:7] <= "06" else "b") for day in days])
    months = 26.8  # 2024-07-01 .. 2026-09-25
    Xtr, ytr, Xte, yte = X[train], y[train], X[test], y[test]
    budget = int(round(BUDGET_PER_MONTH * months))

    def oof(model, params):
        out = np.zeros(train.sum())
        tr_blocks = blocks[train]
        for block in sorted(set(tr_blocks)):
            hold = tr_blocks == block
            if ytr[~hold].sum() == 0:
                continue
            out[hold] = model.fit_score(params, Xtr[~hold], ytr[~hold], Xtr[hold])
        return out

    results = []
    curves = {}

    def record(name, family, train_scores, test_scores, params=None):
        th_recall = recall_threshold(ytr, train_scores, RECALL_TARGET)
        th_hss = hss_threshold(ytr, train_scores)
        order = np.argsort(-test_scores)
        caught_budget = int(yte[order[:budget]].sum())
        all_need = int(np.max(np.where(yte[order] == 1)[0]) + 1)
        result = {
            "model": name,
            "family": family,
            "params": {k: str(v) for k, v in (params or {}).items()},
            "testRocAuc": round(float(roc_auc_score(yte, test_scores)), 3),
            "testRocAuc95": bootstrap_auc(yte, test_scores),
            "testPrAuc": round(float(average_precision_score(yte, test_scores)), 3),
            "trainOofRocAuc": round(float(roc_auc_score(ytr, train_scores)), 3),
            "highRecall": operating(yte, test_scores, th_recall, months),
            "balanced": operating(yte, test_scores, th_hss, months),
            "budget": {"warnings": budget, "stormsCaught": caught_budget, "storms": int(yte.sum())},
            "warningsToCatchAll": all_need,
        }
        results.append(result)
        curves[name] = np.cumsum(yte[order]).tolist()
        print(f"{name:34s} AUC {result['testRocAuc']:.3f} {result['testRocAuc95']}  PR-AUC {result['testPrAuc']:.3f}  "
              f"high-recall {result['highRecall']['stormsCaught']}/{int(yte.sum())} with {result['highRecall']['warnings']} warnings  "
              f"budget {caught_budget}/{int(yte.sum())}  all-in {all_need}", flush=True)

    peak = base.FEATURES.index("log_peak")
    record("Rule: flare peak brightness only", "rule", Xtr[:, peak], Xte[:, peak])

    fitted = base.fit(Xtr, ytr)
    tr_blocks = blocks[train]
    numpy_oof = np.zeros(train.sum())
    for block in sorted(set(tr_blocks)):
        hold = tr_blocks == block
        numpy_oof[hold] = base.predict(base.fit(Xtr[~hold], ytr[~hold]), Xtr[hold])
    record("Current app model (numpy logistic)", "linear", numpy_oof, base.predict(fitted, Xte))

    for model in MODELS:
        best, best_ap = None, -1
        for params in model.grid or [{}]:
            scores = oof(model, params)
            ap = average_precision_score(ytr, scores)
            if ap > best_ap:
                best, best_ap = params, ap
        train_scores = oof(model, best)
        test_scores = model.fit_score(best, Xtr, ytr, Xte)
        record(model.name, model.family, train_scores, test_scores, best)

    out = {
        "protocol": {
            "features": base.FEATURES,
            "trainPeriod": f"2022-06-17 to {base.SPLIT}",
            "testPeriod": f"{base.SPLIT} to 2026-09-25",
            "trainFlares": int(train.sum()), "trainStorms": int(ytr.sum()),
            "testFlares": int(test.sum()), "testStorms": int(yte.sum()),
            "tuning": "grid search on out-of-fold average precision, half-year blocks of the training period",
            "highRecallTarget": RECALL_TARGET,
            "budgetPerMonth": BUDGET_PER_MONTH,
        },
        "results": results,
        "curves": curves,
    }
    (ROOT / "data" / "models" / "comparison.json").write_text(json.dumps(out, indent=1))
    report(out)
    plot(out)


def report(out):
    p = out["protocol"]
    ranked = sorted(out["results"], key=lambda r: (-r["testPrAuc"], -r["testRocAuc"]))
    lines = [
        "# Flare early-warning: model comparison",
        "",
        f"Task: at a flare's X-ray peak, will it drive a ≥10 pfu proton storm? Features: {', '.join(p['features'])}.",
        f"Train {p['trainPeriod']} ({p['trainFlares']} flares, {p['trainStorms']} storms); test {p['testPeriod']} ({p['testFlares']} flares, {p['testStorms']} storms). "
        f"Hyperparameters and thresholds use training data only ({p['tuning']}).",
        "",
        "| Model | Test ROC-AUC (95% CI) | Test PR-AUC | High-recall mode: storms caught / warnings | Balanced mode: storms / warnings | Top 3/month: storms caught | Warnings to catch all |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in ranked:
        hr, bal = r["highRecall"], r["balanced"]
        lines.append(
            f"| {r['model']} | {r['testRocAuc']:.3f} ({r['testRocAuc95'][0]:.2f}–{r['testRocAuc95'][1]:.2f}) | {r['testPrAuc']:.3f} | "
            f"{hr['stormsCaught']}/{hr['storms']} · {hr['warnings']} ({hr['warningsPerMonth']}/mo) | "
            f"{bal['stormsCaught']}/{bal['storms']} · {bal['warnings']} | {r['budget']['stormsCaught']}/{r['budget']['storms']} | {r['warningsToCatchAll']} |"
        )
    lines += [
        "",
        "High-recall mode: threshold that caught ≥90% of training storms out-of-fold. Balanced mode: threshold maximizing the Heidke skill score out-of-fold. "
        f"Top 3/month: the model's {out['results'][0]['budget']['warnings']} highest-scoring test flares. PR-AUC matters most here because storms are ~1% of flares.",
        "With 11 test storms, differences of one or two storms are within noise; see the ROC-AUC intervals.",
    ]
    (ROOT / "docs" / "model_comparison.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


def plot(out):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9, 5.5), dpi=140)
    fig.patch.set_facecolor("#0e1520")
    ax.set_facecolor("#0e1520")
    ranked = sorted(out["results"], key=lambda r: -r["testPrAuc"])
    palette = ["#3dce86", "#8eb4ff", "#e2b15a", "#c9a8ff", "#f09a92", "#7fe0b8", "#d7c6ff", "#93a3b8", "#f4c56d", "#6f7f95", "#b0bec5"]
    for color, r in zip(palette, ranked):
        curve = out["curves"][r["model"]]
        style = "--" if r["family"] == "rule" else "-"
        ax.plot(range(1, len(curve) + 1), curve, style, color=color, lw=1.8, label=f"{r['model']} (PR-AUC {r['testPrAuc']:.2f})")
    budget = out["results"][0]["budget"]["warnings"]
    ax.axvline(budget, color="#e8eef7", lw=0.8, alpha=0.5)
    ax.text(budget + 3, 0.6, "3 warnings / month", color="#e8eef7", fontsize=8)
    ax.set_xscale("log")
    ax.set_xlim(1, 1100)
    ax.set_xlabel("Warnings issued on the test period (top-scoring flares first)", color="#c9d4e3")
    ax.set_ylabel("Proton storms caught (of 11)", color="#c9d4e3")
    ax.set_title("Held-out test, Jul 2024 – Sep 2026: storms caught vs warnings issued", color="#e8eef7", fontsize=11)
    ax.tick_params(colors="#93a3b8")
    for spine in ax.spines.values():
        spine.set_color("#3a4658")
    ax.grid(alpha=0.15)
    ax.legend(fontsize=7, facecolor="#131c2a", edgecolor="#3a4658", labelcolor="#e8eef7", loc="upper left")
    fig.tight_layout()
    fig.savefig(ROOT / "docs" / "model_comparison.png", facecolor=fig.get_facecolor())


if __name__ == "__main__":
    main()
