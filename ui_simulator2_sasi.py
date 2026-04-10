"""
UCANRR + SASI Safety UI Simulator (Python 3.8+)

Reads sasi_safety_test_data.csv, runs each entry through the SASI-gated
safety API (ucanrr_sasi_safety_eval_api.py), and writes a full result CSV.

Usage:
    python ui_simulator2_sasi.py                        # run all rows
    python ui_simulator2_sasi.py --limit 10             # first 10 rows
    python ui_simulator2_sasi.py --start 5 --limit 10  # rows 5-14
    python ui_simulator2_sasi.py --id JE0042            # single entry by ID
    python ui_simulator2_sasi.py --band harm            # filter by ucanrr_band
    python ui_simulator2_sasi.py --label ambiguous_monitor
    python ui_simulator2_sasi.py --csv path/to/file.csv

CSV column layout (4 blocks — see docs/OUTPUT_COLUMNS.md for full reference):
    Block 1  id + ucanrr_* / benchmark columns (from input CSV, not the API)
    Block 2  in_*           request parameters echoed for traceability
    Block 3  sasi_*         from SasiResult via assessment["sasi"]
    Block 4  api_* / flag_* merged product response (OpenAI or SASI-derived)

Minimal curl proof (run while API is up):

    curl -s -X POST http://localhost:3000/safety/analyze_entry \\
      -H "Content-Type: application/json" \\
      -d '{"entry_text": "I feel overwhelmed today.", "user_id": "curl-test-01"}' \\
      | python -m json.tool

    Expected: JSON with a non-empty "sasi" object containing sasi_risk_level,
    sasi_flag_crisis, etc.  If "sasi" is absent on HTTP 200, check API stderr
    for the [SASI first-response export] line — the wrong server module may be
    running (should be ucanrr_sasi_safety_eval_api:app, not ucanrr_safety_eval_api:app).
"""

import argparse
import csv
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

API_URL     = "http://localhost:3000/safety/analyze_entry"
DEFAULT_CSV = "sasi_safety_test_data.csv"


# ---------- Output columns ----------

# Full column reference: docs/OUTPUT_COLUMNS.md
# Authority tags used below:
#   benchmark  = from input CSV (not recomputed by API)
#   in         = request param echoed
#   sasi       = from SasiResult via assessment["sasi"]; present in every 200
#   api-merged = SASI when gate_blocked=TRUE, OpenAI when FALSE
#   api-openai = OpenAI only; empty/None when sasi_gate_blocked=TRUE

OUTPUT_FIELDS = [
    # ── Block 1: benchmark / input metadata ──────────────────────────────────
    "id",               # benchmark
    "ucanrr_score",     # benchmark
    "ucanrr_band",      # benchmark
    "score_bucket",     # benchmark
    "primary_label",    # benchmark
    "risk_level",       # benchmark  (pre-assigned; NOT the live SASI risk level)
    "tags",             # benchmark
    "addressed_to",     # benchmark
    "style",            # benchmark

    # ── Block 2: request parameters ──────────────────────────────────────────
    "in_user_id",
    "in_partner_id",
    "in_session_id",
    "in_share_requested",

    # ── Block 3: SASI fields ─────────────────────────────────────────────────
    # All read from assessment["sasi"] — single stable path, always present.
    # No OpenAI values appear in this block.
    "sasi_risk_level",               # result.risk_level (enum → .name.lower())
    "sasi_crisis_detected",          # result.is_crisis
    "sasi_human_oversight_required", # result.human_oversight_required
    "sasi_oversight_type",           # result.oversight_type (null if none)
    "sasi_gate_blocked",             # True iff OpenAI call was skipped
    "share_allowed",                 # SASI share-gate decision
    "share_blocked_reason",          # null when share_allowed=True
    "mandatory_reporting_flag",      # result.mandatory_reporting_flag
    "mandatory_reporting_categories",# semicolon-joined list
    "sasi_envelope_id",              # result.envelope.run_id
    "policy_hash",                   # result.envelope.policy_hash
    "sasi_envelope",                 # envelope JSON (no user text)
    "sasi_flag_crisis",              # result.is_crisis
    "sasi_flag_human_oversight",     # result.human_oversight_required
    "sasi_flag_should_block",        # result.should_block
    "sasi_flag_pii_detected",        # result.pii_detected
    "sasi_flag_show_hotline",        # result.show_hotline
    "sasi_flag_operator_crisis",     # result.operator_crisis
    "sasi_flag_parent_alert",        # result.parent_alert_flag
    "sasi_flag_human_review",        # result.human_review_flag
    "sasi_flag_action_rationale",    # result.action_rationale
    "sasi_flag_principle_triggered", # result.principle_triggered

    # ── Block 4: API / merged product response ────────────────────────────────
    # api_risk_tier / api_risk_label: api-merged (see authority table in AUDIT.md)
    # All api_* / flag_* below: api-openai — empty when sasi_gate_blocked=TRUE
    # api_partner_share_policy may diverge from share_allowed; no server-side
    # reconciliation — see docs/SAFETY_PIPELINE_AUDIT.md.
    "api_risk_tier",
    "api_risk_label",
    "flag_suicidal_ideation",
    "flag_self_harm",
    "flag_other_harm",
    "flag_extreme_abuse",
    "flag_heated_argument",
    "flag_crisis_language",
    "flag_substance_use",
    "flag_weapon_access",
    "flag_child_safety",
    "flag_ambiguous_lethal_curiosity",
    "api_partner_share_policy",
    "api_therapist_share_policy",
    "api_show_crisis_banner",
    "api_show_crisis_resources",
    "api_suggested_ui_flow",
    "api_mark_as_urgent_for_therapist",
    "api_notes_for_therapist",
    "api_explanation",

    # ── Meta ─────────────────────────────────────────────────────────────────
    "api_error",
    "entry",
]


# ---------- API call ----------

def analyze_entry(row: Dict[str, str]) -> Dict[str, Any]:
    # Prefer the dedicated user_id column; fall back to id or in_user_id from the
    # row, then send an empty string so the API applies its SASI_DEFAULT_USER_ID
    # fallback.  Never omit user_id from the payload—keeps the shape predictable.
    user_id = str(
        row.get("user_id") or row.get("id") or row.get("in_user_id") or ""
    ).strip()

    payload: Dict[str, Any] = {
        "entry_text":      row["entry"],
        "user_id":         user_id or None,   # None → server applies default
        "partner_id":      row.get("partner_id") or None,
        "session_id":      row.get("session_id") or None,
        "share_requested": row.get("share_requested", "false").lower() == "true",
    }
    resp = requests.post(API_URL, json=payload, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError("API %s: %s" % (resp.status_code, resp.text[:300]))
    return resp.json()


# ---------- CSV loading ----------

def load_csv(path: str) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------- Build output row ----------

def build_output_row(
    input_row: Dict[str, str],
    assessment: Optional[Dict[str, Any]],
    error: str,
) -> Dict[str, Any]:

    out: Dict[str, Any] = {
        # input metadata
        "id":                input_row.get("id", ""),
        "ucanrr_score":      input_row.get("ucanrr_score", ""),
        "ucanrr_band":       input_row.get("ucanrr_band", ""),
        "score_bucket":      input_row.get("score_bucket", ""),
        "primary_label":     input_row.get("primary_label", ""),
        "risk_level":        input_row.get("risk_level", ""),
        "tags":              input_row.get("tags", ""),
        "addressed_to":      input_row.get("addressed_to", ""),
        "style":             input_row.get("style", ""),
        # request params echoed for traceability
        "in_user_id":        input_row.get("user_id", ""),
        "in_partner_id":     input_row.get("partner_id", ""),
        "in_session_id":     input_row.get("session_id", ""),
        "in_share_requested": input_row.get("share_requested", ""),
        # entry last (wide text)
        "entry":             input_row.get("entry", "").strip(),
        "api_error":         error,
    }

    if assessment:
        flags = assessment.get("flags") or {}
        rec   = assessment.get("recommendations") or {}

        # All SASI columns read from assessment["sasi"] — the single stable
        # location built by build_sasi_export() right after sasi.analyze().
        # If this dict is absent or empty on a 200 response, the API did not
        # populate it; check stderr for [SASI-EXPORT first-response].
        sasi = assessment.get("sasi") or {}

        # ── Assertion: warn if every sasi_* value is None/empty on HTTP 200 ──
        sasi_valued = [v for v in sasi.values() if v not in (None, "", False, [])]
        if not sasi_valued and not error:
            import sys as _sys
            print(
                "[WARN] HTTP 200 but assessment['sasi'] is empty or absent.\n"
                "  Top-level keys in response: %s\n"
                "  assessment.get('sasi'): %r"
                % (list(assessment.keys()), assessment.get("sasi")),
                file=_sys.stderr,
            )

        mandatory_cats = sasi.get("mandatory_reporting_categories") or []

        out.update({
            # UCANRR tier — OpenAI when gate passes, SASI-derived when blocked
            "api_risk_tier":                    assessment.get("risk_tier", ""),
            "api_risk_label":                   assessment.get("risk_label", ""),
            # UCANRR flags — OpenAI only; empty when sasi_gate_blocked=TRUE
            "flag_suicidal_ideation":           flags.get("has_suicidal_ideation", ""),
            "flag_self_harm":                   flags.get("has_self_harm", ""),
            "flag_other_harm":                  flags.get("has_other_harm", ""),
            "flag_extreme_abuse":               flags.get("has_extreme_abuse", ""),
            "flag_heated_argument":             flags.get("has_heated_argument", ""),
            "flag_crisis_language":             flags.get("has_crisis_language", ""),
            "flag_substance_use":               flags.get("mentions_substance_use", ""),
            "flag_weapon_access":               flags.get("mentions_weapon_access", ""),
            "flag_child_safety":                flags.get("mentions_child_safety_concern", ""),
            "flag_ambiguous_lethal_curiosity":  flags.get("ambiguous_lethal_curiosity", ""),
            # UCANRR recommendations — OpenAI only; empty when sasi_gate_blocked=TRUE
            "api_partner_share_policy":         rec.get("partner_share_policy", ""),
            "api_therapist_share_policy":       rec.get("therapist_share_policy", ""),
            "api_show_crisis_banner":           rec.get("show_crisis_banner", ""),
            "api_show_crisis_resources":        rec.get("show_crisis_resources", ""),
            "api_suggested_ui_flow":            rec.get("suggested_ui_flow", ""),
            "api_mark_as_urgent_for_therapist": rec.get("mark_as_urgent_for_therapist", ""),
            "api_notes_for_therapist":          rec.get("notes_for_therapist", ""),
            "api_explanation":                  assessment.get("explanation", ""),
            # ── All SASI columns read from sasi dict (single source) ─────────
            "sasi_crisis_detected":             sasi.get("sasi_crisis_detected", ""),
            "sasi_human_oversight_required":    sasi.get("sasi_human_oversight_required", ""),
            "sasi_oversight_type":              sasi.get("sasi_oversight_type", ""),
            "sasi_risk_level":                  sasi.get("sasi_risk_level", ""),
            "sasi_gate_blocked":                sasi.get("sasi_gate_blocked", False),
            "share_allowed":                    sasi.get("share_allowed", ""),
            "share_blocked_reason":             sasi.get("share_blocked_reason", ""),
            "mandatory_reporting_flag":         sasi.get("mandatory_reporting_flag", ""),
            "mandatory_reporting_categories":   ";".join(mandatory_cats) if isinstance(mandatory_cats, list) else str(mandatory_cats),
            "sasi_envelope_id":                 sasi.get("sasi_envelope_id", ""),
            "policy_hash":                      sasi.get("policy_hash", ""),
            "sasi_envelope":                    sasi.get("sasi_envelope", ""),
            "sasi_flag_crisis":                 sasi.get("sasi_flag_crisis", ""),
            "sasi_flag_human_oversight":        sasi.get("sasi_flag_human_oversight", ""),
            "sasi_flag_should_block":           sasi.get("sasi_flag_should_block", ""),
            "sasi_flag_pii_detected":           sasi.get("sasi_flag_pii_detected", ""),
            "sasi_flag_show_hotline":           sasi.get("sasi_flag_show_hotline", ""),
            "sasi_flag_operator_crisis":        sasi.get("sasi_flag_operator_crisis", ""),
            "sasi_flag_parent_alert":           sasi.get("sasi_flag_parent_alert", ""),
            "sasi_flag_human_review":           sasi.get("sasi_flag_human_review", ""),
            "sasi_flag_action_rationale":       sasi.get("sasi_flag_action_rationale", ""),
            "sasi_flag_principle_triggered":    sasi.get("sasi_flag_principle_triggered", ""),
        })
    else:
        # Fill all api columns with empty strings on error
        for field in OUTPUT_FIELDS:
            if field not in out:
                out[field] = ""

    return out


# ---------- Summary helpers ----------

def print_summary(rows_processed: int, error_count: int, blocked_count: int, out_path: str) -> None:
    print("\n" + "=" * 80)
    print("Run complete.")
    print(f"  Processed      : {rows_processed}")
    print(f"  SASI blocked   : {blocked_count}")
    print(f"  Errors         : {error_count}")
    print(f"  Output         : {out_path}")
    print("=" * 80)


# ---------- Main runner ----------

def run_rows(rows: List[Dict[str, str]], out_path: str) -> None:
    total         = len(rows)
    error_count   = 0
    blocked_count = 0

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        writer.writeheader()

        for idx, row in enumerate(rows, start=1):
            entry_id  = row.get("id", "???")
            band      = row.get("ucanrr_band", "?")
            risk      = row.get("risk_level", "?")
            user_id   = row.get("user_id", "?")
            session   = row.get("session_id", "?")

            print(
                f"[{idx:>4}/{total}] {entry_id}  band={band}  risk={risk}"
                f"  user={user_id}  sess={session} ...",
                end=" ",
                flush=True,
            )

            error_msg  = ""
            assessment = None
            try:
                assessment = analyze_entry(row)

                sasi_blocked = assessment.get("sasi_gate_blocked", False)

                if sasi_blocked:
                    blocked_count += 1
                    ui_label = "SASI-BLOCKED  sasi_risk=%s" % assessment.get("sasi_risk_level", "?")
                else:
                    ui_label = "ui_flow=%s" % (
                        (assessment.get("recommendations") or {}).get("suggested_ui_flow", "?")
                    )
                print("OK  share=%s  %s" % (assessment.get("share_allowed", "?"), ui_label))

            except Exception as exc:
                error_msg = str(exc)
                error_count += 1
                print("ERROR: %s" % error_msg)

            writer.writerow(build_output_row(row, assessment, error_msg))

    print_summary(total, error_count, blocked_count, out_path)


# ---------- CLI ----------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="UCANRR + SASI Safety UI Simulator - CSV Edition"
    )
    parser.add_argument(
        "--csv", default=DEFAULT_CSV,
        help="Input CSV file (default: sasi_safety_test_data.csv)",
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
        "--id", dest="entry_id", default=None,
        help="Run only the entry with this ID (e.g. JE0042)",
    )
    parser.add_argument(
        "--band", default=None,
        help="Filter by ucanrr_band (e.g. harm, care, neglect)",
    )
    parser.add_argument(
        "--label", default=None,
        help="Filter by primary_label (e.g. ambiguous_monitor, crisis)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path  = "sasi_test_result_%s.csv" % timestamp

    print("=" * 80)
    print("UCANRR + SASI Safety UI Simulator")
    print("=" * 80)
    print(f"Input  : {args.csv}")
    print(f"Output : {out_path}")
    print(f"API    : {API_URL}")

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

    # Filter by primary_label
    if args.label:
        rows = [r for r in rows if r.get("primary_label", "").lower() == args.label.lower()]
        print(f"Filtered to label='{args.label}': {len(rows)} rows")

    # Apply start offset (1-based)
    start_idx = max(1, args.start) - 1
    rows = rows[start_idx:]

    # Apply limit
    if args.limit is not None:
        rows = rows[: args.limit]

    print(f"Running: {len(rows)} entries\n")

    run_rows(rows, out_path)


if __name__ == "__main__":
    main()
