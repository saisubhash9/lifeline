"""Precompute the default five-seed evaluation (served instantly by /api/evaluate)."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.sim import STRATEGY_NAMES, evaluate, headline  # noqa: E402

rows = evaluate(seeds=5, latency=1, loss=0.0)
out = {"rows": rows, "headline": headline(rows), "seeds": 5, "strategies": STRATEGY_NAMES}
(ROOT / "data" / "models" / "evaluation_default.json").write_text(json.dumps(out))
print("wrote", len(rows), "rows")
