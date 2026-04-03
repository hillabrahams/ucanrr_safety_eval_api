"""
UCANRR + SASI Safety Evaluation API
-------------------------------------

FastAPI service that runs SASI as a pre-LLM safety gate, then—if the gate
passes—calls the OpenAI API for the full UCANRR tiered assessment.

Flow per request:
  1) SASI pre-LLM analysis (fail-closed)
  2) If SASI blocks → return immediately (no OpenAI call)
  3) If SASI passes → OpenAI structured-output evaluation
  4) Merge SASI outcomes + UCANRR assessment in a single response

Requires:
    pip install fastapi uvicorn "openai>=1.0.0" pydantic sasi-sdk python-dotenv

Run:
    export OPENAI_API_KEY="YOUR_KEY"
    uvicorn ucanrr_sasi_safety_eval_api:app --host 0.0.0.0 --port 3000 --reload
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from openai import OpenAI

# SASI SDK — comment out and swap in your real import path if needed
try:
    from sasi_sdk import SasiSession
    SASI_AVAILABLE = True
except ImportError:
    SASI_AVAILABLE = False

# Dev flag: print the first successful SASI export to stderr so we can verify
# the real SasiResult shape before trusting the CSV.  Flip to False in prod.
_sasi_logged = False


# ---------- environment ----------

load_dotenv()

OPENAI_MODEL = "gpt-4o"
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# Absolute path to the SASI config file — works regardless of cwd when uvicorn starts.
_SASI_CONFIG_PATH = str(Path(__file__).resolve().parent / "config" / "sasi.yaml")


# ---------- Request model ----------

class JournalEntryRequest(BaseModel):
    entry_text: str = Field(..., description="Raw journal entry text from the UCANRR mobile app.")
    user_id: Optional[str] = Field(
        None,
        description=(
            "Stable pseudonymous user identifier (no PII). "
            "Production clients must always supply a real per-user ID. "
            "When omitted or empty, the server falls back to SASI_DEFAULT_USER_ID "
            "(intended for local/batch testing only)."
        ),
    )
    partner_id: Optional[str] = Field(None, description="Pseudonymous partner identifier for couple context.")
    session_id: Optional[str] = Field(None, description="Optional session/conversation ID for SASI continuity.")
    share_requested: Optional[bool] = Field(False, description="Whether the user is attempting to share this entry.")
    # kept for backward-compat; falls back to user_id for OpenAI safety_identifier
    user_hash: Optional[str] = Field(None, description="Deprecated alias for user_id; use user_id instead.")


# ---------- Response model ----------

class JournalSafetyFlags(BaseModel):
    has_suicidal_ideation: bool
    has_self_harm: bool
    has_other_harm: bool
    has_extreme_abuse: bool
    has_heated_argument: bool
    has_crisis_language: bool
    mentions_substance_use: bool
    mentions_weapon_access: bool
    mentions_child_safety_concern: bool
    ambiguous_lethal_curiosity: bool


class JournalRecommendations(BaseModel):
    partner_share_policy: str
    therapist_share_policy: str
    show_crisis_banner: bool
    show_crisis_resources: bool
    suggested_ui_flow: str
    mark_as_urgent_for_therapist: bool
    notes_for_therapist: str


class CombinedSafetyAssessment(BaseModel):
    # ── UCANRR tier ──────────────────────────────────────────────────────────
    risk_tier: int = Field(..., ge=0, le=3)
    risk_label: str
    flags: Optional[JournalSafetyFlags] = None
    recommendations: Optional[JournalRecommendations] = None
    explanation: Optional[str] = None
    debug_notes: Optional[str] = None

    # ── SASI outcomes ─────────────────────────────────────────────────────────
    sasi_crisis_detected: bool
    sasi_human_oversight_required: bool
    sasi_oversight_type: Optional[str] = None
    # risk_level as string (e.g. "low", "medium", "high", "imminent");
    # sourced from result.risk_level.value (enum) or str(result.risk_level).
    sasi_risk_level: str

    # ── Gate ──────────────────────────────────────────────────────────────────
    # True iff SASI short-circuited analysis before the OpenAI call, i.e.
    # result.is_crisis or result.human_oversight_required was set.
    sasi_gate_blocked: bool

    # ── Share gate ────────────────────────────────────────────────────────────
    share_allowed: bool
    share_blocked_reason: Optional[str] = None

    # ── Mandatory-reporting (placeholder until feature ships) ─────────────────
    mandatory_reporting_flag: bool
    mandatory_reporting_categories: List[str]

    # ── Audit refs ────────────────────────────────────────────────────────────
    sasi_envelope_id: Optional[str] = None
    policy_hash: Optional[str] = None
    # Envelope serialized as JSON (stable non-PII keys only; no raw user text).
    # None when result.envelope is absent.
    sasi_envelope: Optional[str] = None

    # ── SASI direct flags from SasiResult ─────────────────────────────────────
    sasi_flag_crisis: bool                    # result.is_crisis
    sasi_flag_human_oversight: bool           # result.human_oversight_required
    sasi_flag_should_block: Optional[bool] = None   # result.should_block
    sasi_flag_pii_detected: Optional[bool] = None   # result.pii_detected
    sasi_flag_show_hotline: Optional[bool] = None   # result.show_hotline
    sasi_flag_operator_crisis: Optional[bool] = None  # result.operator_crisis

    # ── SASI extra flags (ancillary string/bool fields from SasiResult) ───────
    sasi_flags: Dict[str, Optional[str]]

    # ── Authoritative flat export — single source of truth for the CSV ────────
    # Keys match CSV column names exactly; built right after sasi.analyze() and
    # never modified through multiple helper layers.  The batch tester reads
    # assessment["sasi"][key] — not top-level keys — for all sasi_* columns.
    sasi: Dict[str, Any] = Field(default_factory=dict)


# ---------- SASI helpers ----------

def build_sasi_session(user_id: Optional[str]) -> "SasiSession":
    """Create a per-user SASI session (avoid a global singleton).

    user_id     – from the request body (pseudonymous, no PII); may be None/empty
                  for local/batch runs.  Production should always send real per-user
                  IDs; the SASI_DEFAULT_USER_ID fallback is for local/batch testing only.
    config_path – absolute path so this works regardless of uvicorn cwd.
    """
    effective_user_id = (user_id or "").strip() or os.getenv(
        "SASI_DEFAULT_USER_ID", "local_batch_test"
    )
    return SasiSession(
        user_id=effective_user_id,
        mode="therapist",
        config_path=_SASI_CONFIG_PATH,
    )


# Keys that could carry raw user text — strip before serialising envelope.
_ENVELOPE_TEXT_KEYS = frozenset({"message", "text", "content", "input", "entry", "prompt"})


def build_sasi_export(result) -> Dict[str, Any]:
    """Build the authoritative SASI export dict immediately after sasi.analyze().

    This is the ONLY place SasiResult attributes are read.  Every other part of
    the code (response builder, batch tester) reads from this dict, not from
    SasiResult directly.  That makes attribute-access failures visible in one spot
    rather than silently producing Nones through multiple helper layers.

    Keys match CSV column names exactly so the batch tester can do:
        sasi = assessment["sasi"]
        row["sasi_risk_level"] = sasi["sasi_risk_level"]
    with no further translation.
    """
    import sys as _sys

    # ── Risk level: try .name (enum string), then .value, then str() ────────
    rl = getattr(result, "risk_level", None)
    if rl is None:
        risk_level_str = "unknown"
    elif hasattr(rl, "name"):       # Python enum  → e.g. "LOW", "HIGH"
        risk_level_str = rl.name.lower()
    elif hasattr(rl, "value"):      # enum-like with .value
        risk_level_str = str(rl.value).lower()
    else:
        risk_level_str = str(rl).lower()

    # ── Envelope (safe; absent envelope is common in low-risk responses) ─────
    envelope = getattr(result, "envelope", None)
    envelope_id: Optional[str] = None
    policy_hash: Optional[str] = None
    envelope_json: Optional[str] = None
    if envelope is not None:
        envelope_id = getattr(envelope, "run_id", None)
        policy_hash = getattr(envelope, "policy_hash", None)
        if hasattr(envelope, "to_dict"):
            _edata: Dict[str, Any] = dict(envelope.to_dict())
        elif hasattr(envelope, "__dict__"):
            _edata = dict(envelope.__dict__)
        else:
            _edata = {}
        for _k in _ENVELOPE_TEXT_KEYS:
            _edata.pop(_k, None)
        envelope_json = json.dumps(_edata, default=str)
    else:
        policy_hash = getattr(result, "policy_hash", None)

    # ── Share gate ───────────────────────────────────────────────────────────
    if result.is_crisis:
        share_allowed: bool = False
        share_blocked_reason: Optional[str] = "crisis_detected"
    elif result.human_oversight_required:
        share_allowed = False
        share_blocked_reason = "pending_human_review"
    elif getattr(result, "mandatory_reporting_flag", False):
        share_allowed = False
        share_blocked_reason = "mandatory_reporting_obligation"
    else:
        share_allowed = True
        share_blocked_reason = None

    mandatory_cats: List[str] = list(
        getattr(result, "mandatory_reporting_categories", None) or []
    )

    export: Dict[str, Any] = {
        # Core risk
        "sasi_risk_level":                risk_level_str,
        "sasi_crisis_detected":           result.is_crisis,
        "sasi_human_oversight_required":  result.human_oversight_required,
        "sasi_oversight_type":            getattr(result, "oversight_type", None),
        # Gate — caller sets to True when returning early (see analyze_entry)
        "sasi_gate_blocked":              False,
        # Share gate
        "share_allowed":                  share_allowed,
        "share_blocked_reason":           share_blocked_reason,
        # Mandatory reporting
        "mandatory_reporting_flag":       getattr(result, "mandatory_reporting_flag", False),
        "mandatory_reporting_categories": mandatory_cats,
        # Audit
        "sasi_envelope_id":               envelope_id,
        "policy_hash":                    policy_hash,
        "sasi_envelope":                  envelope_json,
        # Direct boolean flags (1-to-1 from SasiResult)
        "sasi_flag_crisis":               result.is_crisis,
        "sasi_flag_human_oversight":      result.human_oversight_required,
        "sasi_flag_should_block":         getattr(result, "should_block", None),
        "sasi_flag_pii_detected":         getattr(result, "pii_detected", None),
        "sasi_flag_show_hotline":         getattr(result, "show_hotline", None),
        "sasi_flag_operator_crisis":      getattr(result, "operator_crisis", None),
        # Ancillary flags
        "sasi_flag_parent_alert":         getattr(result, "parent_alert_flag", None),
        "sasi_flag_human_review":         getattr(result, "human_review_flag", None),
        "sasi_flag_action_rationale":     getattr(result, "action_rationale", None),
        "sasi_flag_principle_triggered":  getattr(result, "principle_triggered", None),
    }

    # Warn loudly if the two required booleans are not actually bool — means
    # SasiResult has a different shape than expected.
    for _req in ("sasi_crisis_detected", "sasi_human_oversight_required"):
        if not isinstance(export[_req], bool):
            print(
                f"[SASI-EXPORT WARNING] {_req} = {export[_req]!r} "
                f"(type {type(export[_req]).__name__}), expected bool. "
                "SasiResult shape may differ from SDK docs.",
                file=_sys.stderr,
            )

    return export


def map_sasi_to_ucanrr_tier(sasi_export: Dict[str, Any]) -> Tuple[int, str]:
    """Derive UCANRR risk_tier / risk_label from the pre-built sasi_export dict."""
    if sasi_export["sasi_crisis_detected"]:
        if sasi_export["sasi_risk_level"] == "imminent":
            return 3, "Extreme Abuse / Crisis"
        return 3, "Crisis"
    if sasi_export["sasi_human_oversight_required"]:
        return 1, "Ambiguous / Monitor"
    return 0, "Normal"


# ---------- System prompt for OpenAI call ----------

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
     - Tier 0–2: "allow"
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
     - 1–3 sentences, concise clinical summary.
     - Do NOT instruct the therapist to call 911 or take specific legal actions.
     - Do NOT mention UCANRR implementation details.

5. EXPLANATION AND DEBUG NOTES
   - "explanation": 1–3 sentences explaining tier choice and key flags.
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


# ---------- FastAPI app ----------

app = FastAPI(
    title="UCANRR + SASI Safety Evaluation API",
    version="2.0.0",
    description=(
        "Evaluates UCANRR journal entries using SASI as a pre-LLM safety gate, "
        "followed by OpenAI structured-output tiering."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://ucanrr.com",
        "https://www.ucanrr.com",
        "https://ucanrr.ngrok-free.dev",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- Main endpoint ----------

@app.post("/safety/analyze_entry", response_model=CombinedSafetyAssessment)
async def analyze_entry(request: JournalEntryRequest):
    """
    Safety pipeline for POST /safety/analyze_entry
    ================================================
    Every step is listed in execution order.  "Blocks" means the HTTP response
    is returned immediately (no further steps run).  "Modifies text" means the
    entry_text seen by downstream steps differs from what the client sent.

    Step 0 — Input validation (FastAPI, synchronous)
        What:   Rejects empty entry_text; rejects if OPENAI_API_KEY or sasi_sdk
                are missing.
        Blocks: Yes → HTTP 400 / 500.
        Modifies text: No.
        Logs:   No explicit logging; FastAPI exception middleware handles it.

    Step 1 — SASI analysis  [sasi.analyze()]
        What:   Calls SasiSession.analyze(message=entry_text.strip(), metadata).
                Populates all sasi_* response fields.
                Also computes share_allowed / share_blocked_reason via
                build_sasi_export() share-gate logic, which checks is_crisis,
                human_oversight_required, mandatory_reporting_flag in that order.
        Blocks: If SASI raises → fail-closed HTTP 500 (share_allowed=False
                is included in the error detail but not in a structured response).
        Modifies text: No.  SASI receives entry_text.strip() unchanged; no
                redaction or rewriting occurs.
        Logs:   None in application code; SASI SDK may log internally.

    Step 2 — Gate check  [if gate_blocked]
        What:   gate_blocked = result.is_crisis OR result.human_oversight_required.
                If True, returns immediately with a SASI-only response:
                  • risk_tier / risk_label come from map_sasi_to_ucanrr_tier()
                    (SASI-derived, NOT from OpenAI).
                  • flags = None, recommendations = None.
                  • share_allowed = False (crisis or pending_human_review).
        Blocks: Yes → full HTTP 200 response, OpenAI is never called.
        Modifies text: No.
        Logs:   None.

    Step 3 — OpenAI structured-output evaluation  [client.chat.completions.create]
        What:   Sends entry_text.strip() + SYSTEM_PROMPT to gpt-4o.
                OpenAI returns risk_tier, risk_label, flags, recommendations,
                explanation, debug_notes.
                Runs ONLY when SASI did not block (gate_blocked=False).
        Blocks: If OpenAI raises → HTTP 500 (no structured response).
        Modifies text: No; OpenAI receives the same stripped entry SASI saw.
        Logs:   None in application code; store=False suppresses OpenAI logging.

    Step 4 — Merge  [return CombinedSafetyAssessment(...)]
        What:   Assembles the final response.  No additional business logic
                beyond field assignment.  There are NO post-rules that reconcile
                disagreements between SASI and OpenAI.
        Blocks: No.
        Modifies text: No.

    ── Decision authority ──────────────────────────────────────────────────────
    Safety decisions that affect the HTTP response come from:
      SASI only  — when gate_blocked=True (is_crisis or human_oversight_required).
      Both layers — when gate_blocked=False (SASI passed, OpenAI ran), with the
                    following field-level precedence:

      Field                             Source (gate_blocked=False)
      ─────────────────────────────────────────────────────────────────────────
      risk_tier / risk_label            OpenAI (LLM assessment)
      flags (JournalSafetyFlags)        OpenAI
      recommendations (all sub-fields)  OpenAI
      explanation / debug_notes         OpenAI
      share_allowed / share_blocked_reason  SASI (sasi_export["share_*"])
      mandatory_reporting_*             SASI (sasi_export["mandatory_*"])
      sasi_crisis_detected              SASI (sasi_export["sasi_crisis_detected"])
      sasi_human_oversight_required     SASI (sasi_export["sasi_human_oversight_required"])
      sasi_risk_level                   SASI (sasi_export["sasi_risk_level"])
      sasi_flag_*                       SASI (sasi_export["sasi_flag_*"])
      sasi_envelope / policy_hash       SASI (sasi_export["sasi_envelope*" / "policy_hash"])
      sasi (nested dict)                SASI — the raw export; batch tester reads this

    ── Known divergence scenario (no automatic reconciliation) ─────────────────
    When SASI passes (gate_blocked=False) but OpenAI escalates the risk:
      • share_allowed may be True (SASI found no crisis / mandatory-report flag)
        while recommendations.partner_share_policy is "block" (OpenAI tier-3).
      • risk_tier from OpenAI (e.g. 3) may differ from what SASI's risk_level
        implies.
    In both cases the response exposes BOTH values; the caller (UI layer) is
    responsible for applying precedence.  If you want server-side reconciliation,
    add a post-merge rule here before the return statement.
    """
    import sys as _sys

    if not request.entry_text or not request.entry_text.strip():
        raise HTTPException(status_code=400, detail="entry_text must not be empty.")

    if not os.environ.get("OPENAI_API_KEY"):
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY is not set in the environment.")

    if not SASI_AVAILABLE:
        raise HTTPException(status_code=500, detail="sasi_sdk is not installed.")

    # ── Step 1: SASI gate ─────────────────────────────────────────────────────
    try:
        _sasi_session = build_sasi_session(request.user_id)
        sasi_result = _sasi_session.analyze(
            message=request.entry_text.strip(),
            metadata={
                "conversation_id": request.session_id,
                "partner_id": request.partner_id,
            },
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "sasi_analysis_failed",
                "message": f"Safety gate could not be completed. Entry blocked. ({exc})",
                "share_allowed": False,
            },
        )

    # ── Build the authoritative export dict ONCE, right here ─────────────────
    # This is the only place SasiResult attributes are read.  Everything below
    # derives from sasi_export, not from sasi_result directly.
    sasi_export = build_sasi_export(sasi_result)

    # ── Dev logging: first successful analyze per process ─────────────────────
    # Prints the export dict so you can verify the real SasiResult shape before
    # trusting the CSV.  Check stderr after the first batch row.
    global _sasi_logged
    if not _sasi_logged:
        print(
            "[SASI-EXPORT first-response]\n"
            + json.dumps(sasi_export, default=str, indent=2)[:2000],
            file=_sys.stderr,
            flush=True,
        )
        _sasi_logged = True

    # ── Step 2: SASI blocks → early return ───────────────────────────────────
    gate_blocked: bool = (
        sasi_export["sasi_crisis_detected"]
        or sasi_export["sasi_human_oversight_required"]
    )
    if gate_blocked:
        sasi_export["sasi_gate_blocked"] = True
        sasi_tier, sasi_tier_label = map_sasi_to_ucanrr_tier(sasi_export)
        return CombinedSafetyAssessment(
            # UCANRR tier derived from SASI (no OpenAI call)
            risk_tier=sasi_tier,
            risk_label=sasi_tier_label,
            flags=None,
            recommendations=None,
            explanation="Entry blocked by SASI safety gate before LLM evaluation.",
            debug_notes=None,
            # Top-level SASI fields — all sourced from sasi_export
            sasi_crisis_detected=sasi_export["sasi_crisis_detected"],
            sasi_human_oversight_required=sasi_export["sasi_human_oversight_required"],
            sasi_oversight_type=sasi_export["sasi_oversight_type"],
            sasi_risk_level=sasi_export["sasi_risk_level"],
            sasi_gate_blocked=True,
            share_allowed=sasi_export["share_allowed"],
            share_blocked_reason=sasi_export["share_blocked_reason"],
            mandatory_reporting_flag=sasi_export["mandatory_reporting_flag"],
            mandatory_reporting_categories=sasi_export["mandatory_reporting_categories"],
            sasi_envelope_id=sasi_export["sasi_envelope_id"],
            policy_hash=sasi_export["policy_hash"],
            sasi_envelope=sasi_export["sasi_envelope"],
            sasi_flag_crisis=sasi_export["sasi_flag_crisis"],
            sasi_flag_human_oversight=sasi_export["sasi_flag_human_oversight"],
            sasi_flag_should_block=sasi_export["sasi_flag_should_block"],
            sasi_flag_pii_detected=sasi_export["sasi_flag_pii_detected"],
            sasi_flag_show_hotline=sasi_export["sasi_flag_show_hotline"],
            sasi_flag_operator_crisis=sasi_export["sasi_flag_operator_crisis"],
            sasi_flags={
                "parent_alert":         str(sasi_export["sasi_flag_parent_alert"]),
                "human_review":         str(sasi_export["sasi_flag_human_review"]),
                "action_rationale":     sasi_export["sasi_flag_action_rationale"],
                "principle_triggered":  sasi_export["sasi_flag_principle_triggered"],
            },
            sasi=sasi_export,
        )

    # ── Step 3: SASI passes → OpenAI evaluation ──────────────────────────────
    safety_id = request.user_hash or request.user_id

    try:
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": request.entry_text.strip()},
            ],
            response_format={"type": "json_schema", "json_schema": SAFETY_JSON_SCHEMA},
            safety_identifier=safety_id,
            store=False,
            temperature=0,
        )
        assessment_dict = json.loads(completion.choices[0].message.content)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Error calling OpenAI safety model: {exc}",
        )

    # ── Step 4: Merge — sasi_export remains authoritative for all SASI cols ───
    sasi_tier, sasi_tier_label = map_sasi_to_ucanrr_tier(sasi_export)
    return CombinedSafetyAssessment(
        # UCANRR fields from OpenAI
        risk_tier=assessment_dict["risk_tier"],
        risk_label=assessment_dict["risk_label"],
        flags=JournalSafetyFlags(**assessment_dict["flags"]),
        recommendations=JournalRecommendations(**assessment_dict["recommendations"]),
        explanation=assessment_dict.get("explanation"),
        debug_notes=assessment_dict.get("debug_notes"),
        # Top-level SASI fields — all sourced from sasi_export (gate_blocked=False)
        sasi_crisis_detected=sasi_export["sasi_crisis_detected"],
        sasi_human_oversight_required=sasi_export["sasi_human_oversight_required"],
        sasi_oversight_type=sasi_export["sasi_oversight_type"],
        sasi_risk_level=sasi_export["sasi_risk_level"],
        sasi_gate_blocked=False,
        share_allowed=sasi_export["share_allowed"],
        share_blocked_reason=sasi_export["share_blocked_reason"],
        mandatory_reporting_flag=sasi_export["mandatory_reporting_flag"],
        mandatory_reporting_categories=sasi_export["mandatory_reporting_categories"],
        sasi_envelope_id=sasi_export["sasi_envelope_id"],
        policy_hash=sasi_export["policy_hash"],
        sasi_envelope=sasi_export["sasi_envelope"],
        sasi_flag_crisis=sasi_export["sasi_flag_crisis"],
        sasi_flag_human_oversight=sasi_export["sasi_flag_human_oversight"],
        sasi_flag_should_block=sasi_export["sasi_flag_should_block"],
        sasi_flag_pii_detected=sasi_export["sasi_flag_pii_detected"],
        sasi_flag_show_hotline=sasi_export["sasi_flag_show_hotline"],
        sasi_flag_operator_crisis=sasi_export["sasi_flag_operator_crisis"],
        sasi_flags={
            "parent_alert":         str(sasi_export["sasi_flag_parent_alert"]),
            "human_review":         str(sasi_export["sasi_flag_human_review"]),
            "action_rationale":     sasi_export["sasi_flag_action_rationale"],
            "principle_triggered":  sasi_export["sasi_flag_principle_triggered"],
        },
        sasi=sasi_export,
    )


@app.get("/health")
async def health_check():
    version = "unavailable"
    if SASI_AVAILABLE:
        try:
            import sasi_sdk
            version = sasi_sdk.__version__
        except Exception:
            pass
    return {"status": "ok", "sasi_version": version}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("ucanrr_sasi_safety_eval_api:app", host="0.0.0.0", port=3000, reload=True)
