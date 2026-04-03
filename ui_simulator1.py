"""
UCANRR Safety UI Simulator - CSV Edition (Python 3.8+ compatible)

Reads safety_test_data.csv, runs each entry through the safety API,
and writes results to safety_test_result1_<YYYYMMDD_HHMMSS>.csv.

Usage:
    python ui_simulator1.py                        # run all rows
    python ui_simulator1.py --limit 10             # run first 10 rows
    python ui_simulator1.py --start 5 --limit 10   # rows 5-14
    python ui_simulator1.py --id JE0042            # run a specific entry by ID
    python ui_simulator1.py --band harm            # filter by ucanrr_band
    python ui_simulator1.py --csv path/to/file.csv # use a different CSV file
"""

import argparse
import csv
import sys
from datetime import datetime
from typing import Dict, Any, List, Optional

import requests

API_URL = "http://localhost:3000/safety/analyze_entry"
DEFAULT_CSV = "safety_test_data.csv"

# Columns written to the output CSV
OUTPUT_FIELDS = [
    # --- from input CSV ---
    "id",
    "ucanrr_score",
    "ucanrr_band",
    "score_bucket",
    "primary_label",
    "risk_level",
    "tags",
    "addressed_to",
    "style",
    # --- from API response ---
    "api_risk_tier",
    "api_risk_label",
    # flags
    "flag_crisis",
    "flag_self_harm",
    "flag_abuse_detected",
    "flag_substance_use",
    "flag_child_safety",
    "flag_heated_conflict",
    # recommendations
    "api_partner_share_policy",
    "api_therapist_share_policy",
    "api_show_crisis_banner",
    "api_show_crisis_resources",
    "api_suggested_ui_flow",
    "api_mark_as_urgent_for_therapist",
    "api_notes_for_therapist",
    # meta
    "api_error",
    "entry",
]


# ---------- API call ----------

def analyze_entry(entry_text: str, user_hash: Optional[str] = None) -> Dict[str, Any]:
    payload = {"entry_text": entry_text}
    if user_hash:
        payload["user_hash"] = user_hash

    resp = requests.post(API_URL, json=payload, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError("API error %s: %s" % (resp.status_code, resp.text))

    return resp.json()


# ---------- CSV loading ----------

def load_csv(path: str) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


# ---------- Build an output row ----------

def build_output_row(input_row: Dict[str, str], assessment: Optional[Dict[str, Any]], error: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "id":            input_row.get("id", ""),
        "ucanrr_score":  input_row.get("ucanrr_score", ""),
        "ucanrr_band":   input_row.get("ucanrr_band", ""),
        "score_bucket":  input_row.get("score_bucket", ""),
        "primary_label": input_row.get("primary_label", ""),
        "risk_level":    input_row.get("risk_level", ""),
        "tags":          input_row.get("tags", ""),
        "addressed_to":  input_row.get("addressed_to", ""),
        "style":         input_row.get("style", ""),
        "entry":         input_row.get("entry", "").strip(),
        "api_error":     error,
    }

    if assessment:
        flags = assessment.get("flags", {})
        rec   = assessment.get("recommendations", {})
        out.update({
            "api_risk_tier":                    assessment.get("risk_tier", ""),
            "api_risk_label":                   assessment.get("risk_label", ""),
            "flag_crisis":                      flags.get("crisis", ""),
            "flag_self_harm":                   flags.get("self_harm", ""),
            "flag_abuse_detected":              flags.get("abuse_detected", ""),
            "flag_substance_use":               flags.get("substance_use", ""),
            "flag_child_safety":                flags.get("child_safety", ""),
            "flag_heated_conflict":             flags.get("heated_conflict", ""),
            "api_partner_share_policy":         rec.get("partner_share_policy", ""),
            "api_therapist_share_policy":       rec.get("therapist_share_policy", ""),
            "api_show_crisis_banner":           rec.get("show_crisis_banner", ""),
            "api_show_crisis_resources":        rec.get("show_crisis_resources", ""),
            "api_suggested_ui_flow":            rec.get("suggested_ui_flow", ""),
            "api_mark_as_urgent_for_therapist": rec.get("mark_as_urgent_for_therapist", ""),
            "api_notes_for_therapist":          rec.get("notes_for_therapist", ""),
        })
    else:
        for field in OUTPUT_FIELDS:
            if field not in out:
                out[field] = ""

    return out


# ---------- Main runner ----------

def run_rows(rows: List[Dict[str, str]], total_count: int, out_path: str) -> None:
    error_count = 0

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        writer.writeheader()

        for idx, row in enumerate(rows, start=1):
            entry_id   = row.get("id", "???")
            entry_text = row.get("entry", "").strip()

            print(f"[{idx}/{total_count}] {entry_id}  band={row.get('ucanrr_band','?')}  risk_level={row.get('risk_level','?')} ...", end=" ", flush=True)

            error_msg  = ""
            assessment = None
            try:
                assessment = analyze_entry(entry_text, user_hash="csv-sim-user")
                print("OK  ui_flow=%s" % assessment.get("recommendations", {}).get("suggested_ui_flow", "?"))
            except Exception as exc:
                error_msg = str(exc)
                error_count += 1
                print("ERROR: %s" % error_msg)

            writer.writerow(build_output_row(row, assessment, error_msg))

    print("\n" + "=" * 80)
    print("Run complete.")
    print(f"  Processed : {total_count}")
    print(f"  Errors    : {error_count}")
    print(f"  Output    : {out_path}")
    print("=" * 80)


# ---------- CLI ----------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="UCANRR Safety UI Simulator - CSV Edition"
    )
    parser.add_argument(
        "--csv", default=DEFAULT_CSV,
        help="Path to the input CSV file (default: safety_test_data.csv)"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Maximum number of rows to process"
    )
    parser.add_argument(
        "--start", type=int, default=1,
        help="1-based row index to start from (default: 1)"
    )
    parser.add_argument(
        "--id", dest="entry_id", default=None,
        help="Run only the entry with this ID (e.g. JE0042)"
    )
    parser.add_argument(
        "--band", default=None,
        help="Filter rows by ucanrr_band value (e.g. harm, growth, neutral)"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path  = "safety_test_result1_%s.csv" % timestamp

    print("=" * 80)
    print("UCANRR Safety UI Simulator - CSV Edition")
    print("=" * 80)
    print(f"Input  : {args.csv}")
    print(f"Output : {out_path}")

    rows = load_csv(args.csv)
    print(f"Total rows in CSV: {len(rows)}")

    # Filter by ID
    if args.entry_id:
        rows = [r for r in rows if r.get("id") == args.entry_id]
        if not rows:
            print(f"No entry found with id={args.entry_id}")
            sys.exit(1)

    # Filter by band
    if args.band:
        rows = [r for r in rows if r.get("ucanrr_band", "").lower() == args.band.lower()]
        print(f"Filtered to band='{args.band}': {len(rows)} rows")

    # Apply start offset (1-based)
    start_idx = max(1, args.start) - 1
    rows = rows[start_idx:]

    # Apply limit
    if args.limit is not None:
        rows = rows[: args.limit]

    print(f"Running: {len(rows)} entries\n")

    run_rows(rows, len(rows), out_path)


if __name__ == "__main__":
    main()
