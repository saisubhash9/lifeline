"""Build the flare early-warning dataset from GOES-18 XRS products and the NOAA SEP list.

Inputs (downloaded to /tmp/goes18-xrs):
  xrsf-l2-flsum  mission-long flare summary (EVENT_START / EVENT_PEAK / EVENT_END records)
  xrsf-l2-flloc  flare locations (Stonyhurst heliographic lon/lat) keyed by flare_id
  xrsf-l2-avg1m  daily 1-minute X-ray flux, only for the replay windows (timeline curve)
  NOAA "Solar Proton Events Affecting the Earth Environment" table (labels)

Every feature is known at the flare's peak time: peak flux, flux integrated from start to
peak, rise time, pre-flare background, and location. Label = the NOAA SEP list names this
flare as the parent of a >=10 pfu proton event.

Outputs: data/flares/training.csv, data/flares/windows.json, data/noaa/xray/<window>.json
"""

from __future__ import annotations

import csv
import html
import json
import math
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from netCDF4 import Dataset

ROOT = Path(__file__).resolve().parent.parent
CACHE = Path("/tmp/goes18-xrs")
BASE = "https://data.ngdc.noaa.gov/platforms/solar-space-observing-satellites/goes/goes18/l2/data"
FLSUM = "xrsf-l2-flsum_science/sci_xrsf-l2-flsum_g18_s20220617_e20260925_v2-2-1.nc"
FLLOC = "xrsf-l2-flloc_science/sci_xrsf-l2-flloc_g18_s20220617_e20260925_v2-2-1.nc"
SEP_URL = "https://www.ngdc.noaa.gov/stp/space-weather/interplanetary-data/solar-proton-events/SEP%20page%20code.html"
EPOCH = datetime(2000, 1, 1, 12, tzinfo=timezone.utc)
MIN_PEAK = 1e-5  # M1.0 and above
MATCH_MINUTES = 30


def fetch(url: str, path: Path) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size < 1000:
        print("get", path.name, flush=True)
        subprocess.check_call(["curl", "-fsSL", "-o", str(path), url])
    return path


def stamp(seconds: float) -> datetime:
    return EPOCH + timedelta(seconds=float(seconds))


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def load_flares() -> list[dict]:
    path = fetch(f"{BASE}/{FLSUM}", CACHE / Path(FLSUM).name)
    ds = Dataset(path)
    times = ds["time"][:]
    status = ds["status"][:]
    ids = ds["flare_id"][:]
    flux = np.ma.filled(ds["xrsb_flux"][:].astype(float), np.nan)
    background = np.ma.filled(ds["background_flux"][:].astype(float), np.nan)
    integrated = np.ma.filled(ds["integrated_flux"][:].astype(float), np.nan)
    classes = ds["flare_class"][:]
    flares: dict[int, dict] = {}
    for i in range(len(times)):
        fid = int(ids[i])
        item = flares.setdefault(fid, {"id": fid})
        kind = str(status[i])
        if kind == "EVENT_START":
            item["start"] = stamp(times[i])
        elif kind == "EVENT_PEAK":
            item["peak"] = stamp(times[i])
            item["peak_flux"] = flux[i]
            item["background"] = background[i]
            item["fluence_to_peak"] = integrated[i]
            item["class"] = str(classes[i])
        elif kind == "EVENT_END":
            item["end"] = stamp(times[i])
    ds.close()
    return [item for item in flares.values() if "peak" in item and "start" in item and np.isfinite(item["peak_flux"])]


def load_locations() -> dict[int, tuple[float, float]]:
    path = fetch(f"{BASE}/{FLLOC}", CACHE / Path(FLLOC).name)
    ds = Dataset(path)
    ids = ds["flare_id"][:]
    hg = np.ma.filled(ds["flloc_hg"][:].astype(float), np.nan)
    found: dict[int, tuple[float, float]] = {}
    for i in range(len(ids)):
        lon, lat = hg[i]
        if np.isfinite(lon) and np.isfinite(lat):
            # flloc_hg longitude is 0-360 with east limb near 270; wrap to -180..180 (east negative).
            found.setdefault(int(ids[i]), (float((lon + 180.0) % 360.0 - 180.0), float(lat)))
    ds.close()
    return found


def load_seps() -> list[dict]:
    path = fetch(SEP_URL, CACHE / "sep.html")
    raw = path.read_text(encoding="latin-1")
    events = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", raw, flags=re.S | re.I):
        cells = [
            re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", cell))).strip()
            for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.S | re.I)
        ]
        if len(cells) < 6:
            continue
        begin = re.match(r"(\d{4}) (\d{2})/(\d{2}) (\d{4})", cells[0])
        if not begin or int(begin.group(1)) < 2022:
            continue
        year = int(begin.group(1))
        start = datetime(year, int(begin.group(2)), int(begin.group(3)), int(begin.group(4)[:2]), int(begin.group(4)[2:]), tzinfo=timezone.utc)
        pfu = float(re.sub(r"[^\d.]", "", cells[2]) or 0)
        flare = re.search(r"(\d{2})/(\d{2}) (\d{4})\s*$", cells[5])
        parent = None
        if flare:
            month = int(flare.group(1))
            parent_year = year - 1 if month > start.month + 6 else year
            parent = datetime(parent_year, month, int(flare.group(2)), int(flare.group(3)[:2]), int(flare.group(3)[2:]), tzinfo=timezone.utc)
        events.append({"start": start, "pfu": pfu, "location": cells[4], "flare": cells[5], "parent": parent})
    return events


def label(flares: list[dict], seps: list[dict]) -> None:
    for flare in flares:
        flare["sep"] = 0
        flare["sep_pfu"] = 0.0
        flare["onset_hours"] = None
    by_peak = sorted(flares, key=lambda item: item["peak"])
    matched = 0
    for event in seps:
        if event["parent"] is None:
            continue
        best = min(by_peak, key=lambda item: abs((item["peak"] - event["parent"]).total_seconds()))
        if abs((best["peak"] - event["parent"]).total_seconds()) <= MATCH_MINUTES * 60:
            best["sep"] = 1
            best["sep_pfu"] = max(best["sep_pfu"], event["pfu"])
            best["onset_hours"] = round((event["start"] - best["peak"]).total_seconds() / 3600, 2)
            matched += 1
    print(f"SEP events since 2022: {len(seps)}, matched to a GOES-18 flare: {matched}")


def features(flare: dict, location: tuple[float, float] | None) -> dict:
    lon, lat = location if location else (math.nan, math.nan)
    rise = max(1.0, (flare["peak"] - flare["start"]).total_seconds() / 60)
    return {
        "flare_id": flare["id"],
        "peak_utc": iso(flare["peak"]),
        "start_utc": iso(flare["start"]),
        "class": flare["class"],
        "log_peak": round(math.log10(flare["peak_flux"]), 4),
        "log_fluence": round(math.log10(max(flare["fluence_to_peak"], 1e-6)), 4) if np.isfinite(flare["fluence_to_peak"]) else -3.0,
        "log_rise_min": round(math.log10(rise), 4),
        "log_background": round(math.log10(max(flare["background"], 1e-9)), 4) if np.isfinite(flare["background"]) else -6.5,
        "has_location": int(location is not None),
        "lon": round(lon, 2) if location else "",
        "lat": round(lat, 2) if location else "",
        "sep": flare["sep"],
        "sep_pfu": flare["sep_pfu"],
        "onset_hours": flare["onset_hours"] if flare["onset_hours"] is not None else "",
    }


def xray_series(start: str, end: str) -> list[dict]:
    """5-minute maxima of XRS-B from the daily 1-minute science files."""
    day = datetime.fromisoformat(start)
    stop = datetime.fromisoformat(end)
    rows: dict[str, float | None] = {}
    while day <= stop:
        tag = day.strftime("%Y%m%d")
        listing = subprocess.check_output(["curl", "-fsSL", f"{BASE}/xrsf-l2-avg1m_science/{tag[:4]}/{tag[4:6]}/"], text=True)
        names = sorted(set(re.findall(rf"sci_xrsf-l2-avg1m_g18_d{tag}_v[0-9-]+\.nc", listing)))
        if not names:
            raise SystemExit(f"no XRS file for {tag}")
        path = fetch(f"{BASE}/xrsf-l2-avg1m_science/{tag[:4]}/{tag[4:6]}/{names[-1]}", CACHE / names[-1])
        with Dataset(path) as ds:
            times = ds["time"][:]
            flux = np.ma.filled(ds["xrsb_flux"][:].astype(float), np.nan)
            flags = np.ma.filled(ds["xrsb_flag"][:], 0) if "xrsb_flag" in ds.variables else np.zeros(len(times))
        for seconds, value, flag in zip(times, flux, flags):
            moment = stamp(seconds)
            bucket = moment.replace(minute=moment.minute - moment.minute % 5, second=0, microsecond=0)
            key = iso(bucket)
            good = np.isfinite(value) and value > 0 and int(flag) == 0
            if good:
                rows[key] = max(rows.get(key) or 0.0, float(value))
            else:
                rows.setdefault(key, None)
        day += timedelta(days=1)
    return [{"t": key, "xrsb": (None if value is None else float(f"{value:.3e}"))} for key, value in sorted(rows.items())]


def main() -> None:
    flares = load_flares()
    locations = load_locations()
    seps = load_seps()
    label(flares, seps)
    strong = [flare for flare in flares if flare["peak_flux"] >= MIN_PEAK]
    rows = [features(flare, locations.get(flare["id"])) for flare in sorted(strong, key=lambda item: item["peak"])]
    out = ROOT / "data" / "flares"
    out.mkdir(parents=True, exist_ok=True)
    with (out / "training.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"flares >= M1: {len(rows)}, with location: {sum(r['has_location'] for r in rows)}, SEP-positive: {sum(r['sep'] for r in rows)}")

    windows = {}
    xray_dir = ROOT / "data" / "noaa" / "xray"
    xray_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted((ROOT / "data" / "noaa").glob("*.json")):
        if path.name == "manifest.json":
            continue
        record = json.loads(path.read_text())
        first, last = record["samples"][0]["t"], record["samples"][-1]["t"]
        inside = [row for row in rows if first <= row["peak_utc"] <= last]
        windows[record["id"]] = inside
        series = xray_series(record["start"], record["end"])
        series = [item for item in series if first <= item["t"] <= last]
        (xray_dir / f"{record['id']}.json").write_text(json.dumps({"id": record["id"], "product": "xrsf-l2-avg1m (5-min max of XRS-B, W/m2)", "samples": series}))
        print(record["id"], "flares >= M1 in window:", len(inside), "xray samples:", len(series))
    (out / "windows.json").write_text(json.dumps(windows, indent=1))


if __name__ == "__main__":
    main()
