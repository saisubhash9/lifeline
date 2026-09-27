"""Cache Grok's Sun-watch briefing for every replay window (data/sun/assessments.json).

The dashboard serves these instantly and offline; "Re-run with Grok" asks again live.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.app import load_env  # noqa: E402,F401  (loads .env)
from backend.grok import sun_watch  # noqa: E402

SUN = ROOT / "data" / "sun"


def main() -> None:
    index = json.loads((SUN / "images.json").read_text())
    path = SUN / "assessments.json"
    cached = json.loads(path.read_text()) if path.exists() else {}
    only = set(sys.argv[1:])
    for window, entry in index.items():
        if only and window not in only:
            continue
        images = [("SDO AIA 193 corona", SUN / entry["watch"]["aia193"]), ("SDO HMI magnetogram", SUN / entry["watch"]["hmi"])]
        result = sun_watch(images, entry["watchUtc"])
        if result is None:
            print(window, "no result")
            continue
        cached[window] = {**result, "utc": entry["watchUtc"]}
        print(window, result["outlook"], result["earthConnectedRisk"], "-", result["briefing"][:140])
        path.write_text(json.dumps(cached, indent=1))


if __name__ == "__main__":
    main()
