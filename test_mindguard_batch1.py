"""
UCANRR Safety UI Simulator - Mindguard Testset Edition

Reads unsafe-content-analysis-Mindguard-testset.csv, parses each row's
conversation from the `prompt` column, runs it through the safety API,
and writes results to mindguard_result_<YYYYMMDD_HHMMSS>.csv.

Usage:
    python test_mindguard_batch1.py                         # run all rows
    python test_mindguard_batch1.py --limit 10              # run first 10 rows
    python test_mindguard_batch1.py --start 5 --limit 10    # rows 5-14
    python test_mindguard_batch1.py --label unsafe_self_harm_risk
    python test_mindguard_batch1.py --label unsafe_harm_to_others
    python test_mindguard_batch1.py --mode last             # use only final user message
    python test_mindguard_batch1.py --csv path/to/file.csv  # different input file
"""

import argparse
import csv
import re
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

API_URL = "https://safetyapi-c6cqctbghub5f5d8.canadacentral-01.azurewebsites.net/safety/analyze_entry"
DEFAULT_CSV = "full-dataset-mindguard-test-set-training-data.csv"

OUTPUT_FIELDS = [
    # --- from input CSV ---
    "row_num",
    "label",
    "user_message",
    "annotator_labels",
    # --- parsing meta ---
    "parsed_msg_count",
    "entry_mode",
    # --- from API response ---
    "api_risk_tier",
    "api_risk_label",
    # flags
    "flag_suicidal_ideation",
    "flag_self_harm",
    "flag_other_harm",
    "flag_extreme_abuse",
    "flag_heated_argument",
    "flag_crisis_language",
    "flag_substance_use",
    "flag_weapon_access",
    "flag_child_safety",
    "flag_ambiguous_lethal",
    # recommendations
    "api_partner_share_policy",
    "api_therapist_share_policy",
    "api_show_crisis_banner",
    "api_show_crisis_resources",
    "api_suggested_ui_flow",
    "api_mark_as_urgent_for_therapist",
    "api_notes_for_therapist",
    # evaluation
    "tier_correct",
    # meta
    "api_error",
    "entry",
]


# ---------- Conversation parser ----------

_MSG_RE = re.compile(
    r"\{'role':\s*(?P<role>user|assistant),\s*'content':\s*"
    r"(?:'(?P<quoted>(?:[^'\\]|\\.)*)'|(?P<unquoted>[^}]*))\}",
    re.DOTALL,
)


def parse_conversation(prompt_str: str) -> List[Dict[str, str]]:
    """Parse the Mindguard prompt format into a list of {role, content} dicts."""
    messages = []
    for m in _MSG_RE.finditer(prompt_str):
        role = m.group("role")
        if m.group("quoted") is not None:
            content = m.group("quoted").replace("\\'", "'")
        else:
            content = (m.group("unquoted") or "").strip()
        if content:
            messages.append({"role": role, "content": content})
    return messages


def format_as_transcript(messages: List[Dict[str, str]]) -> str:
    """Format message list as a plain-text conversation transcript."""
    lines = []
    for msg in messages:
        prefix = "User" if msg["role"] == "user" else "Therapist"
        lines.append("%s: %s" % (prefix, msg["content"]))
    return "\n".join(lines)


# ---------- Label helpers ----------

_LABEL_TO_TIER = {
    "unsafe_self_harm_risk": 3,
    "unsafe_harm_to_others": 3,
    "safe": 0,
}


def expected_tier(label: str) -> int:
    return _LABEL_TO_TIER.get(label.strip(), -1)


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

def build_output_row(
    row_num: int,
    input_row: Dict[str, str],
    entry_text: str,
    parsed_msg_count: int,
    entry_mode: str,
    assessment: Optional[Dict[str, Any]],
    error: str,
) -> Dict[str, Any]:
    true_label = input_row.get("label", "").strip()

    out: Dict[str, Any] = {
        "row_num":          row_num,
        "label":            true_label,
        "user_message":     input_row.get("user_message", "").strip(),
        "annotator_labels": input_row.get("annotator_labels", ""),
        "parsed_msg_count": parsed_msg_count,
        "entry_mode":       entry_mode,
        "entry":            entry_text[:200],
        "api_error":        error,
        "tier_correct":     "",
    }

    if assessment:
        flags = assessment.get("flags") or {}
        rec   = assessment.get("recommendations") or {}
        pred_tier = assessment.get("risk_tier", -1)
        exp_tier  = expected_tier(true_label)
        out.update({
            "api_risk_tier":                    pred_tier,
            "api_risk_label":                   assessment.get("risk_label", ""),
            "flag_suicidal_ideation":           flags.get("has_suicidal_ideation", ""),
            "flag_self_harm":                   flags.get("has_self_harm", ""),
            "flag_other_harm":                  flags.get("has_other_harm", ""),
            "flag_extreme_abuse":               flags.get("has_extreme_abuse", ""),
            "flag_heated_argument":             flags.get("has_heated_argument", ""),
            "flag_crisis_language":             flags.get("has_crisis_language", ""),
            "flag_substance_use":               flags.get("mentions_substance_use", ""),
            "flag_weapon_access":               flags.get("mentions_weapon_access", ""),
            "flag_child_safety":                flags.get("mentions_child_safety_concern", ""),
            "flag_ambiguous_lethal":            flags.get("ambiguous_lethal_curiosity", ""),
            "api_partner_share_policy":         rec.get("partner_share_policy", ""),
            "api_therapist_share_policy":       rec.get("therapist_share_policy", ""),
            "api_show_crisis_banner":           rec.get("show_crisis_banner", ""),
            "api_show_crisis_resources":        rec.get("show_crisis_resources", ""),
            "api_suggested_ui_flow":            rec.get("suggested_ui_flow", ""),
            "api_mark_as_urgent_for_therapist": rec.get("mark_as_urgent_for_therapist", ""),
            "api_notes_for_therapist":          rec.get("notes_for_therapist", ""),
            "tier_correct":                     pred_tier == exp_tier,
        })
    else:
        for field in OUTPUT_FIELDS:
            if field not in out:
                out[field] = ""

    return out


# ---------- Main runner ----------

def run_rows(rows: List[Dict[str, str]], total_count: int, out_path: str, mode: str) -> None:
    error_count   = 0
    correct_count = 0
    evaluated     = 0

    import os
    os.makedirs("results", exist_ok=True)

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        writer.writeheader()

        for idx, row in enumerate(rows, start=1):
            true_label = row.get("label", "").strip()
            user_message = row.get("user_message", "").strip()

            # Build entry_text
            if mode == "last":
                entry_text       = user_message
                parsed_msg_count = 1
            else:
                messages         = parse_conversation(row.get("prompt", ""))
                parsed_msg_count = len(messages)
                entry_text       = format_as_transcript(messages) if messages else user_message

            print(
                "[%d/%d] label=%-28s mode=%s ..." % (idx, total_count, true_label, mode),
                end=" ", flush=True,
            )

            error_msg  = ""
            assessment = None
            try:
                assessment = analyze_entry(entry_text, user_hash="mindguard-row-%d" % idx)
                pred_tier  = assessment.get("risk_tier", "?")
                ui_flow    = assessment.get("recommendations", {}).get("suggested_ui_flow", "?")
                tier_ok    = pred_tier == expected_tier(true_label)
                if tier_ok:
                    correct_count += 1
                evaluated += 1
                print("OK  tier=%s  ui_flow=%s  %s" % (
                    pred_tier, ui_flow, "CORRECT" if tier_ok else "MISS"
                ))
            except Exception as exc:
                error_msg = str(exc)
                error_count += 1
                print("ERROR: %s" % error_msg)

            writer.writerow(build_output_row(
                idx, row, entry_text, parsed_msg_count, mode, assessment, error_msg
            ))

    print("\n" + "=" * 80)
    print("Run complete.")
    print("  Processed  : %d" % total_count)
    print("  Evaluated  : %d" % evaluated)
    print("  Tier correct: %d/%d  (%.1f%%)" % (
        correct_count, evaluated, 100 * correct_count / evaluated if evaluated else 0
    ))
    print("  Errors     : %d" % error_count)
    print("  Output     : %s" % out_path)
    print("=" * 80)


# ---------- CLI ----------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="UCANRR Safety UI Simulator - Mindguard Testset Edition"
    )
    parser.add_argument(
        "--csv", default=DEFAULT_CSV,
        help="Path to the Mindguard testset CSV (default: %s)" % DEFAULT_CSV,
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Maximum number of rows to process",
    )
    parser.add_argument(
        "--start", type=int, default=1,
        help="1-based row index to start from (default: 1)",
    )
    parser.add_argument(
        "--label", default=None,
        help="Filter rows by label value (e.g. unsafe_self_harm_risk, unsafe_harm_to_others)",
    )
    parser.add_argument(
        "--mode", choices=["full", "last"], default="full",
        help="full = whole conversation as entry_text (default); last = final user message only",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path  = "results/mindguard_result_%s.csv" % timestamp

    print("=" * 80)
    print("UCANRR Safety UI Simulator - Mindguard Testset Edition")
    print("=" * 80)
    print("Input  : %s" % args.csv)
    print("Output : %s" % out_path)
    print("API    : %s" % API_URL)

    rows = load_csv(args.csv)
    print("Total rows in CSV: %d" % len(rows))

    # Filter by label
    if args.label:
        rows = [r for r in rows if r.get("label", "").strip() == args.label]
        print("Filtered to label='%s': %d rows" % (args.label, len(rows)))

    # Apply start offset (1-based)
    start_idx = max(1, args.start) - 1
    rows = rows[start_idx:]

    # Apply limit
    if args.limit is not None:
        rows = rows[: args.limit]

    print("Running: %d entries  mode=%s\n" % (len(rows), args.mode))

    run_rows(rows, len(rows), out_path, args.mode)


if __name__ == "__main__":
    main()
