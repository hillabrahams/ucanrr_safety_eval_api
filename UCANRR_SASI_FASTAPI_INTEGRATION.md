# UCANRR + SASI Integration (FastAPI)

Production-ready integration template for using SASI as a pre-LLM safety gate in UCANRR workflows.

---

## Drop-in FastAPI example

```python
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, Tuple, List

from sasi_sdk import SasiSession

app = FastAPI(title="UCANRR Safety Evaluation API")


# --- Request / Response Models ---

class JournalEntryRequest(BaseModel):
    entry_text: str
    user_id: str
    partner_id: Optional[str] = None
    session_id: Optional[str] = None
    share_requested: Optional[bool] = False


class SafetyAssessmentResponse(BaseModel):
    # UCANRR tiering
    risk_tier: int
    risk_tier_label: str

    # SASI outcomes
    sasi_crisis_detected: bool
    sasi_human_oversight_required: bool
    sasi_oversight_type: Optional[str]
    sasi_risk_level: str

    # Share gate
    share_allowed: bool
    share_blocked_reason: Optional[str]

    # Placeholder until mandatory-reporting feature ships
    mandatory_reporting_flag: bool
    mandatory_reporting_categories: List[str]

    # Audit refs
    sasi_envelope_id: Optional[str]
    policy_hash: Optional[str]

    # Extra flags
    flags: Dict[str, Optional[str]]


def _risk_level_str(result) -> str:
    # risk_level is enum-like in current SDK
    return getattr(result.risk_level, "value", str(result.risk_level)).lower()


def map_sasi_to_ucanrr_tier(result) -> Tuple[int, str]:
    if result.is_crisis:
        # Keep this simple and deterministic; refine with your own policy later
        if _risk_level_str(result) == "imminent":
            return 3, "Extreme Abuse / Crisis"
        return 2, "Crisis"

    if result.human_oversight_required:
        return 1, "Ambiguous / Monitor"

    return 0, "Normal"


def evaluate_share_gate(result) -> Tuple[bool, Optional[str]]:
    # Hard safety gate for couples-therapy share workflows
    if result.is_crisis:
        return False, "crisis_detected"
    if result.human_oversight_required:
        return False, "pending_human_review"
    if getattr(result, "mandatory_reporting_flag", False):
        return False, "mandatory_reporting_obligation"
    return True, None


def build_sasi_session(user_id: str) -> SasiSession:
    """
    Create a session per user/session boundary.
    Avoid one global singleton for all users.
    """
    return SasiSession(
        user_id=user_id,
        mode="therapist",
        # Recommended in production:
        # config_path="config/sasi.yaml",
        # llm_profile="anthropic",
    )


@app.post("/safety/analyze_entry", response_model=SafetyAssessmentResponse)
async def analyze_entry(request: JournalEntryRequest):
    """
    Flow:
      1) SASI pre-LLM safety analysis
      2) Share gate decision
      3) UCANRR tier mapping
      4) Structured response with audit refs
    """
    try:
        sasi = build_sasi_session(request.user_id)
        result = sasi.analyze(
            message=request.entry_text,
            metadata={
                "conversation_id": request.session_id,
                "partner_id": request.partner_id,
            },
        )
    except Exception:
        # Fail-closed behavior for app-level workflow
        raise HTTPException(
            status_code=500,
            detail={
                "error": "safety_analysis_failed",
                "message": "Safety check could not be completed. Entry blocked.",
                "share_allowed": False,
            },
        )

    risk_tier, risk_tier_label = map_sasi_to_ucanrr_tier(result)
    share_allowed, share_blocked_reason = evaluate_share_gate(result)

    mandatory_flag = getattr(result, "mandatory_reporting_flag", False)
    mandatory_categories = getattr(result, "mandatory_reporting_categories", [])

    envelope = getattr(result, "envelope", None)
    envelope_run_id = getattr(envelope, "run_id", None) if envelope else None
    envelope_policy_hash = getattr(envelope, "policy_hash", None) if envelope else getattr(result, "policy_hash", None)

    return SafetyAssessmentResponse(
        risk_tier=risk_tier,
        risk_tier_label=risk_tier_label,
        sasi_crisis_detected=result.is_crisis,
        sasi_human_oversight_required=result.human_oversight_required,
        sasi_oversight_type=getattr(result, "oversight_type", None),
        sasi_risk_level=_risk_level_str(result),
        share_allowed=share_allowed,
        share_blocked_reason=share_blocked_reason,
        mandatory_reporting_flag=mandatory_flag,
        mandatory_reporting_categories=mandatory_categories,
        sasi_envelope_id=envelope_run_id,
        policy_hash=envelope_policy_hash,
        flags={
            "parent_alert": str(getattr(result, "parent_alert_flag", False)),
            "human_review": str(getattr(result, "human_review_flag", False)),
            "action_rationale": getattr(result, "action_rationale", None),
            "principle_triggered": getattr(result, "principle_triggered", None),
        },
    )


@app.get("/health")
async def health():
    import sasi_sdk

    return {"status": "ok", "sasi_version": sasi_sdk.__version__}
```

---

## Notes for UCANRR

- Keep SASI as pre-LLM gate; run your Care/Neglect/Abuse continuum only when gate allows.
- Prefer persistent session management keyed by `(user_id, session_id)` if you need continuity across turns.
- Never persist raw journal text in safety/audit logs; store only policy-safe metadata and references.

