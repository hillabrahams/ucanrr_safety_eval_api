# Output Column Reference — sasi_test_result_*.csv

Produced by `ui_simulator2_sasi.py`.  Column groups listed in CSV order.

---

## Column authority legend

| Tag | Meaning |
|-----|---------|
| **benchmark** | Read from input CSV; reference labels assigned before the API run. Never recomputed by the API. |
| **in** | Request parameter echoed for traceability. |
| **sasi** | Sourced from `SasiResult` via `safety_sasi.run_sasi()`. Present in every HTTP 200 response. Read from `assessment["sasi"][key]`. |
| **api-openai** | Sourced from the OpenAI LLM call. Empty/None when `sasi_gate_blocked=TRUE` (OpenAI was not called). |
| **api-merged** | Sourced from SASI when gate blocks, from OpenAI when gate passes. |

---

## Block 1 — Benchmark / input metadata

These columns travel from the input CSV unchanged.  They are **not** computed
by the API and do not reflect live SASI or UCANRR output.

| Column | Type | Authority | Notes |
|--------|------|-----------|-------|
| `id` | string | **benchmark** | Row identifier (e.g. `JE0001`) |
| `ucanrr_score` | float | **benchmark** | Pre-assigned UCANRR risk score |
| `ucanrr_band` | string | **benchmark** | Risk band label (e.g. `harm`, `care`) |
| `score_bucket` | string | **benchmark** | Score bucket label |
| `primary_label` | string | **benchmark** | Expected safety label for this entry |
| `risk_level` | int/string | **benchmark** | Pre-assigned risk level integer/label |
| `tags` | string | **benchmark** | Semicolon-separated tags |
| `addressed_to` | string | **benchmark** | Who the entry is addressed to |
| `style` | string | **benchmark** | Writing style label |

---

## Block 2 — Request parameters (`in_*`)

| Column | Type | Authority | Notes |
|--------|------|-----------|-------|
| `in_user_id` | string | **in** | `user_id` sent in request body |
| `in_partner_id` | string | **in** | `partner_id` sent in request body |
| `in_session_id` | string | **in** | `session_id` sent in request body |
| `in_share_requested` | bool | **in** | `share_requested` sent in request body |

---

## Block 3 — SASI fields (`sasi_*` and share/mandatory)

All populated from `assessment["sasi"]` — the dict built by
`safety_sasi._build_export()` immediately after `SasiSession.analyze()`.
Present in **every** HTTP 200 response regardless of whether OpenAI ran.
No value in this block comes from OpenAI.

| Column | Type | Authority | SasiResult attribute | Notes |
|--------|------|-----------|----------------------|-------|
| `sasi_risk_level` | string | **sasi** | `result.risk_level` (enum→`.name.lower()`) | e.g. `"safe"`, `"moderate"`, `"high"`, `"imminent"` |
| `sasi_crisis_detected` | bool | **sasi** | `result.is_crisis` | |
| `sasi_human_oversight_required` | bool | **sasi** | `result.human_oversight_required` | |
| `sasi_oversight_type` | string/null | **sasi** | `result.oversight_type` | Null when no oversight triggered |
| `sasi_gate_blocked` | bool | **sasi** | derived | True iff OpenAI call was skipped |
| `share_allowed` | bool | **sasi** | derived in `_build_export` | False if crisis / oversight / mandatory |
| `share_blocked_reason` | string/null | **sasi** | derived | `"crisis_detected"` \| `"pending_human_review"` \| `"mandatory_reporting_obligation"` \| null |
| `mandatory_reporting_flag` | bool | **sasi** | `result.mandatory_reporting_flag` | |
| `mandatory_reporting_categories` | string | **sasi** | `result.mandatory_reporting_categories` | Semicolon-joined list |
| `sasi_envelope_id` | string/null | **sasi** | `result.envelope.run_id` | Null when envelope absent |
| `policy_hash` | string/null | **sasi** | `result.envelope.policy_hash` or `result.policy_hash` | |
| `sasi_envelope` | JSON string/null | **sasi** | `result.envelope.to_dict()` | Raw user text keys stripped; null when absent |
| `sasi_flag_crisis` | bool | **sasi** | `result.is_crisis` | Mirrors `sasi_crisis_detected` |
| `sasi_flag_human_oversight` | bool | **sasi** | `result.human_oversight_required` | Mirrors `sasi_human_oversight_required` |
| `sasi_flag_should_block` | bool/null | **sasi** | `result.should_block` | |
| `sasi_flag_pii_detected` | bool/null | **sasi** | `result.pii_detected` | |
| `sasi_flag_show_hotline` | bool/null | **sasi** | `result.show_hotline` | |
| `sasi_flag_operator_crisis` | bool/null | **sasi** | `result.operator_crisis` | |
| `sasi_flag_parent_alert` | bool/null | **sasi** | `result.parent_alert_flag` | |
| `sasi_flag_human_review` | bool/null | **sasi** | `result.human_review_flag` | |
| `sasi_flag_action_rationale` | string/null | **sasi** | `result.action_rationale` | Human-readable rationale from SASI |
| `sasi_flag_principle_triggered` | string/null | **sasi** | `result.principle_triggered` | Which SASI principle fired |

---

## Block 4 — API / merged fields (`api_*` and `flag_*`)

| Column | Type | Authority | Notes |
|--------|------|-----------|-------|
| `api_risk_tier` | int | **api-merged** | 0–3; from OpenAI when gate passes, from `map_sasi_to_ucanrr_tier` when blocked |
| `api_risk_label` | string | **api-merged** | `"normal"` \| `"ambiguous_monitor"` \| `"heated"` \| `"crisis"` \| `"extreme_abuse"` (OpenAI) or `"Crisis"` / `"Ambiguous / Monitor"` / `"Normal"` (SASI-derived) |
| `flag_suicidal_ideation` | bool/null | **api-openai** | Empty when `sasi_gate_blocked=TRUE` |
| `flag_self_harm` | bool/null | **api-openai** | Empty when blocked |
| `flag_other_harm` | bool/null | **api-openai** | Empty when blocked |
| `flag_extreme_abuse` | bool/null | **api-openai** | Empty when blocked |
| `flag_heated_argument` | bool/null | **api-openai** | Empty when blocked |
| `flag_crisis_language` | bool/null | **api-openai** | Empty when blocked |
| `flag_substance_use` | bool/null | **api-openai** | Empty when blocked |
| `flag_weapon_access` | bool/null | **api-openai** | Empty when blocked |
| `flag_child_safety` | bool/null | **api-openai** | Empty when blocked |
| `flag_ambiguous_lethal_curiosity` | bool/null | **api-openai** | Empty when blocked |
| `api_partner_share_policy` | string/null | **api-openai** | `"allow"` \| `"warn"` \| `"block"`. **May diverge from `share_allowed` (SASI).** No reconciliation rule. Empty when blocked. |
| `api_therapist_share_policy` | string/null | **api-openai** | Empty when blocked |
| `api_show_crisis_banner` | bool/null | **api-openai** | Empty when blocked |
| `api_show_crisis_resources` | bool/null | **api-openai** | Empty when blocked |
| `api_suggested_ui_flow` | string/null | **api-openai** | Empty when blocked |
| `api_mark_as_urgent_for_therapist` | bool/null | **api-openai** | Empty when blocked |
| `api_notes_for_therapist` | string/null | **api-openai** | Empty when blocked |
| `api_explanation` | string/null | **api-openai** | Empty when blocked (replaced by "Entry blocked…" in response body, not in CSV) |

---

## Block 5 — Meta

| Column | Type | Notes |
|--------|------|-------|
| `api_error` | string | Non-empty only when HTTP call failed or exception raised |
| `entry` | string | The journal text from the input CSV (not the API response) |

---

## Known divergence (no server-side reconciliation today)

When `sasi_gate_blocked=FALSE`:

- `share_allowed` (Block 3, SASI) may be `True` while `api_partner_share_policy`
  (Block 4, OpenAI) is `"block"`.
- `api_risk_tier` (OpenAI tier 3) may contradict `sasi_risk_level` (e.g. `"safe"`).

Both values are present in the row so the discrepancy is visible.  See
`docs/SAFETY_PIPELINE_AUDIT.md` for where to add a reconciliation rule.

---

## `compare_*` columns

Not implemented.  A mapping from UCANRR benchmark labels (`primary_label`,
`risk_level`) to SASI or OpenAI output labels is not defined today.  Inventing
one silently would produce misleading agreement metrics.  If you define such a
mapping, add columns `compare_crisis_agreement`, `compare_tier_match`, etc. here
and document the exact rule used.
