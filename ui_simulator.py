"""
UCANRR Safety UI Simulator (Python 3.8+ compatible)

This script calls your running FastAPI safety API and prints:
- risk tier / label
- flags
- recommended UI flow
- how Share should behave (therapist vs partner)

Usage:
    python ui_simulator.py               # run built-in test cases
    python ui_simulator.py "your entry"  # test a single custom entry
"""

import json
import sys
import textwrap
from typing import Dict, Any, List, Optional

import requests

API_URL = "http://localhost:3000/safety/analyze_entry"


# ---------- Helper: pretty print a header ----------

def print_header(title: str) -> None:
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80 + "\n")


# ---------- Helper: describe UI behavior based on the assessment ----------

def describe_ui_behavior(assessment: Dict[str, Any]) -> None:
    risk_tier = assessment["risk_tier"]
    risk_label = assessment["risk_label"]
    flags = assessment["flags"]
    rec = assessment["recommendations"]

    print(f"Risk Tier: {risk_tier} ({risk_label})")
    print()
    print("Flags:")
    for k, v in flags.items():
        print(f"  - {k}: {v}")

    print("\nRecommendations:")
    print(f"  - partner_share_policy: {rec['partner_share_policy']}")
    print(f"  - therapist_share_policy: {rec['therapist_share_policy']}")
    print(f"  - show_crisis_banner: {rec['show_crisis_banner']}")
    print(f"  - show_crisis_resources: {rec['show_crisis_resources']}")
    print(f"  - suggested_ui_flow: {rec['suggested_ui_flow']}")
    print(f"  - mark_as_urgent_for_therapist: {rec['mark_as_urgent_for_therapist']}")
    print("\nNotes for therapist:")
    print(textwrap.indent(textwrap.fill(rec["notes_for_therapist"], width=76), "    "))

    # Now map suggested_ui_flow to a human description of what UCANRR will show
    print("\nSimulated UCANRR UI behavior:")
    flow = rec["suggested_ui_flow"]
    partner_policy = rec["partner_share_policy"]

    if flow == "normal_share_dialog":
        print("  • Show NORMAL share dialog:")
        print("      - Buttons: [Share with my therapist] [Share with my partner] [Cancel]")
        print("      - Partner share policy: %s (should be 'allow')" % partner_policy)

    elif flow == "heated_warning_dialog":
        print("  • Show HEATED warning dialog:")
        print("      - Title: 'This looks like a heated moment'")
        print("      - Explain that sending may escalate conflict.")
        print("      - Buttons:")
        print("          [Share with my therapist]")
        if partner_policy != "block":
            print("          [Share anyway to partner]")
        print("          [Edit first]")
        print("          [Don't share now]")

    elif flow == "crisis_interstitial":
        print("  • Show CRISIS interstitial:")
        print("      - Title: 'This looks like a crisis'")
        print("      - Body: 911 / 988 message and explain crisis resources.")
        print("      - Buttons:")
        print("          [Share with my therapist now]")
        if rec["show_crisis_resources"]:
            print("          [Use a crisis resource]")
        if partner_policy != "block":
            print("          [Share with my partner anyway]")
        print("          [Keep this private for now]")

    elif flow == "abuse_block_partner":
        print("  • Show ABUSE / BLOCK-PARTNER dialog:")
        print("      - Title: 'This looks harmful'")
        print("      - Explain partner sharing is blocked for safety.")
        print("      - Buttons:")
        print("          [Share with my therapist]")
        print("          [Keep it private]")
        print("      - Partner sharing should NOT be offered.")

    else:
        print("  • Unknown suggested_ui_flow; default to heated warning dialog behavior.")


# ---------- Call the safety API ----------

def analyze_entry(entry_text: str, user_hash: Optional[str] = None) -> Dict[str, Any]:

    payload = {"entry_text": entry_text}
    if user_hash:
        payload["user_hash"] = user_hash

    resp = requests.post(API_URL, json=payload, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError("API error %s: %s" % (resp.status_code, resp.text))

    return resp.json()


# ---------- Built-in test cases ----------

TEST_CASES: List[Dict[str, Any]] = [
    {
        "name": "Normal / non-emotional entry (Tier 0)",
        "text": """
        We had a quiet day today. We cooked dinner and watched a movie together.
        Nothing major happened and we were both pretty relaxed.
        """,
    },
    {
        "name": "Heated argument, no abuse (Tier 1)",
        "text": """
        I'm extremely frustrated. She never listens and I feel like I'm talking
        to a wall. I'm sick of this today but I don't want to hurt her.
        """,
    },
    {
        "name": "Suicidal ideation (Tier 2 crisis)",
        "text": """
        I don't want to be here anymore. Everything feels pointless and I keep
        thinking it would be easier if I just disappeared.
        """,
    },
    {
        "name": "Extreme abuse / threats (Tier 3)",
        "text": """
        You are worthless and I am going to ruin your life. I will make sure
        you lose everything and regret ever meeting me.
        """,
    },
    {
        "name": "Crisis + substance use",
        "text": """
        I've been drinking heavily every night. I keep thinking that maybe it
        would be better if I just stopped waking up. I don't see a way out.
        """,
    },
    {
        "name": "Child safety concern + conflict",
        "text": """
        The way we were shouting last night scared the kids. Our daughter hid in
        her room crying while we argued. I feel horrible about it.
        """,
    },
]


def run_builtin_tests() -> None:
    print_header("UCANRR Safety UI Simulator - Built-in Test Cases")

    for idx, case in enumerate(TEST_CASES, start=1):
        name = case["name"]
        text = " ".join(line.strip() for line in case["text"].strip().splitlines())

        print_header("Test %d: %s" % (idx, name))
        print("Journal entry:")
        print(textwrap.indent(textwrap.fill(text, width=76), "  "))
        print("\nCalling safety API...")

        assessment = analyze_entry(text, user_hash="sim-user-123")
        print("\nSafety assessment JSON:")
        print(json.dumps(assessment, indent=2))
        print()
        describe_ui_behavior(assessment)


def run_single_entry(entry_text: str) -> None:
    print_header("UCANRR Safety UI Simulator - Single Entry")
    print("Journal entry:")
    print(textwrap.indent(textwrap.fill(entry_text, width=76), "  "))
    print("\nCalling safety API...")

    assessment = analyze_entry(entry_text, user_hash="sim-user-123")
    print("\nSafety assessment JSON:")
    print(json.dumps(assessment, indent=2))
    print()
    describe_ui_behavior(assessment)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        # Test a single custom entry from CLI arguments
        entry = " ".join(sys.argv[1:])
        run_single_entry(entry)
    else:
        # Run through predefined test cases
        run_builtin_tests()
