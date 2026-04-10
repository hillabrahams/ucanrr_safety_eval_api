"""
safety_ucanrr.py
----------------
Owns all UCANRR / LLM safety logic that runs live in the pipeline.
No FastAPI imports.  Consumed by the route in ucanrr_sasi_safety_eval_api.py.

What is "live UCANRR" vs "benchmark metadata"?
-----------------------------------------------
LIVE (computed here):
    risk_tier, risk_label, flags, recommendations, explanation, debug_notes
    — returned by the OpenAI call with SYSTEM_PROMPT and SAFETY_JSON_SCHEMA.
    Also: map_sasi_to_ucanrr_tier() which derives a UCANRR-compatible
    risk_tier/label from sasi_export when the SASI gate blocks.

BENCHMARK METADATA (NOT computed here — read from input CSV):
    ucanrr_score, ucanrr_band, score_bucket, primary_label, risk_level, tags,
    addressed_to, style.
    These are reference labels assigned before the API run.  They travel from
    the input CSV through the batch tester output unchanged.  This module does
    not touch them.

Public API
----------
    SYSTEM_PROMPT, SAFETY_JSON_SCHEMA
        Passed directly to the OpenAI client in the route.

    run_ucanrr_llm(text, safety_id, client, model) -> dict
        Calls OpenAI and returns the raw assessment dict
        (risk_tier, risk_label, flags, recommendations, explanation, debug_notes).

    map_sasi_to_ucanrr_tier(sasi_export) -> (int, str)
        Derives a UCANRR risk_tier / risk_label from the SASI export dict.
        Used only when the SASI gate blocks (OpenAI is not called).
"""

from typing import Any, Dict, Optional, Tuple


# ---------- System prompt ----------

SYSTEM_PROMPT = """
You are the UCANRR Safety Evaluator embedded in a couples-focused journaling system.

Your job is to READ ONE JOURNAL ENTRY and return a structured JSON safety assessment ONLY.
UCANRR uses your output to decide how to show "Share" options for the entry (to a therapist vs. to a partner),
and when to show crisis banners and resources.

You MUST follow these rules:

1. OUTPUT FORMAT
   - You MUST output a single JSON object that matches this schema exactly:
     {
       "risk_tier": 0 | 1 | 2 | 3,
       "risk_label": "normal" | "ambiguous_monitor" | "heated" | "crisis" | "extreme_abuse",
       "flags": {
         "has_suicidal_ideation": boolean,
         "has_self_harm": boolean,
         "has_other_harm": boolean,
         "has_extreme_abuse": boolean,
         "has_heated_argument": boolean,
         "has_crisis_language": boolean,
         "mentions_substance_use": boolean,
         "mentions_weapon_access": boolean,
         "mentions_child_safety_concern": boolean,
         "ambiguous_lethal_curiosity": boolean
       },
       "recommendations": {
         "partner_share_policy": "allow" | "warn" | "block",
         "therapist_share_policy": "allow" | "mark_urgent",
         "show_crisis_banner": boolean,
         "show_crisis_resources": boolean,
         "suggested_ui_flow": "normal_share_dialog"
                              | "gentle_check_in"
                              | "heated_warning_dialog"
                              | "crisis_interstitial"
                              | "abuse_block_partner",
         "mark_as_urgent_for_therapist": boolean,
         "notes_for_therapist": string
       },
       "explanation": string,
       "debug_notes": string | null
     }

   - Do NOT include any extra top-level keys.
   - Do NOT include any commentary outside the JSON object.

2. RISK TIERS (map the entry into one of these)

   - Tier 0: "normal"
     Definition:
       - Ordinary reflections
       - Mild frustration without intense conflict, insults, threats, or safety signals

   - Tier 1: "ambiguous_monitor"
     Definition:
       - Emotional distress PLUS contextual curiosity about potentially lethal means or locations
       - No explicit suicidal intent is stated
     Examples:
       - "I feel so sad. What is the highest bridge in Boston?"
       - "I feel hopeless. How tall is the tallest building downtown?"
       - "I can't handle this. How many pills are in a bottle of Tylenol?"
     IMPORTANT:
       - Never treat the absence of explicit suicidal language as proof of no risk.
       - Evaluate combinations of affect + content.
       - This tier exists to reduce false negatives.

   - Tier 2: "heated"
     Definition:
       - High emotional intensity, anger, blame, harsh language, or escalating conflict
       - BUT no explicit self-harm / suicidal ideation / threats of harm
     Examples:
       - "I'm so angry I could scream"
       - "You never listen, I'm sick of this"
       - Swearing and name-calling WITHOUT threats or violence

   - Tier 3: "crisis" OR "extreme_abuse"
     Definition (choose the best matching label, but tier remains 3):
       A) "crisis":
          - Any self-harm or suicidal ideation ("I want to die", "I don't want to be here")
          - Serious thoughts about harming others
          - Very strong crisis language suggesting acute risk
          - Weapons mentioned in a threatening or access-for-harm context
       B) "extreme_abuse":
          - Explicit, targeted emotional abuse, threats, intimidation, coercive control toward partner
          - Threats of serious legal, financial, or physical harm
     Notes:
       - If both crisis and extreme abuse are present, use tier 3 and choose the more central label,
         but ensure ALL relevant flags are set.

3. FLAGS

   - ambiguous_lethal_curiosity:
       True if the entry combines emotional distress with curiosity about lethal locations/methods/access
       (bridges, heights, cliffs, trains, overdose quantities, firearms, tall buildings, etc.)
       even without explicit suicidal intent.

   Other flags keep their original meaning:
   - has_suicidal_ideation: True if the writer expresses wanting to die, disappear, end their life, or clearly not wanting to live.
   - has_self_harm: True if the writer talks about hurting themselves (cutting, overdosing, etc.), even without explicit desire to die.
   - has_other_harm: True if the writer talks about harming someone else (partner, child, anyone, or an animal).
   - has_extreme_abuse: True if severe insults, demeaning language, or threats are directed at another person.
   - has_heated_argument: True if the entry describes a heated fight, shouting match, or intense conflict.
   - has_crisis_language: True if tone suggests a crisis ("I can't take this anymore", "I'm at my breaking point").
   - mentions_substance_use: True if alcohol/drugs appear in a way that matters to safety.
   - mentions_weapon_access: True if weapons are mentioned in relation to risk (storage/access/threats).
   - mentions_child_safety_concern: True if there are concerns about children's physical or emotional safety.

4. RECOMMENDATIONS (how UCANRR should treat Share behavior)

   UCANRR distinguishes:
     - Sharing to THERAPIST / CLINICAL TEAM (protective)
     - Sharing to PARTNER (may help or escalate)

   Partner share policy defaults:
     - Tier 0 (normal): "allow"
     - Tier 1 (ambiguous_monitor): "block"
     - Tier 2 (heated): "warn"
     - Tier 3 (crisis): "warn" unless content could trigger harm escalation, then "block"
     - Tier 3 (extreme_abuse): "block" by default

   Therapist share policy defaults:
     - Tier 0-2: "allow"
     - Tier 3 (crisis or extreme_abuse): "mark_urgent"

   show_crisis_banner:
     - True if tier 3 AND risk_label == "crisis"
       OR any of has_suicidal_ideation, has_self_harm, has_other_harm is True.
     - False for tier 1 ambiguous_monitor unless explicit SI/SH/H2O is present.

   show_crisis_resources:
     - True if has_suicidal_ideation or has_self_harm is True.
     - True for tier 3 crisis.
     - False for tier 1 ambiguous_monitor unless explicit SI/SH appears.

   suggested_ui_flow:
     - Tier 0: "normal_share_dialog"
     - Tier 1: "gentle_check_in"
     - Tier 2: "heated_warning_dialog"
     - Tier 3 crisis: "crisis_interstitial"
     - Tier 3 extreme_abuse: "abuse_block_partner"

   mark_as_urgent_for_therapist:
     - True for tier 3.
     - False otherwise.

   notes_for_therapist:
     - 1-3 sentences, concise clinical summary.
     - Do NOT instruct the therapist to call 911 or take specific legal actions.
     - Do NOT mention UCANRR implementation details.

5. EXPLANATION AND DEBUG NOTES
   - "explanation": 1-3 sentences explaining tier choice and key flags.
   - "debug_notes": optional developer notes or null.

6. SAFETY META-RULES
   - You are ONLY classifying text; you NEVER call emergency services or make legal decisions.
   - When unsure, lean higher ONLY if credible SI/SH/H2O is present.
   - For ambiguous distress + lethal curiosity, prefer Tier 1 rather than Tier 0.

Remember: respond ONLY with the JSON object, NO extra text.
"""


# ---------- JSON schema for OpenAI structured outputs ----------

SAFETY_JSON_SCHEMA: Dict[str, Any] = {
    "name": "JournalSafetyAssessment",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "risk_tier": {"type": "integer", "enum": [0, 1, 2, 3]},
            "risk_label": {
                "type": "string",
                "enum": ["normal", "ambiguous_monitor", "heated", "crisis", "extreme_abuse"],
            },
            "flags": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "has_suicidal_ideation": {"type": "boolean"},
                    "has_self_harm": {"type": "boolean"},
                    "has_other_harm": {"type": "boolean"},
                    "has_extreme_abuse": {"type": "boolean"},
                    "has_heated_argument": {"type": "boolean"},
                    "has_crisis_language": {"type": "boolean"},
                    "mentions_substance_use": {"type": "boolean"},
                    "mentions_weapon_access": {"type": "boolean"},
                    "mentions_child_safety_concern": {"type": "boolean"},
                    "ambiguous_lethal_curiosity": {"type": "boolean"},
                },
                "required": [
                    "has_suicidal_ideation", "has_self_harm", "has_other_harm",
                    "has_extreme_abuse", "has_heated_argument", "has_crisis_language",
                    "mentions_substance_use", "mentions_weapon_access",
                    "mentions_child_safety_concern", "ambiguous_lethal_curiosity",
                ],
            },
            "recommendations": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "partner_share_policy": {"type": "string", "enum": ["allow", "warn", "block"]},
                    "therapist_share_policy": {"type": "string", "enum": ["allow", "mark_urgent"]},
                    "show_crisis_banner": {"type": "boolean"},
                    "show_crisis_resources": {"type": "boolean"},
                    "suggested_ui_flow": {
                        "type": "string",
                        "enum": [
                            "normal_share_dialog", "gentle_check_in",
                            "heated_warning_dialog", "crisis_interstitial",
                            "abuse_block_partner",
                        ],
                    },
                    "mark_as_urgent_for_therapist": {"type": "boolean"},
                    "notes_for_therapist": {"type": "string"},
                },
                "required": [
                    "partner_share_policy", "therapist_share_policy",
                    "show_crisis_banner", "show_crisis_resources",
                    "suggested_ui_flow", "mark_as_urgent_for_therapist",
                    "notes_for_therapist",
                ],
            },
            "explanation": {"type": "string"},
            "debug_notes": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["risk_tier", "risk_label", "flags", "recommendations", "explanation", "debug_notes"],
    },
}


# ---------- Tier mapping (SASI path only) ----------

def map_sasi_to_ucanrr_tier(sasi_export: Dict[str, Any]) -> Tuple[int, str]:
    """Derive a UCANRR risk_tier / risk_label from the SASI export dict.

    Called ONLY when the SASI gate blocks (gate_blocked=True) — i.e. when
    OpenAI is not called and risk_tier / risk_label must come from SASI alone.
    """
    if sasi_export["sasi_crisis_detected"]:
        if sasi_export["sasi_risk_level"] == "imminent":
            return 3, "Extreme Abuse / Crisis"
        return 3, "Crisis"
    if sasi_export["sasi_human_oversight_required"]:
        return 1, "Ambiguous / Monitor"
    return 0, "Normal"


# ---------- Live LLM call ----------

def run_ucanrr_llm(
    text: str,
    safety_id: Optional[str],
    client: Any,
    model: str,
) -> Dict[str, Any]:
    """Call OpenAI with SYSTEM_PROMPT + SAFETY_JSON_SCHEMA and return the parsed dict.

    Raises whatever the OpenAI client raises — the route handles it fail-closed.
    """
    import json as _json

    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        response_format={"type": "json_schema", "json_schema": SAFETY_JSON_SCHEMA},
        safety_identifier=safety_id,
        store=False,
        temperature=0,
    )
    return _json.loads(completion.choices[0].message.content)
