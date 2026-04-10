"""
UCANRR Safety Evaluation API — Azure App Service Edition
---------------------------------------------------------

FastAPI service that uses the OpenAI API to evaluate journal entries for:
- Crisis risk (suicidal / self-harm / harm-to-others)
- Extreme abuse / threats
- Heated but non-crisis conflict
- Ambiguous pre-crisis patterns (distress + potentially lethal curiosity)

It returns:
- risk_tier 0–3
- risk_label
- flags (booleans)
- recommendations (how the UI should handle Share to therapist vs partner)
- a short explanation for logs / clinicians

Azure changes vs. local version:
- API key read exclusively from environment (set in Azure App Service > Configuration > App Settings)
- flask.cli.load_dotenv removed; python-dotenv used as local-dev fallback only
- Gunicorn-compatible entry point (no uvicorn --reload in production)
- Azure Application Insights logging via APPLICATIONINSIGHTS_CONNECTION_STRING env var (optional)
- CORS origins updated to include your Azure app URL (replace placeholder below)
- PORT read from environment (Azure sets this automatically)

Requirements:
    pip install fastapi uvicorn gunicorn "openai>=1.0.0" pydantic python-dotenv

Azure startup command (App Service > Configuration > Startup Command):
    gunicorn -w 4 -k uvicorn.workers.UvicornWorker azure_ucanrr_safety_eval_api:app --timeout 120

Environment variables to set in Azure App Service > Configuration > App Settings:
    OPENAI_API_KEY         = <your OpenAI key — store in Key Vault and reference here>
    OPENAI_MODEL           = gpt-4o          (optional, defaults to gpt-4o)
    ALLOWED_ORIGINS        = https://ucanrr.com,https://www.ucanrr.com  (optional override)
    APPLICATIONINSIGHTS_CONNECTION_STRING = <from Azure Monitor>  (optional)
"""

import json
import logging
import os
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from openai import OpenAI

# ---------------------------------------------------------------------------
# Logging — integrates with Azure Monitor when the App Insights SDK is present
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ucanrr_safety_api")

try:
    from opencensus.ext.azure.log_exporter import AzureLogHandler  # type: ignore

    _conn_str = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if _conn_str:
        logger.addHandler(AzureLogHandler(connection_string=_conn_str))
        logger.info("Azure Application Insights logging enabled.")
except ImportError:
    pass  # opencensus optional; standard logging still works


# ---------------------------------------------------------------------------
# Local-dev fallback: load .env when running outside Azure
# (App Service injects env vars directly — .env is never present there)
# ---------------------------------------------------------------------------

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except ImportError:
    pass  # python-dotenv not required in production


# ---------------------------------------------------------------------------
# OpenAI client — key MUST come from environment, never hardcoded
# ---------------------------------------------------------------------------

_api_key = os.environ.get("OPENAI_API_KEY")
if not _api_key:
    logger.warning("OPENAI_API_KEY not set at startup. Requests will fail until it is configured.")

client = OpenAI(api_key=_api_key or "")

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class JournalEntryRequest(BaseModel):
    entry_text: str = Field(..., description="Raw journal entry text from the UCANRR mobile app.")
    user_hash: Optional[str] = Field(
        None,
        description="Stable hash or pseudonymous user id for safety_identifier (no PII)."
    )


class SafetyAssessment(BaseModel):
    risk_tier: int = Field(..., ge=0, le=3, description="0=normal, 1=ambiguous_monitor, 2=heated, 3=crisis_or_extreme_abuse.")
    risk_label: str = Field(..., description="One of: normal, ambiguous_monitor, heated, crisis, extreme_abuse.")
    flags: Dict[str, Any] = Field(..., description="Boolean flags and other fine-grained signals.")
    recommendations: Dict[str, Any] = Field(..., description="Recommended UI & routing behavior for UCANRR.")
    explanation: str = Field(..., description="Short natural-language explanation for logs / clinicians (not user-facing crisis copy).")
    debug_notes: Optional[str] = Field(None, description="Optional extra reasoning notes for developers; not shown to end-users.")


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

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
     - Tier 3 (crisis): "warn" (default) unless content could trigger harm escalation, then "block"
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


# ---------------------------------------------------------------------------
# JSON schema for structured outputs
# ---------------------------------------------------------------------------

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
                            "normal_share_dialog", "gentle_check_in", "heated_warning_dialog",
                            "crisis_interstitial", "abuse_block_partner",
                        ],
                    },
                    "mark_as_urgent_for_therapist": {"type": "boolean"},
                    "notes_for_therapist": {"type": "string"},
                },
                "required": [
                    "partner_share_policy", "therapist_share_policy", "show_crisis_banner",
                    "show_crisis_resources", "suggested_ui_flow",
                    "mark_as_urgent_for_therapist", "notes_for_therapist",
                ],
            },
            "explanation": {"type": "string"},
            "debug_notes": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["risk_tier", "risk_label", "flags", "recommendations", "explanation", "debug_notes"],
    },
}


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

# CORS: add your Azure App Service URL below (e.g. https://ucanrr-api.azurewebsites.net)
_default_origins = [
    "https://ucanrr.com",
    "https://www.ucanrr.com",
    "https://ucanrr.ngrok-free.dev",
    "https://ucanrr-api.azurewebsites.net",   # <-- replace with your actual Azure URL
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

_allowed_origins_env = os.environ.get("ALLOWED_ORIGINS")
allowed_origins = (
    [o.strip() for o in _allowed_origins_env.split(",") if o.strip()]
    if _allowed_origins_env
    else _default_origins
)

app = FastAPI(
    title="UCANRR Safety Evaluation API",
    version="1.2.0",
    description="Evaluates UCANRR journal entries for crisis and abuse risk using the OpenAI API. Azure-hosted edition.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/safety/analyze_entry", response_model=SafetyAssessment)
async def analyze_entry(payload: JournalEntryRequest):
    if not payload.entry_text or not payload.entry_text.strip():
        raise HTTPException(status_code=400, detail="entry_text must not be empty.")

    if not os.environ.get("OPENAI_API_KEY"):
        logger.error("OPENAI_API_KEY is missing — request rejected.")
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY is not configured on the server.")

    try:
        logger.info("Safety eval requested. user_hash=%s", payload.user_hash or "anonymous")
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": payload.entry_text.strip()},
            ],
            response_format={"type": "json_schema", "json_schema": SAFETY_JSON_SCHEMA},
            safety_identifier=payload.user_hash if payload.user_hash else None,
            store=False,
            temperature=0,
        )

        raw_content = completion.choices[0].message.content
        assessment_dict = json.loads(raw_content)
        logger.info("Safety eval complete. risk_tier=%s risk_label=%s",
                    assessment_dict.get("risk_tier"), assessment_dict.get("risk_label"))

    except Exception as e:
        logger.exception("OpenAI call failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Error calling OpenAI safety model: {e}")

    return SafetyAssessment(**assessment_dict)


@app.get("/health")
async def health_check():
    """Azure App Service health probe endpoint."""
    api_key_set = bool(os.environ.get("OPENAI_API_KEY"))
    return {"status": "ok", "openai_key_configured": api_key_set, "model": OPENAI_MODEL}


# ---------------------------------------------------------------------------
# Local dev entry point — not used by Azure (gunicorn handles startup there)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 3000))
    uvicorn.run("azure_ucanrr_safety_eval_api:app", host="0.0.0.0", port=port, reload=True)
