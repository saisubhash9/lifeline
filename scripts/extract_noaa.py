"""Build GOES-18 replay series from NCEI sgps-l2-avg5m files.

The archived integral variable AvgIntProtonFlux is the P11 >500 MeV channel.
The S-scale uses ≥10 MeV integral flux, which this product does not store.
Each sample is the westward-looking differential-channel sum at and above 10 MeV:
flux (protons/cm^2/sr/keV/s) times channel width (keV). The channel that
straddles 10 MeV contributes only the fraction of its width above 10 MeV.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from netCDF4 import Dataset

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "noaa"
CACHE = Path("/tmp/goes18-sgps")
BASE = (
    "https://data.ngdc.noaa.gov/platforms/solar-space-observing-satellites/"
    "goes/goes18/l2/data/sgps-l2-avg5m"
)
EPOCH = datetime(2000, 1, 1, 12, 0, tzinfo=timezone.utc)

WINDOWS = [
    {
        "id": "may2024",
        "label": "May 9–13, 2024",
        "why": "Two closely spaced S2 peaks. Tests alert sharing and whether nodes resume too soon.",
        "start": "2024-05-09",
        "end": "2024-05-13",
        "defaultOutageHours": 24,
        "catalog": [
            {"utc": "2024-05-10T17:45:00Z", "pfu": 208, "scale": "S2"},
            {"utc": "2024-05-11T09:10:00Z", "pfu": 116, "scale": "S2"},
        ],
    },
    {
        "id": "mar2024",
        "label": "Mar 22–25, 2024",
        "why": "Near the S3 threshold. Tests protection without excessive downtime.",
        "start": "2024-03-22",
        "end": "2024-03-25",
        "defaultOutageHours": 24,
        "catalog": [{"utc": "2024-03-23T18:20:00Z", "pfu": 956, "scale": "S2"}],
    },
    {
        "id": "jun2024",
        "label": "Jun 7–10, 2024",
        "why": "Crosses into S3. Tests response time and checkpointing on a stronger rise.",
        "start": "2024-06-07",
        "end": "2024-06-10",
        "defaultOutageHours": 24,
        "catalog": [{"utc": "2024-06-08T08:00:00Z", "pfu": 1030, "scale": "S3"}],
    },
    {
        "id": "oct2024",
        "label": "Oct 8–12, 2024",
        "why": "Onset one day and a peak the next. Tests prolonged protection, deadlines, and recovery.",
        "start": "2024-10-08",
        "end": "2024-10-12",
        "defaultOutageHours": 24,
        "catalog": [{"utc": "2024-10-10T15:15:00Z", "pfu": 1810, "scale": "S3"}],
    },
    {
        "id": "jan2026",
        "label": "Jan 17–22, 2026",
        "why": "Severe S4 stress case for the safety rule and service continuity.",
        "start": "2026-01-17",
        "end": "2026-01-22",
        "defaultOutageHours": 24,
        "catalog": [{"utc": "2026-01-19T19:15:00Z", "pfu": 37000, "scale": "S4"}],
    },
    {
        "id": "quiet2024",
        "label": "Jul 18–22, 2024",
        "why": "Quiet control. Tests unnecessary shutdowns while the channel sum stays below the S1 threshold of 10 pfu.",
        "start": "2024-07-18",
        "end": "2024-07-22",
        "defaultOutageHours": 0,
        "catalog": [],
    },
]


def days_between(start: str, end: str) -> list[str]:
    cursor = datetime.fromisoformat(start)
    stop = datetime.fromisoformat(end)
    found = []
    while cursor <= stop:
        found.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return found


def archive_name(day: str) -> tuple[str, str]:
    url = f"{BASE}/{day[:4]}/{day[4:6]}/"
    html = subprocess.check_output(["curl", "-fsSL", url], text=True)
    names = sorted(set(re.findall(rf"sci_sgps-l2-avg5m_g18_d{day}_v[0-9-]+\.nc", html)))
    if not names:
        raise SystemExit(f"no GOES-18 file for {day}")
    return url + names[-1], names[-1]


def download(day: str) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    url, name = archive_name(day)
    path = CACHE / name
    if not path.exists() or path.stat().st_size < 1000:
        print("get", name, flush=True)
        subprocess.check_call(["curl", "-fsSL", "-o", str(path), url])
    return path


def channel_sum(ds: Dataset) -> tuple[list[str], list[float], list[int], list[int], str]:
    times = [
        (EPOCH + timedelta(seconds=float(seconds))).strftime("%Y-%m-%dT%H:%M:%SZ")
        for seconds in ds.variables["time"][:]
    ]
    yaw = np.array(ds.variables["yaw_flip_flag"][:])
    flux = np.array(ds.variables["AvgDiffProtonFlux"][:], dtype=float)
    lo = np.array(ds.variables["DiffProtonLowerEnergy"][:]) / 1000.0
    hi = np.array(ds.variables["DiffProtonUpperEnergy"][:]) / 1000.0
    valid = np.array(ds.variables["DiffValidL1bSamplesInAvg"][:])
    dtc = np.array(ds.variables["DiffDQFdtcSum"][:])
    oob = np.array(ds.variables["DiffDQFoobSum"][:])
    err = np.array(ds.variables["DiffDQFerrSum"][:])
    pfu, valids, dtc_max, oob_max, err_max = [], [], [], [], []
    for index, _stamp in enumerate(times):
        unit = 1 if int(yaw[index]) == 1 else 0
        total = 0.0
        used = False
        min_valid = None
        dead = 0
        band = 0
        error = 0
        for channel in range(flux.shape[2]):
            lower, upper = float(lo[unit, channel]), float(hi[unit, channel])
            if upper <= 10:
                continue
            count = int(valid[index, unit, channel])
            if count < 0:
                count = 0
            min_valid = count if min_valid is None else min(min_valid, count)
            dead = max(dead, int(dtc[index, unit, channel]))
            band = max(band, int(oob[index, unit, channel]))
            error = max(error, int(err[index, unit, channel]))
            sample = float(flux[index, unit, channel])
            if not np.isfinite(sample) or sample < 0 or sample > 1e6:
                continue
            fraction = 1.0 if lower >= 10 else (upper - 10) / (upper - lower)
            total += sample * (upper - lower) * 1000.0 * fraction
            used = True
        if not used:
            pfu.append(None)
            valids.append(0)
            dtc_max.append(dead)
            oob_max.append(band)
            err_max.append(error)
        else:
            pfu.append(round(total, 3))
            valids.append(0 if min_valid is None else min_valid)
            dtc_max.append(dead)
            oob_max.append(band)
            err_max.append(error)
    return times, pfu, valids, dtc_max, oob_max, err_max, str(ds.id)


def nearest(samples: list[dict], stamp: str) -> dict:
    target = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    best = min(
        samples,
        key=lambda sample: abs(datetime.fromisoformat(sample["t"].replace("Z", "+00:00")) - target),
    )
    return best


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = []
    for spec in WINDOWS:
        samples = []
        files = []
        for day in days_between(spec["start"], spec["end"]):
            path = download(day)
            files.append(path.name)
            with Dataset(path) as dataset:
                times, pfu, valids, dtc_max, oob_max, err_max, _file_id = channel_sum(dataset)
            for stamp, value, count, dead, band, error in zip(times, pfu, valids, dtc_max, oob_max, err_max):
                if samples and samples[-1]["t"] == stamp:
                    continue
                samples.append(
                    {"t": stamp, "pfu": value, "valid": count, "dtc": dead, "oob": band, "err": error}
                )
        finite = [sample["pfu"] for sample in samples if sample["pfu"] is not None]
        peak_value = max(finite)
        peak = next(sample for sample in samples if sample["pfu"] == peak_value)
        catalog = []
        for item in spec["catalog"]:
            observed = nearest(samples, item["utc"])
            catalog.append(
                {
                    **item,
                    "extractedPfu": observed["pfu"],
                    "extractedUtc": observed["t"],
                }
            )
        record = {
            "id": spec["id"],
            "label": spec["label"],
            "why": spec["why"],
            "start": spec["start"],
            "end": spec["end"],
            "defaultOutageHours": spec["defaultOutageHours"],
            "satellite": "GOES-18",
            "product": "sgps-l2-avg5m",
            "files": files,
            "method": (
                "Westward-looking AvgDiffProtonFlux summed for channel widths at and above 10 MeV. "
                "AvgIntProtonFlux in these files is the >500 MeV channel and is not used."
            ),
            "peakUtc": peak["t"],
            "peakPfu": peak["pfu"],
            "maxPfu": peak_value,
            "catalog": catalog,
            "samples": samples,
        }
        (OUT / f"{spec['id']}.json").write_text(json.dumps(record))
        manifest.append(
            {
                "id": spec["id"],
                "label": spec["label"],
                "peakUtc": peak["t"],
                "peakPfu": peak["pfu"],
                "maxPfu": peak_value,
                "samples": len(samples),
                "catalog": catalog,
            }
        )
        print(spec["id"], len(samples), "peak", peak["pfu"], peak["t"], "catalog", catalog, flush=True)
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
