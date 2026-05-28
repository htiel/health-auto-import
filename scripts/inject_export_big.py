"""Inject manual HAE export data into HA config entry for state persistence.

Reads exports from the BIG ``Export`` folder (top-level per-category JSON
files plus a ``Workouts-*`` subdirectory of per-workout JSONs) and writes
``latest_records`` into the HA config entry.

Uses ijson for the huge ECG file (829 MB) to avoid OOM — only the metadata
of every record is streamed, then the top-N records by start time are
re-loaded in full so the voltage waveform survives.

For workouts, each per-workout JSON is small (KB), so we walk the
subdirectory directly.
"""
from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from typing import Any

import ijson  # streaming JSON parser


def _to_jsonable(obj: Any) -> Any:
    """Convert ijson Decimals (and nested containers) to JSON-safe values."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_jsonable(v) for v in obj]
    return obj

BASE = r"C:\Users\leith\OneDrive\LCARS\Health\Export"
CONFIG_PATH = r"\\homeassistant.local\config\.storage\core.config_entries"

# How many records to inject per tool (capped to keep latest_records < 1 MB).
KEEP_ECG = 10
KEEP_WORKOUTS = 20
KEEP_HRN = 20
KEEP_MEDICATIONS = 50
KEEP_METRIC_POINTS = 60  # per metric

ECG_VOLTAGE_DOWNSAMPLE = 2000  # cap voltage_uv to this many points


# ---- ECG streaming -----------------------------------------------------------

def stream_ecg_topn(path: str, n: int) -> list[dict[str, Any]]:
    """Stream the huge ECG export and return the N most recent records.

    Two passes: (1) collect (start, byte_offset, record_index) tuples by
    streaming the top-level array, (2) pick top-N by start, (3) re-parse
    the file to extract just those records.
    """
    sz = os.path.getsize(path)
    print(f"  ECG file: {sz/1024/1024:.1f} MB — streaming…")
    # Single-pass: collect each record as parsed dict; keep only essentials.
    # We need start + voltage_uv + classification + meta. The record itself
    # may be ~2-5 MB (15360 floats). We can't keep all 410. Strategy:
    #   - Stream every record as a dict
    #   - Downsample voltage_uv immediately to ECG_VOLTAGE_DOWNSAMPLE points
    #   - Keep min-heap of N most recent by start
    import heapq

    heap: list[tuple[str, int, dict[str, Any]]] = []
    counter = 0
    with open(path, "rb") as fh:
        # The ECG JSON is { "data": { "ecg": [ ... ], "metrics": [] } }
        for rec in ijson.items(fh, "data.ecg.item"):
            counter += 1
            if counter % 50 == 0:
                print(f"    streamed {counter} ECG records, heap={len(heap)}")
            # Downsample voltage_uv before holding in memory
            v = rec.get("voltage_uv")
            if isinstance(v, list) and len(v) > ECG_VOLTAGE_DOWNSAMPLE:
                step = len(v) // ECG_VOLTAGE_DOWNSAMPLE
                rec["voltage_uv"] = v[::step][:ECG_VOLTAGE_DOWNSAMPLE]
            start = str(rec.get("start", ""))
            # Use counter as tiebreaker to avoid dict comparison
            item = (start, counter, rec)
            if len(heap) < n:
                heapq.heappush(heap, item)
            else:
                heapq.heappushpop(heap, item)
    print(f"  ECG: scanned {counter} records, kept top {len(heap)}")
    # Return newest first
    return [rec for _, _, rec in sorted(heap, reverse=True)]


# ---- Workouts ----------------------------------------------------------------

def load_workouts(workouts_dir: str, n: int) -> list[dict[str, Any]]:
    """Walk the Workouts-* subdirectory and return the N most recent."""
    all_wo: list[dict[str, Any]] = []
    files = sorted(os.listdir(workouts_dir))
    json_files = [f for f in files if f.endswith(".json")]
    print(f"  Workouts: {len(json_files)} per-workout JSON files")
    for jf in json_files:
        fp = os.path.join(workouts_dir, jf)
        try:
            with open(fp, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception as exc:
            print(f"    skip {jf}: {exc}")
            continue
        wk_list = (d.get("data") or {}).get("workouts") or []
        all_wo.extend(wk_list)
    sorted_wo = sorted(all_wo, key=lambda r: str(r.get("start", "")), reverse=True)
    kept = sorted_wo[:n]
    print(f"  Workouts: scanned {len(all_wo)} records, kept top {len(kept)}")
    if kept:
        print(f"    newest: {kept[0].get('start','?')[:25]} - {kept[0].get('name','?')}")
    return kept


# ---- Metrics / HRN / Medications --------------------------------------------

def load_metrics(path: str, points_per_metric: int) -> list[dict[str, Any]]:
    """Load the metrics export and return latest N points per metric bucket."""
    print(f"  Metrics file: {os.path.getsize(path)/1024/1024:.1f} MB — loading…")
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    buckets = (d.get("data") or {}).get("metrics") or []
    out: list[dict[str, Any]] = []
    for b in buckets:
        name = b.get("name", "")
        if not name:
            continue
        pts = b.get("data") or []
        sorted_pts = sorted(pts, key=lambda p: str(p.get("date", "")), reverse=True)
        kept = sorted_pts[:points_per_metric]
        if not kept:
            continue
        out.append({"name": name, "units": b.get("units", ""), "data": kept})
    total_pts = sum(len(b["data"]) for b in out)
    print(f"  Metrics: {len(out)} buckets, {total_pts} total points")
    return out


def load_simple(path: str, key: str, sort_key: str, n: int) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    items = (d.get("data") or {}).get(key) or []
    sorted_items = sorted(
        items, key=lambda r: str(r.get(sort_key, "") or ""), reverse=True
    )
    kept = sorted_items[:n]
    print(f"  {key}: scanned {len(items)} records, kept top {len(kept)}")
    return kept


# ---- Inject ------------------------------------------------------------------

def inject_into_config_entry(records: dict[str, list]) -> None:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)
    entries = config.get("data", {}).get("entries", [])
    found = False
    for entry in entries:
        if entry.get("domain") == "health_auto_import":
            opts = entry.setdefault("options", {})
            opts["latest_records"] = records
            found = True
            print(f"\n  Injected into config entry "
                  f"(entry_id: {entry.get('entry_id', '?')[:8]}…)")
            break
    if not found:
        print("ERROR: health_auto_import config entry not found!")
        sys.exit(1)
    raw = json.dumps(config, ensure_ascii=False, indent=2)
    with open(CONFIG_PATH, "w", encoding="utf-8", newline="") as f:
        f.write(raw)
    print(f"  Config file size: {os.path.getsize(CONFIG_PATH):,} bytes")


def find_one(prefix: str) -> str | None:
    for f in os.listdir(BASE):
        if f.startswith(prefix):
            return os.path.join(BASE, f)
    return None


def main() -> None:
    print(f"Loading exports from {BASE}\n")

    records: dict[str, list] = {}

    ecg_path = find_one("ECG-")
    if ecg_path:
        records["ecg"] = stream_ecg_topn(ecg_path, KEEP_ECG)

    workouts_dir = find_one("Workouts-")
    if workouts_dir and os.path.isdir(workouts_dir):
        records["workouts"] = load_workouts(workouts_dir, KEEP_WORKOUTS)

    hrn_path = find_one("HeartNotifications-")
    if hrn_path:
        records["heart_notifications"] = load_simple(
            hrn_path, "heartRateNotifications", "start", KEEP_HRN,
        )

    med_path = find_one("Medications-")
    if med_path:
        records["medications"] = load_simple(
            med_path, "medications", "scheduledDate", KEEP_MEDICATIONS,
        )

    met_path = find_one("Metrics-")
    if met_path:
        records["health_metrics"] = load_metrics(met_path, KEEP_METRIC_POINTS)

    # ijson returns Decimal for numbers — convert to float for JSON output.
    records = {k: _to_jsonable(v) for k, v in records.items()}

    # Rough size check
    approx = len(json.dumps(records))
    print(f"\nlatest_records approx size: {approx/1024:.1f} KB "
          f"({approx/1024/1024:.2f} MB)")

    print("\nInjecting into HA config entry…")
    inject_into_config_entry(records)
    print("\n*** DONE — restart HA to load the injected data ***")


if __name__ == "__main__":
    main()
