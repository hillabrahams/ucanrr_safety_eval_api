# Safety Pipeline Audit — POST /safety/analyze_entry

Generated from code review of `ucanrr_sasi_safety_eval_api.py`, `safety_sasi.py`,
`safety_ucanrr.py`.  Update this document whenever the pipeline changes.

Last updated: 2026-04-03 — added startup guard (step S) and crisis reconciliation rule (step 3b).

---

## Ordered pipeline steps

| # | Step | Code location | Blocks? | Modifies text? | Logs? |
|---|------|---------------|---------|----------------|-------|
| S | **Startup guard** | `_startup_guard()` (`@app.on_event("startup")`) | No | No | Always: prints `[STARTUP] module=ucanrr_sasi_safety_eval_api version=… SASI_AVAILABLE=…` to **stderr**. Absence of this line means the wrong server module is running. |
| 0 | **Input validation** | `analyze_entry()` guard clauses | Yes → HTTP 400/500 on empty text, missing `OPENAI_API_KEY`, or `sasi_sdk` absent | No | No |
| 1 | **SASI analysis** | `safety_sasi.run_sasi()` → `SasiSession.analyze()` | Yes → HTTP 500 (fail-closed) if SASI raises | No — receives `entry_text.strip()` unchanged; no redaction or rewriting | First call only: prints `sasi_export` dict to **stderr** (`[SASI first-response export]`) |
| 1b | **SASI export build** | `safety_sasi._build_export(result)` | No | No | Prints warning to stderr if required booleans are not `bool` type |
| 2 | **SASI gate check** | `if gate_blocked:` in `analyze_entry()` | Yes → HTTP 200 early return; OpenAI is **never called** | No | No |
| 3 | **OpenAI LLM evaluation** | `safety_ucanrr.run_ucanrr_llm()` | Yes → HTTP 500 if OpenAI raises | No — receives same stripped text SASI saw | No (`store=False` suppresses OpenAI logging) |
| 3b | **Reconciliation** | `if assessment["risk_tier"] == 3 and sasi_export["share_allowed"]:` | No | No | Yes → prints `[RECONCILE]` to **stderr** when rule fires, showing both SASI and OpenAI values |
| 4 | **Merge + return** | `_assemble_response()` | No | No | No |

**gate_blocked** is `True` iff `sasi_export["sasi_crisis_detected"] OR sasi_export["sasi_human_oversight_required"]`.

---

## Decision authority

**Safety decisions that affect the HTTP response come from: both SASI and OpenAI, with the following field-level precedence.**

| Response field | Source when `gate_blocked=FALSE` | Source when `gate_blocked=TRUE` |
|---|---|---|
| `risk_tier` | **OpenAI** (`assessment["risk_tier"]`) | **SASI** (`map_sasi_to_ucanrr_tier`) |
| `risk_label` | **OpenAI** | **SASI** |
| `flags` (all sub-fields) | **OpenAI** | `None` (not computed) |
| `recommendations` (all sub-fields) | **OpenAI** | `None` (not computed) |
| `explanation` | **OpenAI** | Hard-coded: "Entry blocked by SASI safety gate…" |
| `debug_notes` | **OpenAI** | `None` |
| `share_allowed` | **SASI then reconciled** — SASI sets it; step 3b may override to `False` | **SASI** |
| `share_blocked_reason` | **SASI then reconciled** — may become `"openai_crisis_escalation"` | **SASI** |
| `mandatory_reporting_*` | **SASI** | **SASI** |
| `sasi_risk_level` | **SASI** | **SASI** |
| `sasi_crisis_detected` | **SASI** | **SASI** |
| `sasi_human_oversight_required` | **SASI** | **SASI** |
| `sasi_gate_blocked` | `False` (OpenAI ran) | `True` |
| All `sasi_flag_*` | **SASI** | **SASI** |
| `sasi_envelope` / `sasi_envelope_id` | **SASI** | **SASI** |
| `policy_hash` | **SASI** | **SASI** |
| `sasi` (nested dict) | **SASI** — single source for batch tester | **SASI** |

---

## Precedence when SASI and OpenAI disagree

One reconciliation rule is active (step 3b). Remaining divergences are
surfaced in the response but not resolved server-side.

### Active rule — crisis OR wins (`share_allowed`)

**Trigger:** `gate_blocked=False` AND `assessment["risk_tier"] == 3` AND `sasi_export["share_allowed"] == True`

**Action:** Override `share_allowed → False`, `share_blocked_reason → "openai_crisis_escalation"` on a *copy* of `sasi_export` before passing to `_assemble_response`. The original SASI values (`sasi_flag_crisis`, `sasi_risk_level`, etc.) are unchanged — they remain the raw SASI output for audit.

**Rationale:** OpenAI tier 3 means the LLM assessed the entry as crisis or extreme abuse. Allowing sharing in that case contradicts the product intent even if SASI's pre-LLM gate did not fire.

**Logged to stderr:**

```text
[RECONCILE] SASI share_allowed=True overridden → False (OpenAI risk_tier=3 / crisis; SASI risk_level=safe)
```

### Remaining unreconciled divergences

| Field | SASI value | OpenAI value | Resolution |
| ----- | ---------- | ------------ | ---------- |
| `risk_tier` / `risk_label` | implied by `sasi_risk_level` | `assessment["risk_tier/label"]` | OpenAI wins (authoritative for tiering when gate passes) |
| `recommendations.partner_share_policy` | n/a | `"block"` or `"warn"` | OpenAI wins (this field is OpenAI-only) |
| `sasi_risk_level` vs `api_risk_tier` | e.g. `"safe"` | e.g. `3` | Both present in response; no further rule |

---

## Share-gate logic (SASI-owned, always runs)

Located in `safety_sasi._build_export()`.  Evaluated against SasiResult in
**every** code path (blocked and passed):

```
if result.is_crisis            → share_allowed=False, reason="crisis_detected"
elif result.human_oversight_required → share_allowed=False, reason="pending_human_review"
elif result.mandatory_reporting_flag → share_allowed=False, reason="mandatory_reporting_obligation"
else                           → share_allowed=True, reason=None
```

OpenAI output does **not** feed into `share_allowed`.

---

## Text modification: none

No step redacts, rewrites, or truncates `entry_text`.  Both SASI and OpenAI
receive `entry_text.strip()` as-is.
