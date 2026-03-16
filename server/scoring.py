"""Scoring schema and fallback prompts."""

SCORE_FUNCTION = {
    "name": "score_session",
    "description": (
        "Called when the learning session is complete. Evaluate the user's performance "
        "against the session mode and reference material."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "accuracy_score": {
                "type": "integer",
                "description": "0-100. How factually correct was the user?",
            },
            "completeness_score": {
                "type": "integer",
                "description": "0-100. How much of the important material was covered?",
            },
            "clarity_score": {
                "type": "integer",
                "description": "0-100. How clear, structured, and understandable was the user?",
            },
            "depth_score": {
                "type": "integer",
                "description": "0-100. How much mechanism, reasoning, and connection-making did the user show?",
            },
            "overall_score": {
                "type": "integer",
                "description": "0-100. Overall session performance.",
            },
            "strengths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "2-4 specific things the user did well.",
            },
            "gaps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "2-4 concepts that were missed or remain shaky.",
            },
            "misconceptions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Factual errors the user stated. Empty if none.",
            },
            "next_steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "2-3 next study steps.",
            },
        },
        "required": [
            "accuracy_score",
            "completeness_score",
            "clarity_score",
            "depth_score",
            "overall_score",
            "strengths",
            "gaps",
            "misconceptions",
            "next_steps",
        ],
    },
}

SCORING_FALLBACK_PROMPT = """
You are scoring a TeachBack learning session. Return ONLY valid JSON matching the required schema.

Mode:
{mode_name}

Persona:
{persona_name}

Reference material:
{material}

Transcript:
{transcript}

Evaluate accuracy, completeness, clarity, depth, overall performance, strengths, gaps, misconceptions, and next steps.
Keep strengths/gaps concise and specific. If there are no misconceptions, return an empty array.
""".strip()

WRAP_UP_PROMPT = """
The session is ending. Give a short spoken wrap-up in 2-3 encouraging sentences based on these scores:
{scores_json}

Mention one strength and one next step. Sound natural and supportive.
""".strip()
