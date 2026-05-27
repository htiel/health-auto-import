"""Inject manual HAE export data into HA config entry for state persistence.

Reads all JSON exports from the ManExport folder, builds latest_records
for each tool coordinator, and writes them into the HA config entry so
sensors have data on restart even when the HAE server is offline.
"""
import json
import os
import sys
from io import BytesIO

BASE = r"C:\Users\leith\OneDrive\LCARS\Health\ManExport"
CONFIG_PATH = r"\\homeassistant.local\config\.storage\core.config_entries"

def load_all_exports():
    """Load and merge all export JSON files."""
    all_ecg = []
    all_workouts = []
    all_hrn = []
    all_medications = []
    all_metrics = {}  # name -> {name, units, data: [points]}

    for d in sorted(os.listdir(BASE)):
        dp = os.path.join(BASE, d)
        if not os.path.isdir(dp):
            continue
        for jf in os.listdir(dp):
            if not jf.endswith(".json"):
                continue
            fp = os.path.join(dp, jf)
            with open(fp, encoding="utf-8") as fh:
                data = json.load(fh)
            dd = data.get("data", {})

            all_ecg.extend(dd.get("ecg", []))
            all_workouts.extend(dd.get("workouts", []))
            all_hrn.extend(dd.get("heartRateNotifications", []))
            all_medications.extend(dd.get("medications", []))

            for bucket in dd.get("metrics", []):
                name = bucket.get("name", "")
                if not name:
                    continue
                if name not in all_metrics:
                    all_metrics[name] = {
                        "name": name,
                        "units": bucket.get("units", ""),
                        "data": [],
                    }
                pts = bucket.get("data", [])
                if isinstance(pts, list):
                    all_metrics[name]["data"].extend(pts)

    return all_ecg, all_workouts, all_hrn, all_medications, all_metrics


def build_latest_records(all_ecg, all_workouts, all_hrn, all_medications, all_metrics):
    """Build the latest_records dict for each tool coordinator."""
    records = {}

    # ECG: keep last 5 by start time (sensors read max by start)
    if all_ecg:
        sorted_ecg = sorted(all_ecg, key=lambda r: r.get("start", ""), reverse=True)
        records["ecg"] = sorted_ecg[:5]
        latest = sorted_ecg[0]
        print(f"  ecg: {len(sorted_ecg)} total, injecting last 5")
        print(f"    latest: {latest.get('start', '?')[:25]} - {latest.get('classification', '?')}")

    # Workouts: keep last 10 by start time
    if all_workouts:
        sorted_wo = sorted(all_workouts, key=lambda r: r.get("start", ""), reverse=True)
        records["workouts"] = sorted_wo[:10]
        latest = sorted_wo[0]
        print(f"  workouts: {len(sorted_wo)} total, injecting last 10")
        print(f"    latest: {latest.get('start', '?')[:25]} - {latest.get('name', '?')}")

    # Heart notifications: keep last 5
    if all_hrn:
        sorted_hrn = sorted(all_hrn, key=lambda r: r.get("start", ""), reverse=True)
        records["heart_notifications"] = sorted_hrn[:5]
        print(f"  heart_notifications: {len(sorted_hrn)} total, injecting last 5")

    # Medications: keep last 10 by scheduledDate
    if all_medications:
        sorted_med = sorted(
            all_medications,
            key=lambda r: r.get("scheduledDate", "") or "",
            reverse=True,
        )
        records["medications"] = sorted_med[:10]
        latest = sorted_med[0]
        print(f"  medications: {len(sorted_med)} total, injecting last 10")
        print(f"    latest: {latest.get('scheduledDate', '?')[:25]} - {latest.get('displayText', '?')}")

    # Health metrics: keep each metric bucket with its latest N data points
    if all_metrics:
        metric_buckets = []
        for name, bucket in sorted(all_metrics.items()):
            points = bucket["data"]
            # Sort by date descending, keep latest 7 days of points
            sorted_pts = sorted(points, key=lambda p: p.get("date", ""), reverse=True)
            # Keep up to 30 latest points per metric
            kept = sorted_pts[:30]
            if kept:
                metric_buckets.append({
                    "name": bucket["name"],
                    "units": bucket["units"],
                    "data": kept,
                })
        records["health_metrics"] = metric_buckets
        total_pts = sum(len(b["data"]) for b in metric_buckets)
        print(f"  health_metrics: {len(metric_buckets)} metrics, {total_pts} data points")

    return records


def inject_into_config_entry(records):
    """Write latest_records into the HA config entry."""
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)

    entries = config.get("data", {}).get("entries", [])
    found = False
    for entry in entries:
        if entry.get("domain") == "health_auto_import":
            opts = entry.setdefault("options", {})
            opts["latest_records"] = records
            found = True
            print(f"\n  Injected into config entry (entry_id: {entry.get('entry_id', '?')[:8]}...)")
            break

    if not found:
        print("ERROR: health_auto_import config entry not found!")
        sys.exit(1)

    # Write back — use UTF-8 without BOM
    raw = json.dumps(config, ensure_ascii=False, indent=2)
    with open(CONFIG_PATH, "w", encoding="utf-8", newline="") as f:
        f.write(raw)
    print("  Config entry updated successfully")
    print(f"  File size: {os.path.getsize(CONFIG_PATH):,} bytes")


def main():
    print("Loading exports from", BASE)
    all_ecg, all_workouts, all_hrn, all_medications, all_metrics = load_all_exports()

    print(f"\nTotals: {len(all_ecg)} ECG, {len(all_workouts)} workouts, "
          f"{len(all_hrn)} HRN, {len(all_medications)} meds, "
          f"{len(all_metrics)} metric types")

    print("\nBuilding latest_records:")
    records = build_latest_records(all_ecg, all_workouts, all_hrn, all_medications, all_metrics)

    print("\nInjecting into HA config entry...")
    inject_into_config_entry(records)

    print("\n*** DONE — restart HA to load the injected data ***")


if __name__ == "__main__":
    main()
