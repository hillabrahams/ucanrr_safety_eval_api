"""
UCANRR + SASI Safety Evaluation API  (thin orchestrator)
---------------------------------------------------------

FastAPI service that runs SASI as a pre-LLM safety gate, then — if the gate
passes — calls the OpenAI API for the full UCANRR tiered assessment.

Pipeline (in execution order):
  0) Input validation
  1) SASI analysis  →  safety_sasi.run_sasi()
  2) Gate check: if SASI blocks → return immediately (no OpenAI call)
  3) OpenAI structured-output evaluation  →  safety_ucanrr.run_ucanrr_llm()
  4) Merge SASI + UCANRR into CombinedSafetyAssessment and return

Safety logic lives in:
  safety_sasi.py     — all SasiSession / SasiResult interaction
  safety_ucanrr.py   — SYSTEM_PROMPT, JSON schema, LLM call, tier mapping

This file owns only: Pydantic models, FastAPI app + middleware, and the
two-step orchestration inside analyze_entry().

Requires:
    pip install fastapi uvicorn "openai>=1.0.0" pydantic sasi-sdk python-dotenv

Run:
    export OPENAI_API_KEY="YOUR_KEY"
    uvicorn ucanrr_sasi_safety_eval_api:app --host 0.0.0.0 --port 3000 --reload

See docs/SAFETY_PIPELINE_AUDIT.md for the full pipeline decision table.
"""

import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from openai import OpenAI

from safety_sasi import run_sasi, SASI_AVAILABLE
from safety_ucanrr import run_ucanrr_llm, map_sasi_to_ucanrr_tier

# ---------- environment ----------

load_dotenv()

OPENAI_MODEL = "gpt-4o"
_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


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


# ---------- Response models ----------

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
    # ── UCANRR tier ───────────────────────────────────────────────────────────
    # Source: OpenAI when gate_blocked=False; map_sasi_to_ucanrr_tier() when True.
    risk_tier: int = Field(..., ge=0, le=3)
    risk_label: str
    # None when SASI gate blocked (OpenAI not called)
    flags: Optional[JournalSafetyFlags] = None
    recommendations: Optional[JournalRecommendations] = None
    explanation: Optional[str] = None
    debug_notes: Optional[str] = None

    # ── SASI outcomes — always from SasiResult via safety_sasi.run_sasi() ─────
    sasi_crisis_detected: bool
    sasi_human_oversight_required: bool
    sasi_oversight_type: Optional[str] = None
    # risk_level string (e.g. "safe", "low", "moderate", "high", "imminent")
    sasi_risk_level: str
    # True iff is_crisis or human_oversight_required caused early return
    sasi_gate_blocked: bool

    # ── Share gate — SASI-owned in all code paths ─────────────────────────────
    share_allowed: bool
    share_blocked_reason: Optional[str] = None

    # ── Mandatory reporting — SASI only ───────────────────────────────────────
    mandatory_reporting_flag: bool
    mandatory_reporting_categories: List[str]

    # ── Audit ─────────────────────────────────────────────────────────────────
    sasi_envelope_id: Optional[str] = None
    policy_hash: Optional[str] = None
    # Envelope as JSON string (no raw user text). None when absent.
    sasi_envelope: Optional[str] = None

    # ── SASI direct flags — 1-to-1 from SasiResult ───────────────────────────
    sasi_flag_crisis: bool
    sasi_flag_human_oversight: bool
    sasi_flag_should_block: Optional[bool] = None
    sasi_flag_pii_detected: Optional[bool] = None
    sasi_flag_show_hotline: Optional[bool] = None
    sasi_flag_operator_crisis: Optional[bool] = None

    # ── SASI ancillary flags (string/bool) ────────────────────────────────────
    sasi_flags: Dict[str, Optional[str]]

    # ── Authoritative SASI export dict — single source for batch tester ───────
    # Keys match CSV column names exactly (see docs/OUTPUT_COLUMNS.md).
    # Batch tester reads assessment["sasi"][key] for all sasi_* columns.
    sasi: Dict[str, Any] = Field(default_factory=dict)


# ---------- FastAPI app ----------

app = FastAPI(
    title="UCANRR + SASI Safety Evaluation API",
    version="2.2.0",
    description=(
        "Evaluates UCANRR journal entries using SASI as a pre-LLM safety gate, "
        "followed by OpenAI structured-output tiering. "
        "See docs/SAFETY_PIPELINE_AUDIT.md for decision authority."
    ),
)


@app.on_event("startup")
async def _startup_guard() -> None:
    """Print module identity to stderr on startup.

    Prevents silent wrong-server failures: if the old ucanrr_safety_eval_api.py
    is running instead, this line will not appear in the logs and the health
    endpoint will not include sasi_version.
    """
    import sys as _sys
    import sasi_sdk as _sasi_sdk  # already imported transitively; just for version

    print(
        f"[STARTUP] module=ucanrr_sasi_safety_eval_api  version=2.2.0"
        f"  SASI_AVAILABLE={SASI_AVAILABLE}"
        f"  sasi_sdk={_sasi_sdk.__version__ if SASI_AVAILABLE else 'n/a'}",
        file=_sys.stderr,
        flush=True,
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


# ---------- Shared response assembler ----------

def _assemble_response(
    sasi_export: Dict[str, Any],
    risk_tier: int,
    risk_label: str,
    flags: Optional[JournalSafetyFlags],
    recommendations: Optional[JournalRecommendations],
    explanation: Optional[str],
    debug_notes: Optional[str],
    gate_blocked: bool,
) -> CombinedSafetyAssessment:
    """Build CombinedSafetyAssessment from sasi_export + UCANRR fields.

    All SASI values come exclusively from sasi_export (built by
    safety_sasi.run_sasi()).  This function is called for both the early-return
    (gate_blocked=True) and the full-pipeline (gate_blocked=False) paths,
    keeping both branches identical in their SASI wiring.
    """
    return CombinedSafetyAssessment(
        risk_tier=risk_tier,
        risk_label=risk_label,
        flags=flags,
        recommendations=recommendations,
        explanation=explanation,
        debug_notes=debug_notes,
        sasi_crisis_detected=sasi_export["sasi_crisis_detected"],
        sasi_human_oversight_required=sasi_export["sasi_human_oversight_required"],
        sasi_oversight_type=sasi_export["sasi_oversight_type"],
        sasi_risk_level=sasi_export["sasi_risk_level"],
        sasi_gate_blocked=gate_blocked,
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
            "parent_alert":        str(sasi_export["sasi_flag_parent_alert"]),
            "human_review":        str(sasi_export["sasi_flag_human_review"]),
            "action_rationale":    sasi_export["sasi_flag_action_rationale"],
            "principle_triggered": sasi_export["sasi_flag_principle_triggered"],
        },
        sasi={**sasi_export, "sasi_gate_blocked": gate_blocked},
    )


# ---------- Main endpoint ----------

@app.post("/safety/analyze_entry", response_model=CombinedSafetyAssessment)
async def analyze_entry(request: JournalEntryRequest):
    """
    Safety pipeline — see docs/SAFETY_PIPELINE_AUDIT.md for full audit.

    Step 0  Input validation    blocks on empty text / missing env
    Step 1  SASI gate           safety_sasi.run_sasi()  [fail-closed]
    Step 2  Gate check          early return if is_crisis or human_oversight_required
    Step 3  OpenAI LLM          safety_ucanrr.run_ucanrr_llm()  [only if SASI passes]
    Step 4  Merge + return

    Decision authority
    ------------------
    gate_blocked=True  → SASI only (risk_tier/label from map_sasi_to_ucanrr_tier)
    gate_blocked=False → risk_tier/label/flags/recommendations from OpenAI;
                         share_allowed and all sasi_* from SASI, then patched
                         by the reconciliation rule (Step 3b) if needed.

    Reconciliation rule (Step 3b) — crisis OR wins:
        If OpenAI returns risk_tier == 3 (crisis or extreme_abuse) AND
        sasi_export["share_allowed"] is True, override share_allowed to False
        with share_blocked_reason = "openai_crisis_escalation".
        All other sasi_* fields remain SASI-authoritative.
    """
    if not request.entry_text or not request.entry_text.strip():
        raise HTTPException(status_code=400, detail="entry_text must not be empty.")

    if not os.environ.get("OPENAI_API_KEY"):
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY is not set in the environment.")

    if not SASI_AVAILABLE:
        raise HTTPException(status_code=500, detail="sasi_sdk is not installed.")

    # ── Step 1: SASI ─────────────────────────────────────────────────────────
    try:
        sasi_export = run_sasi(
            user_id=request.user_id,
            text=request.entry_text.strip(),
            session_id=request.session_id,
            partner_id=request.partner_id,
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

    # ── Step 2: Gate check ────────────────────────────────────────────────────
    gate_blocked: bool = (
        sasi_export["sasi_crisis_detected"]
        or sasi_export["sasi_human_oversight_required"]
    )
    if gate_blocked:
        tier, label = map_sasi_to_ucanrr_tier(sasi_export)
        return _assemble_response(
            sasi_export=sasi_export,
            risk_tier=tier,
            risk_label=label,
            flags=None,
            recommendations=None,
            explanation="Entry blocked by SASI safety gate before LLM evaluation.",
            debug_notes=None,
            gate_blocked=True,
        )

    # ── Step 3: OpenAI LLM ───────────────────────────────────────────────────
    safety_id = request.user_hash or request.user_id
    try:
        assessment = run_ucanrr_llm(
            text=request.entry_text.strip(),
            safety_id=safety_id,
            client=_client,
            model=OPENAI_MODEL,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Error calling OpenAI safety model: {exc}",
        )

    # ── Step 3b: Reconciliation — crisis OR wins ─────────────────────────────
    # Rule: if OpenAI escalates to tier 3 (crisis/extreme_abuse) but SASI
    # passed (share_allowed=True), the stricter decision wins: block sharing.
    # We patch a copy of sasi_export so the original SASI values remain
    # visible for audit; share_blocked_reason records why it changed.
    if assessment["risk_tier"] == 3 and sasi_export["share_allowed"]:
        import sys as _sys
        print(
            f"[RECONCILE] SASI share_allowed=True overridden → False "
            f"(OpenAI risk_tier=3 / {assessment['risk_label']}; "
            f"SASI risk_level={sasi_export['sasi_risk_level']})",
            file=_sys.stderr,
            flush=True,
        )
        sasi_export = {
            **sasi_export,
            "share_allowed": False,
            "share_blocked_reason": "openai_crisis_escalation",
        }

    # ── Step 4: Merge ─────────────────────────────────────────────────────────
    return _assemble_response(
        sasi_export=sasi_export,
        risk_tier=assessment["risk_tier"],
        risk_label=assessment["risk_label"],
        flags=JournalSafetyFlags(**assessment["flags"]),
        recommendations=JournalRecommendations(**assessment["recommendations"]),
        explanation=assessment.get("explanation"),
        debug_notes=assessment.get("debug_notes"),
        gate_blocked=False,
    )


# ---------- Health ----------

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
