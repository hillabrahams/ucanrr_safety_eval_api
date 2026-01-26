"""
UCANRR Safety Evaluation API
----------------------------

FastAPI service that uses the OpenAI API to evaluate journal entries for:
- Crisis risk (suicidal / self-harm / harm-to-others)
- Extreme abuse / threats
- Heated but non-crisis conflict

It returns:
- risk_tier 0–3
- risk_label
- flags (booleans)
- recommendations (how the UI should handle Share to therapist vs partner)
- a short explanation for logs / clinicians

Requires:
    pip install fastapi uvicorn "openai>=1.0.0" pydantic

Run:
    export OPENAI_API_KEY="***REMOVED_API_KEY***"
    uvicorn main:app --reload --DON'T USE
    uvicorn ucanrr_safety_eval_api:app --host 0.0.0.0 --port 3000 --reload
"""

import json
import os
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from openai import OpenAI

# ---------- OpenAI client ----------

# Make sure OPENAI_API_KEY is set in your environment - done
#client = OpenAI()
#client = OpenAI(api_key="***REMOVED_API_KEY***")

# Choose your model (you can swap to gpt-4.1, gpt-4o, etc.)
OPENAI_MODEL = "gpt-5.1"


# ---------- Pydantic models ----------

class JournalEntryRequest(BaseModel):
    entry_text: str = Field(..., description="Raw journal entry text from the UCANRR mobile app.")
    # Optional hashed user id for safety_identifier; DO NOT send PII here
    user_hash: Optional[str] = Field(
        None,
        description="Stable hash or pseudonymous user id for safety_identifier (no PII)."
    )


class SafetyAssessment(BaseModel):
    risk_tier: int = Field(..., ge=0, le=3, description="0=normal, 1=heated, 2=crisis, 3=extreme_abuse.")
    risk_label: str = Field(..., description="One of: normal, heated, crisis, extreme_abuse.")
    flags: Dict[str, Any] = Field(
        ...,
        description="Boolean flags and other fine-grained signals (e.g., has_suicidal_ideation, has_extreme_abuse)."
    )
    recommendations: Dict[str, Any] = Field(
        ...,
        description="Recommended UI & routing behavior for UCANRR (partner_share_policy, therapist_share_policy, etc.)."
    )
    explanation: str = Field(
        ...,
        description="Short natural-language explanation for logs / clinicians (not user-facing crisis copy)."
    )
    debug_notes: Optional[str] = Field(
        None,
        description="Optional extra reasoning notes for developers; not shown to end-users."
    )


# ---------- System prompt used for the OpenAI call ----------

SYSTEM_PROMPT = """
You are the UCANRR Safety Evaluator embedded in a couples-focused journaling system.

Your job is to READ ONE JOURNAL ENTRY and return a structured JSON safety assessment ONLY.
UCANRR uses your output to decide how to show “Share” options for the entry (to a therapist vs. to a partner),
and when to show crisis banners and resources.

You MUST follow these rules:

1. OUTPUT FORMAT
   - You MUST output a single JSON object that matches this schema exactly:
     {
       "risk_tier": 0 | 1 | 2 | 3,
       "risk_label": "normal" | "heated" | "crisis" | "extreme_abuse",
       "flags": {
         "has_suicidal_ideation": boolean,
         "has_self_harm": boolean,
         "has_other_harm": boolean,
         "has_extreme_abuse": boolean,
         "has_heated_argument": boolean,
         "has_crisis_language": boolean,
         "mentions_substance_use": boolean,
         "mentions_weapon_access": boolean,
         "mentions_child_safety_concern": boolean
       },
       "recommendations": {
         "partner_share_policy": "allow" | "warn" | "block",
         "therapist_share_policy": "allow" | "mark_urgent",
         "show_crisis_banner": boolean,
         "show_crisis_resources": boolean,
         "suggested_ui_flow": "normal_share_dialog"
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
     Examples:
       - Ordinary reflections
       - Mild frustration without insults or threats
       - Everyday disagreements that are not intense

   - Tier 1: "heated"
     Definition:
       - High emotional intensity, anger, blame, or harsh language
       - BUT no explicit self-harm, suicidality, or threats of harm
     Examples:
       - "I'm so angry I could scream"
       - "You never listen, I'm sick of this, you always ruin everything"
       - Swearing and name-calling without threats or violence

   - Tier 2: "crisis"
     Definition:
       - Any self-harm or suicidal ideation ("I want to die", "I don't want to be here")
       - Serious thoughts about harming others
       - Very strong crisis language suggesting acute risk (even if somewhat vague)
     Notes:
       - When in doubt between Tier 1 and Tier 2, PREFER Tier 2 (better safe than sorry).

   - Tier 3: "extreme_abuse"
     Definition:
       - Explicit, targeted emotional abuse, threats, or intimidation toward the partner
       - Threats of serious legal, financial, or physical harm
       - Highly hostile, demeaning, or terrorizing content directed at the other person
     Examples:
       - "I'm going to ruin you in court" said in a clearly vindictive way
       - "You are worthless and I want you to suffer"
       - Threats of violence toward partner or their loved ones

   IMPORTANT:
   - If BOTH crisis and extreme abuse appear, choose the HIGHER of 2 or 3.
     - If there is serious self-harm or suicidal content AND extreme abuse, use Tier 2 *or* Tier 3 based on which aspect is more central, but ensure crisis-related flags are set correctly.

3. FLAGS

   For each flag in the "flags" object:

   - has_suicidal_ideation:
       True if the writer expresses wanting to die, disappear, end their life, or clearly not wanting to live.
   - has_self_harm:
       True if the writer talks about hurting themselves (cutting, overdosing, etc.), even without explicit desire to die.
   - has_other_harm:
       True if the writer talks about harming someone else (partner, child, anyone).
   - has_extreme_abuse:
       True if the entry includes explicit, severe insults, demeaning language, or threats directed at another person.
   - has_heated_argument:
       True if the entry clearly describes a heated fight, shouting match, or intense conflict, even without abuse or threats.
   - has_crisis_language:
       True if the overall tone suggests a psychological or emotional crisis (e.g., "I can't take this anymore", "I'm at my breaking point").
   - mentions_substance_use:
       True if alcohol or drugs appear in a way that matters to safety (e.g., heavy drinking, blackout, rehab, using while in crisis).
   - mentions_weapon_access:
       True if weapons (guns, knives, etc.) are mentioned in a way related to risk (storage, access, threats).
   - mentions_child_safety_concern:
       True if there are concerns about children's physical or emotional safety.

4. RECOMMENDATIONS (how UCANRR should treat Share behavior)

   UCANRR distinguishes:
     - Sharing to THERAPIST / CLINICAL TEAM (protective)
     - Sharing to PARTNER (may help or escalate)

   Set these fields based on the risk tier:

   a) partner_share_policy:
      - Tier 0 (normal): "allow"
      - Tier 1 (heated): "warn"
      - Tier 2 (crisis):
          Usually "warn" (allow but with strong warning and friction).
          A clinic might choose to block, but you should default to "warn".
      - Tier 3 (extreme_abuse): "block" by default
          (Entry can still be saved and shared with therapist, but NOT sent directly to partner.)

   b) therapist_share_policy:
      - Tier 0 or 1: "allow"
      - Tier 2 or 3: "mark_urgent"
        (This means: highlight to the therapist as high priority. Note: UCANRR does NOT call emergency services.)

   c) show_crisis_banner:
      - True if risk_tier is 2 or 3 OR if any of has_suicidal_ideation, has_self_harm, or has_other_harm is True.
      - Otherwise False.

   d) show_crisis_resources:
      - True for Tier 2 (crisis) or whenever has_suicidal_ideation or has_self_harm is True.
      - Usually False for pure Tier 3 extreme abuse without self-harm, unless there's also crisis language.

   e) suggested_ui_flow:
      - Tier 0: "normal_share_dialog"
      - Tier 1: "heated_warning_dialog"
      - Tier 2: "crisis_interstitial"
      - Tier 3: "abuse_block_partner"

   f) mark_as_urgent_for_therapist:
      - True for Tier 2 or 3.
      - False for Tier 0 or 1.

   g) notes_for_therapist:
      - 1–3 sentence, concise summary in clinical language, e.g.:
          - "Entry describes intense marital conflict without suicidal or homicidal content."
          - "Entry includes suicidal ideation and feelings of hopelessness; consider assessing acute risk."
          - "Entry contains explicit emotional abuse and threats toward partner; partner sharing is blocked."
      - Do NOT instruct the therapist to call 911 or take specific legal actions.
      - Do NOT mention UCANRR implementation details, just summarize the clinical concern.

5. EXPLANATION AND DEBUG NOTES

   - "explanation":
       - Short, readable summary (1–3 sentences) of why you chose this tier and main flags.
       - This is for developers and possibly internal logs, not user-facing UI text.
   - "debug_notes":
       - Optional. Use for brief internal notes if needed, or set to null.

6. IMPORTANT SAFETY META-RULES

   - You are ONLY classifying text; you NEVER:
       - Call emergency services.
       - Send messages.
       - Make legal decisions.
   - When unsure between a lower tier and a higher tier, lean toward the HIGHER tier IF:
       - There is any credible mention of self-harm, suicidality, or harm to others.
   - If the entry is very short or ambiguous, choose the best tier you can, set flags conservatively,
     and explain your reasoning briefly in "explanation".

Remember: respond ONLY with the JSON object, NO extra text.
"""


# ---------- JSON schema for structured outputs ----------

SAFETY_JSON_SCHEMA: Dict[str, Any] = {
    "name": "JournalSafetyAssessment",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "risk_tier": {
                "type": "integer",
                "enum": [0, 1, 2, 3]
            },
            "risk_label": {
                "type": "string",
                "enum": ["normal", "heated", "crisis", "extreme_abuse"]
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
                },
                "required": [
                    "has_suicidal_ideation",
                    "has_self_harm",
                    "has_other_harm",
                    "has_extreme_abuse",
                    "has_heated_argument",
                    "has_crisis_language",
                    "mentions_substance_use",
                    "mentions_weapon_access",
                    "mentions_child_safety_concern",
                ],
            },
            "recommendations": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "partner_share_policy": {
                        "type": "string",
                        "enum": ["allow", "warn", "block"],
                    },
                    "therapist_share_policy": {
                        "type": "string",
                        "enum": ["allow", "mark_urgent"],
                    },
                    "show_crisis_banner": {"type": "boolean"},
                    "show_crisis_resources": {"type": "boolean"},
                    "suggested_ui_flow": {
                        "type": "string",
                        "enum": [
                            "normal_share_dialog",
                            "heated_warning_dialog",
                            "crisis_interstitial",
                            "abuse_block_partner",
                        ],
                    },
                    "mark_as_urgent_for_therapist": {"type": "boolean"},
                    "notes_for_therapist": {"type": "string"},
                },
                "required": [
                    "partner_share_policy",
                    "therapist_share_policy",
                    "show_crisis_banner",
                    "show_crisis_resources",
                    "suggested_ui_flow",
                    "mark_as_urgent_for_therapist",
                    "notes_for_therapist",
                ],
            },
            "explanation": {"type": "string"},
            "debug_notes": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "null"},
                ]
            },
        },
        "required": [
            "risk_tier",
            "risk_label",
            "flags",
            "recommendations",
            "explanation",
            "debug_notes",
        ],
    },
}

# ---------- FastAPI app ----------

app = FastAPI(
    title="UCANRR Safety Evaluation API",
    version="1.0.0",
    description="Evaluates UCANRR journal entries for crisis and abuse risk using the OpenAI API."
)

# Optional CORS setup if your Flutter app calls this directly
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],  # tighten in production
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )


# CORS Configuration - Add this RIGHT AFTER creating your FastAPI app
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://ucanrr.com",           # Production
        "http://localhost:3000",         # Local development
        "http://127.0.0.1:3000",        # Alternative localhost
    ],
    allow_credentials=True,
    allow_methods=["*"],  # Allows GET, POST, PUT, DELETE, etc.
    allow_headers=["*"],  # Allows all headers including Authorization
)



@app.post("/safety/analyze_entry", response_model=SafetyAssessment)
async def analyze_entry(payload: JournalEntryRequest):
    """
    Analyze a single journal entry and return the safety assessment.

    Typical usage in UCANRR:
      - Call this right after the user saves an entry.
      - Store the assessment with the entry.
      - Use risk_tier + recommendations to control the Share UI / interstitials.
    """
    if not payload.entry_text or not payload.entry_text.strip():
        raise HTTPException(status_code=400, detail="entry_text must not be empty.")

    try:
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": payload.entry_text.strip()
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": SAFETY_JSON_SCHEMA,
            },
            # Optional: pass a hashed user id to safety_identifier
            safety_identifier=payload.user_hash if payload.user_hash else None,
            temperature=0,  # deterministic classification
        )

        message = completion.choices[0].message
        raw_content = message.content

        # With json_schema structured outputs, content is a JSON string.
        assessment_dict = json.loads(raw_content)

    except Exception as e:
        # In production, log e and maybe include x-request-id from headers
        raise HTTPException(
            status_code=500,
            detail=f"Error calling OpenAI safety model: {e}"
        )

    # Let Pydantic validate against SafetyAssessment for extra safety
    return SafetyAssessment(**assessment_dict)


@app.get("/health")
async def health_check():
    return {"status": "ok"}

# ==================== RUN APPLICATION ====================

if __name__ == "__main__":
    import uvicorn
    #uvicorn.run(app, host="0.0.0.0", port=8000)
    uvicorn.run("ucanrr_safety_eval_api:app", host="0.0.0.0", port=3000, reload=True)