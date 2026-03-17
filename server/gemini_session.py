"""Helpers for configuring and reading Gemini Live sessions."""

from __future__ import annotations

import base64
import json
from typing import Any

from google.genai import types

from server.modes import MODES
from server.personas import PERSONAS
from server.scoring import COMPLETE_CORRECTION_FUNCTION, SCORE_FUNCTION

MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
SEND_SAMPLE_RATE = 16000
RECEIVE_SAMPLE_RATE = 24000


def build_system_prompt(mode_id: str, persona_id: str, material: str, continuation_context: str = "") -> str:
    mode = MODES[mode_id]
    persona = PERSONAS[persona_id]
    if continuation_context.strip():
        material = f"{material}\n\nSession continuation context:\n{continuation_context.strip()}".strip()
    return mode["system_template"].format(
        material=material,
        persona_personality=persona["personality"],
        persona_behavior=persona["behavior"],
    )


def build_score_function_declaration():
    return types.FunctionDeclaration(
        name=SCORE_FUNCTION["name"],
        description=SCORE_FUNCTION["description"],
        parameters=SCORE_FUNCTION["parameters"],
    )


def build_complete_correction_function_declaration():
    return types.FunctionDeclaration(
        name=COMPLETE_CORRECTION_FUNCTION["name"],
        description=COMPLETE_CORRECTION_FUNCTION["description"],
        parameters=COMPLETE_CORRECTION_FUNCTION["parameters"],
    )


def create_live_session(client, mode_id: str, persona_id: str, material: str, continuation_context: str = ""):
    """Create a Gemini Live session for the selected mode/persona/material."""
    persona = PERSONAS[persona_id]
    system_prompt = build_system_prompt(mode_id, persona_id, material, continuation_context=continuation_context)

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=persona["voice"]
                )
            )
        ),
        system_instruction=system_prompt,
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        tools=[types.Tool(function_declarations=[build_score_function_declaration()])],
    )
    return client.aio.live.connect(model=MODEL, config=config)


def build_correction_system_prompt(material: str, issue_claim: str, issue_prompt: str, suggested_correction: str) -> str:
    return f"""
You are the Correction Agent for TeachBack. Your job is to pause the main lesson briefly, fix one misconception, and then hand the user back to the main tutor.

You are not the main tutor. Stay concise, corrective, and focused on one issue only.

Issue to correct:
{issue_claim}

Prompt to anchor the learner:
{issue_prompt}

Target correction:
{suggested_correction}

Reference material:
{material}

Behavior rules:
- Open with one short sentence explaining what needs correcting.
- Ask the learner to restate the corrected idea clearly in their own words.
- If they are still wrong or vague, ask one brief follow-up.
- Do not lecture for multiple paragraphs.
- Do not score the learner.
- When the learner has corrected the idea clearly enough, say one short closing sentence confirming the fix and handing them back to the main tutor, then call complete_correction with a compact resolved summary.
""".strip()


def create_correction_session(client, material: str, issue_claim: str, issue_prompt: str, suggested_correction: str):
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name="Charon"
                )
            )
        ),
        system_instruction=build_correction_system_prompt(
            material,
            issue_claim,
            issue_prompt,
            suggested_correction,
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        tools=[types.Tool(function_declarations=[build_complete_correction_function_declaration()])],
    )
    return client.aio.live.connect(model=MODEL, config=config)


async def send_audio_chunk(session, pcm_bytes: bytes, sample_rate: int = SEND_SAMPLE_RATE):
    """Forward PCM audio to Gemini Live at the given sample rate.

    Uses ``media=`` which serialises as ``mediaChunks`` on the wire –
    the format the Live API actually processes for realtime audio.

    Re-chunks to SUB_CHUNK_SIZE to match the standalone test's proven
    working chunk size (3200 bytes ~ 33ms at 48 kHz).
    """
    SUB_CHUNK_SIZE = 3200
    mime = f"audio/pcm;rate={sample_rate}"
    for i in range(0, len(pcm_bytes), SUB_CHUNK_SIZE):
        await session.send_realtime_input(
            media=types.Blob(data=pcm_bytes[i : i + SUB_CHUNK_SIZE], mime_type=mime)
        )


async def send_text_instruction(session, text: str, turn_complete: bool = True):
    """Send a text turn to the live session."""
    await session.send_client_content(
        turns=[{"role": "user", "parts": [{"text": text}]}],
        turn_complete=turn_complete,
    )


async def send_tool_response(session, responses: list[dict[str, Any]]):
    """A thin wrapper so callers do not need SDK details."""
    function_responses = [
        types.FunctionResponse(
            id=item["id"],
            name=item["name"],
            response=item["response"],
        )
        for item in responses
    ]
    await session.send_tool_response(function_responses=function_responses)


def response_to_dict(response: Any) -> dict[str, Any]:
    """Best-effort normalization for SDK response objects."""
    if hasattr(response, "model_dump"):
        return response.model_dump(exclude_none=True)
    if isinstance(response, dict):
        return response
    if hasattr(response, "__dict__"):
        return {
            key: value
            for key, value in response.__dict__.items()
            if not key.startswith("_") and value is not None
        }
    return {}


def _nested_get(data: Any, *keys: str):
    current = data
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
    return current


def extract_audio_messages(payload: dict[str, Any]) -> list[str]:
    """Pull audio bytes out of live responses and return base64 PCM."""
    candidates = [
        _nested_get(payload, "server_content", "model_turn", "parts"),
        _nested_get(payload, "serverContent", "modelTurn", "parts"),
    ]
    messages: list[str] = []
    for parts in candidates:
        if not parts:
            continue
        for part in parts:
            inline_data = part.get("inline_data") or part.get("inlineData")
            if not inline_data:
                continue
            data = inline_data.get("data")
            if isinstance(data, bytes):
                messages.append(base64.b64encode(data).decode("ascii"))
            elif isinstance(data, str):
                messages.append(data)
    return messages


def extract_transcripts(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return user and agent transcription events with completion state."""
    user_candidates = [
        _nested_get(payload, "server_content", "input_transcription"),
        _nested_get(payload, "serverContent", "inputTranscription"),
    ]
    agent_candidates = [
        _nested_get(payload, "server_content", "output_transcription"),
        _nested_get(payload, "serverContent", "outputTranscription"),
    ]

    def collect(candidates: list[Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for candidate in candidates:
            if not candidate:
                continue
            text = candidate.get("text") if isinstance(candidate, dict) else None
            if text:
                items.append(
                    {
                        "text": text.strip(),
                        "finished": bool(candidate.get("finished")),
                    }
                )
        return items

    return collect(user_candidates), collect(agent_candidates)


def extract_tool_calls(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return score function calls from a live response payload."""
    candidates = [
        _nested_get(payload, "tool_call", "function_calls"),
        _nested_get(payload, "toolCall", "functionCalls"),
    ]
    results: list[dict[str, Any]] = []
    for calls in candidates:
        if not calls:
            continue
        for call in calls:
            args = call.get("args")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            results.append(
                {
                    "id": call.get("id"),
                    "name": call.get("name"),
                    "args": args or {},
                }
            )
    return results


def extract_from_response(response: Any) -> dict[str, Any]:
    """Read structured fields directly from the SDK response object.

    Uses getattr to access SDK properties without going through
    model_dump / dict serialization, which can silently drop fields
    like input_transcription.
    """
    result: dict[str, Any] = {
        "audio_chunks": [],
        "user_transcripts": [],
        "agent_transcripts": [],
        "tool_calls": [],
        "turn_complete": False,
        "interrupted": False,
    }

    sc = getattr(response, "server_content", None)
    if sc is not None:
        result["turn_complete"] = bool(getattr(sc, "turn_complete", False))
        result["interrupted"] = bool(getattr(sc, "interrupted", False))

        # Input transcription (user speech -> text)
        it = getattr(sc, "input_transcription", None)
        if it is not None:
            text = getattr(it, "text", None) or ""
            if text.strip():
                result["user_transcripts"].append({
                    "text": text,
                    "finished": bool(getattr(it, "finished", True)),
                })

        # Output transcription (agent speech -> text)
        ot = getattr(sc, "output_transcription", None)
        if ot is not None:
            text = getattr(ot, "text", None) or ""
            if text.strip():
                result["agent_transcripts"].append({
                    "text": text,
                    "finished": bool(getattr(ot, "finished", True)),
                })

        # Audio from model turn
        mt = getattr(sc, "model_turn", None)
        if mt is not None:
            for part in getattr(mt, "parts", None) or []:
                inline = getattr(part, "inline_data", None)
                if inline is None:
                    continue
                data = getattr(inline, "data", None)
                if isinstance(data, bytes):
                    result["audio_chunks"].append(
                        base64.b64encode(data).decode("ascii")
                    )
                elif isinstance(data, str):
                    result["audio_chunks"].append(data)

    # Tool calls
    tc = getattr(response, "tool_call", None)
    if tc is not None:
        for call in getattr(tc, "function_calls", None) or []:
            args = getattr(call, "args", None)
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            result["tool_calls"].append({
                "id": getattr(call, "id", None),
                "name": getattr(call, "name", None),
                "args": args or {},
            })

    return result
