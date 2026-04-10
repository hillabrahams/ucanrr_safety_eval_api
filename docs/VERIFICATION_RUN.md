# Verification Run

Date: 2026-04-02  
Server: `uvicorn ucanrr_sasi_safety_eval_api:app --port 3000` (v2.1.0)  
SASI SDK: 1.6.2

---

## Step 1 — Correct server running

```
GET http://127.0.0.1:3000/health
{"status": "ok", "sasi_version": "1.6.2"}

GET http://127.0.0.1:3000/openapi.json
→ info.version: "2.1.0"
→ schemas: CombinedSafetyAssessment (26 fields including "sasi" nested dict)
```

**Previous failure mode:** the old `ucanrr_safety_eval_api.py` (no SASI, response model
`SafetyAssessment`) was running on port 3000 instead.  Fix: always start with
`uvicorn ucanrr_sasi_safety_eval_api:app --port 3000`.

---

## Step 2 — Minimal POST (curl proof)

```bash
curl -s -X POST http://127.0.0.1:3000/safety/analyze_entry \
  -H "Content-Type: application/json" \
  -d '{"entry_text": "I feel overwhelmed today.", "user_id": "curl-test-01"}' \
  | python -m json.tool
```

### JSON response (redacted — sasi_envelope truncated, no PII in this entry)

```json
{
  "risk_tier": 0,
  "risk_label": "normal",
  "sasi_crisis_detected": false,
  "sasi_human_oversight_required": false,
  "sasi_risk_level": "moderate",
  "sasi_gate_blocked": false,
  "share_allowed": true,
  "share_blocked_reason": null,
  "mandatory_reporting_flag": false,
  "mandatory_reporting_categories": [],
  "sasi_envelope_id": "ef39d11f2ea046ecb1ecc73a820bd61a",
  "policy_hash": null,
  "sasi_envelope": "{\"envelope_version\": \"1.0\", \"sdk_version\": \"1.6.2\", ... }",
  "sasi_flag_crisis": false,
  "sasi_flag_human_oversight": false,
  "sasi_flag_should_block": false,
  "sasi_flag_pii_detected": false,
  "sasi_flag_show_hotline": false,
  "sasi_flag_operator_crisis": false,
  "sasi_flags": {
    "parent_alert": "False",
    "human_review": "False",
    "action_rationale": "Moderate distress indicators detected. Risk level: MODERATE. Empathetic response recommended.",
    "principle_triggered": "Standard Safety Protocol"
  },
  "sasi": {
    "sasi_risk_level": "moderate",
    "sasi_crisis_detected": false,
    "sasi_human_oversight_required": false,
    "sasi_oversight_type": null,
    "sasi_gate_blocked": false,
    "share_allowed": true,
    "share_blocked_reason": null,
    "mandatory_reporting_flag": false,
    "mandatory_reporting_categories": [],
    "sasi_envelope_id": "ef39d11f2ea046ecb1ecc73a820bd61a",
    "policy_hash": null,
    "sasi_envelope": "{ ... }",
    "sasi_flag_crisis": false,
    "sasi_flag_human_oversight": false,
    "sasi_flag_should_block": false,
    "sasi_flag_pii_detected": false,
    "sasi_flag_show_hotline": false,
    "sasi_flag_operator_crisis": false,
    "sasi_flag_parent_alert": false,
    "sasi_flag_human_review": false,
    "sasi_flag_action_rationale": "Moderate distress indicators detected. Risk level: MODERATE. Empathetic response recommended.",
    "sasi_flag_principle_triggered": "Standard Safety Protocol"
  }
}
```

**SASI fields present: YES** — top-level `sasi_*` keys and `assessment["sasi"]`
nested dict both populated.

---

## Step 3 — Batch run `--limit 3`

```
python ui_simulator2_sasi.py --limit 3
Output: sasi_test_result_20260402_210806.csv
```

Console output:
```
[   1/3] JE0001  band=harm  risk=4  user=sim-u-001  OK  share=True  ui_flow=crisis_interstitial
[   2/3] JE0002  band=harm  risk=4  user=sim-u-002  OK  share=True  ui_flow=crisis_interstitial
[   3/3] JE0003  band=harm  risk=4  user=sim-u-003  OK  share=True  ui_flow=crisis_interstitial
Processed: 3 | SASI blocked: 0 | Errors: 0
```

### CSV sasi_* column fill status

| Row | sasi_risk_level | sasi_flag_action_rationale | Empty cols |
|-----|-----------------|---------------------------|------------|
| JE0001 | `safe` | "Standard processing. Risk level: SAFE…" | 4 (legitimately null — see below) |
| JE0002 | `moderate` | "Moderate distress indicators detected…" | 4 |
| JE0003 | `moderate` | "Moderate distress indicators detected…" | 4 |

**Column layout verified:** Block 1 (cols 0–8 benchmark), Block 2 (cols 9–12 in_*),
Block 3 (cols 13–34 sasi_*), Block 4 (cols 35–56 api_* + meta).

### The 4 legitimately empty columns

These are null by SDK contract, not bugs:

| Column | Reason |
|--------|--------|
| `sasi_oversight_type` | `result.oversight_type = None` — no oversight triggered on passing entries |
| `share_blocked_reason` | `None` when `share_allowed=True` |
| `mandatory_reporting_categories` | Empty list `[]` → joined as `""` |
| `policy_hash` | SDK returns `envelope.policy_hash = null` for this operator config |

---

## Step 4 — Pytest

pytest is **not installed** in this environment (`ModuleNotFoundError: No module named 'pytest'`).
No test files exist.  A minimal integration test stub has been written to
`tests/test_analyze_entry.py` (see file) but cannot be run until pytest is
installed via `pip install pytest httpx`.

---

## Divergence note (expected, not a bug)

For all 3 rows: `sasi_risk_level` = `"safe"` or `"moderate"` (SASI passed) but
`api_risk_tier` = `3` and `api_risk_label` = `"crisis"` (OpenAI escalated).
`share_allowed` = `True` (SASI), `api_partner_share_policy` = `"warn"` or `"block"`
(OpenAI).  This is the documented divergence in `docs/SAFETY_PIPELINE_AUDIT.md` —
no reconciliation rule exists today.
