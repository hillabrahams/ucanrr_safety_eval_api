"""
test_mindguard_batch.py
-----------------------
Batch-tests the UCANRR safety API against the Mindguard testset CSV.

The CSV `prompt` column stores conversations in a non-standard Python-like
format where role values are unquoted identifiers and short content values
may also be unquoted.  This script parses that format, formats each
conversation as a plain-text transcript, and posts it to the safety API.

Usage:
    python test_mindguard_batch.py [OPTIONS]

Options:
    --csv   PATH   Input CSV  (default: unsafe-content-analysis-Mindguard-testset.csv)
    --api   URL    Safety API endpoint (default: Azure-hosted production API)
    --out   PATH   Results CSV output   (default: mindguard_results.csv)
    --delay SECS   Pause between API calls to avoid rate-limiting (default: 0.5)
    --mode  MODE   "full" = whole conversation as entry_text (default)
                   "last" = only the final user message as entry_text

Example:
    python test_mindguard_batch.py --delay 1.0 --out results/mindguard_run1.csv
"""

import argparse
import csv
import re
import sys
import time
from pathlib import Path

import httpx


# ---------------------------------------------------------------------------
# Conversation parser
# ---------------------------------------------------------------------------
# The `prompt` column looks like:
#   [{'role': user, 'content': Hello}, {'role': assistant, 'content': 'Hi Betty...'}]
#
# Quirks:
#   - `role` values (user, assistant) are unquoted Python identifiers
#   - `content` values are either single-quoted strings (with \" escaped as \')
#     or bare unquoted text up to the closing }
#
# Strategy: one regex that captures both forms in named groups.

_MSG_RE = re.compile(
    r"\{'role':\s*(?P<role>user|assistant),\s*'content':\s*"
    r"(?:'(?P<quoted>(?:[^'\\]|\\.)*)'|(?P<unquoted>[^}]*))\}",
    re.DOTALL,
)


def parse_conversation(prompt_str: str) -> list[dict]:
    """Return list of {role, content} dicts parsed from a Mindguard prompt string."""
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


def format_as_transcript(messages: list[dict]) -> str:
    """Convert message list to a readable plain-text transcript for entry_text."""
    lines = []
    for msg in messages:
        prefix = "User" if msg["role"] == "user" else "Therapist"
        lines.append(f"{prefix}: {msg['content']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Label helpers
# ---------------------------------------------------------------------------

# Mindguard labels → expected UCANRR tier
_LABEL_TO_TIER = {
    "unsafe_self_harm_risk": 3,
    "unsafe_harm_to_others": 3,
    "safe": 0,          # safe rows would ideally be tier 0–2
}

# For display / tracking which unsafe category we expected
_LABEL_TO_CATEGORY = {
    "unsafe_self_harm_risk": "crisis",
    "unsafe_harm_to_others": "crisis_or_extreme_abuse",
    "safe": "normal",
}


def expected_tier(label: str) -> int:
    return _LABEL_TO_TIER.get(label, -1)


# ---------------------------------------------------------------------------
# API caller
# ---------------------------------------------------------------------------

def call_api(client: httpx.Client, url: str, entry_text: str, row_id: str) -> dict:
    resp = client.post(
        url,
        json={
            "entry_text": entry_text,
            "user_id": f"mindguard-{row_id}",
        },
        timeout=45,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv",   default="unsafe-content-analysis-Mindguard-testset.csv")
    parser.add_argument("--api",   default="https://safetyapi-c6cqctbghub5f5d8.canadacentral-01.azurewebsites.net/safety/analyze_entry")
    parser.add_argument("--out",   default="mindguard_results.csv")
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--mode",  choices=["full", "last"], default="full",
                        help="full = whole conversation; last = final user message only")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ERROR: CSV not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    print(f"Loaded {len(rows)} rows from {csv_path}")
    print(f"API:   {args.api}")
    print(f"Mode:  {args.mode}")
    print(f"Output: {args.out}\n")

    results = []
    total = correct_tier = parse_errors = api_errors = 0

    out_fields = [
        "row", "true_label", "expected_category",
        "user_message",
        "risk_tier", "risk_label",
        "sasi_gate_blocked", "sasi_risk_level", "sasi_crisis_detected",
        "share_allowed",
        "has_suicidal_ideation", "has_self_harm", "has_other_harm", "has_extreme_abuse",
        "show_crisis_banner", "show_crisis_resources",
        "tier_correct", "error",
    ]

    with httpx.Client() as client:
        for i, row in enumerate(rows, 1):
            true_label = row.get("label", "").strip()
            user_message = row.get("user_message", "").strip()
            prompt_str = row.get("prompt", "")

            # Build entry_text
            if args.mode == "last":
                entry_text = user_message
            else:
                messages = parse_conversation(prompt_str)
                if not messages:
                    print(f"[{i:3d}] PARSE ERROR — no messages extracted")
                    parse_errors += 1
                    total += 1
                    results.append({f: "" for f in out_fields} | {
                        "row": i, "true_label": true_label,
                        "user_message": user_message[:80],
                        "error": "parse_failed",
                    })
                    continue
                entry_text = format_as_transcript(messages)

            # Call API
            error_str = ""
            result = {}
            try:
                result = call_api(client, args.api, entry_text, str(i))
            except httpx.HTTPStatusError as e:
                error_str = f"HTTP {e.response.status_code}: {e.response.text[:120]}"
            except Exception as e:
                error_str = str(e)[:120]

            if error_str:
                api_errors += 1
                total += 1
                print(f"[{i:3d}] API ERROR — {error_str[:80]}")
                results.append({f: "" for f in out_fields} | {
                    "row": i, "true_label": true_label,
                    "user_message": user_message[:80],
                    "error": error_str,
                })
                continue

            # Extract fields
            pred_tier    = result.get("risk_tier", -1)
            pred_label   = result.get("risk_label", "")
            flags        = result.get("flags") or {}
            recs         = result.get("recommendations") or {}
            sasi_blocked = result.get("sasi_gate_blocked", False)
            sasi_risk    = result.get("sasi_risk_level", "")
            sasi_crisis  = result.get("sasi_crisis_detected", False)
            share_ok     = result.get("share_allowed", True)

            exp_tier   = expected_tier(true_label)
            tier_ok    = (pred_tier == exp_tier)
            correct_tier += int(tier_ok)
            total += 1

            status = "OK  " if tier_ok else "MISS"
            print(
                f"[{i:3d}/{len(rows)}] {status} | "
                f"true={true_label:28s} | "
                f"pred={pred_tier}/{pred_label:14s} | "
                f"sasi_blocked={sasi_blocked} | "
                f"{user_message[:55]!r}"
            )

            results.append({
                "row": i,
                "true_label": true_label,
                "expected_category": _LABEL_TO_CATEGORY.get(true_label, ""),
                "user_message": user_message[:120],
                "risk_tier": pred_tier,
                "risk_label": pred_label,
                "sasi_gate_blocked": sasi_blocked,
                "sasi_risk_level": sasi_risk,
                "sasi_crisis_detected": sasi_crisis,
                "share_allowed": share_ok,
                "has_suicidal_ideation": flags.get("has_suicidal_ideation", ""),
                "has_self_harm":         flags.get("has_self_harm", ""),
                "has_other_harm":        flags.get("has_other_harm", ""),
                "has_extreme_abuse":     flags.get("has_extreme_abuse", ""),
                "show_crisis_banner":    recs.get("show_crisis_banner", ""),
                "show_crisis_resources": recs.get("show_crisis_resources", ""),
                "tier_correct": tier_ok,
                "error": "",
            })

            time.sleep(args.delay)

    # Write results
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    # Summary
    print(f"\n{'='*65}")
    print(f"Rows evaluated : {total}")
    print(f"Tier correct   : {correct_tier}/{total}  ({100*correct_tier/total:.1f}%)"
          if total else "Tier correct   : n/a")
    print(f"Parse errors   : {parse_errors}")
    print(f"API errors     : {api_errors}")
    print(f"Results saved  : {out_path}")


if __name__ == "__main__":
    main()
