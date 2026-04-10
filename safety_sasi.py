"""
safety_sasi.py
--------------
Owns all SASI SDK interaction for the UCANRR safety pipeline.
No FastAPI imports.  Consumed by the route in ucanrr_sasi_safety_eval_api.py.

Public API
----------
    run_sasi(user_id, text, session_id, partner_id) -> dict
        Calls SasiSession.analyze() and returns the authoritative sasi_export
        dict whose keys match the CSV column names exactly (see build_sasi_export).

    SASI_AVAILABLE: bool
        True when sasi_sdk is importable.  Callers should check before calling
        run_sasi() and raise an appropriate HTTP error if False.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from sasi_sdk import SasiSession
    SASI_AVAILABLE = True
except ImportError:
    SASI_AVAILABLE = False

# Absolute path — works regardless of uvicorn cwd.
_SASI_CONFIG_PATH = str(Path(__file__).resolve().parent / "config" / "sasi.yaml")

# Keys that could carry raw user text; stripped before serialising envelope.
_ENVELOPE_TEXT_KEYS = frozenset({"message", "text", "content", "input", "entry", "prompt"})

# Per-process flag: print the first export dict to stderr so we can verify the
# real SasiResult attribute shape before trusting the CSV.
_sasi_logged = False


# ---------- Session factory ----------

def _build_session(user_id: Optional[str]) -> "SasiSession":
    """Create a per-request SasiSession (avoid a global singleton).

    user_id – pseudonymous, no PII.  Falls back to SASI_DEFAULT_USER_ID env
              var (intended for local/batch testing only).
    """
    effective = (user_id or "").strip() or os.getenv("SASI_DEFAULT_USER_ID", "local_batch_test")
    return SasiSession(
        user_id=effective,
        mode="therapist",
        config_path=_SASI_CONFIG_PATH,
    )


# ---------- Export builder ----------

def _build_export(result: Any) -> Dict[str, Any]:
    """Read all required SasiResult attributes in one place.

    This is the ONLY function in the codebase that touches SasiResult
    attributes directly.  Any attribute-name mismatch surfaces here, not
    scattered across multiple helper layers.

    Keys match CSV column names exactly so the batch tester can do:
        sasi_export = assessment["sasi"]
        row["sasi_risk_level"] = sasi_export["sasi_risk_level"]
    with zero further translation.
    """
    import sys as _sys

    # ── Risk level: .name (enum) → .value → str() ────────────────────────────
    rl = getattr(result, "risk_level", None)
    if rl is None:
        risk_level_str = "unknown"
    elif hasattr(rl, "name"):
        risk_level_str = rl.name.lower()
    elif hasattr(rl, "value"):
        risk_level_str = str(rl.value).lower()
    else:
        risk_level_str = str(rl).lower()

    # ── Envelope — absent on low-risk responses ───────────────────────────────
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

    # ── Share gate (SASI-owned in all code paths) ─────────────────────────────
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
        # Gate — caller (route) sets to True on early return
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

    # Warn if the two required booleans are not actually bool
    for _req in ("sasi_crisis_detected", "sasi_human_oversight_required"):
        if not isinstance(export[_req], bool):
            print(
                f"[SASI WARNING] {_req} = {export[_req]!r} "
                f"(type {type(export[_req]).__name__}), expected bool. "
                "SasiResult shape may differ from SDK docs.",
                file=_sys.stderr,
            )

    return export


# ---------- Public entry point ----------

def run_sasi(
    user_id: Optional[str],
    text: str,
    session_id: Optional[str] = None,
    partner_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Run SASI analysis and return the export dict.

    Raises RuntimeError if sasi_sdk is unavailable.
    Lets SasiSession exceptions propagate — the caller (route) handles them
    fail-closed.

    Returns a dict whose keys match CSV column names exactly (see _build_export).
    The dict always has sasi_gate_blocked=False; the caller sets it to True when
    choosing an early return.
    """
    import sys as _sys
    global _sasi_logged

    if not SASI_AVAILABLE:
        raise RuntimeError("sasi_sdk is not installed")

    session = _build_session(user_id)
    result = session.analyze(
        message=text,
        metadata={
            "conversation_id": session_id,
            "partner_id": partner_id,
        },
    )

    export = _build_export(result)

    # Dev logging: first successful call per process — check stderr to verify
    # the real SasiResult attribute shape before trusting the CSV.
    if not _sasi_logged:
        print(
            "[SASI first-response export]\n"
            + json.dumps(export, default=str, indent=2)[:2000],
            file=_sys.stderr,
            flush=True,
        )
        _sasi_logged = True

    return export
