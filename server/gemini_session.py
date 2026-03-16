"""Helpers for configuring and reading Gemini Live sessions."""

from __future__ import annotations

import base64
import json
from typing import Any

from google.genai import types

from server.modes import MODES
from server.personas import PERSONAS
from server.scoring import SCORE_FUNCTION

MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
SEND_SAMPLE_RATE = 16000
RECEIVE_SAMPLE_RATE = 24000


def build_system_prompt(mode_id: str, persona_id: str, material: str) -> str:
    mode = MODES[mode_id]
    persona = PERSONAS[persona_id]
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


def create_live_session(client, mode_id: str, persona_id: str, material: str):
    """Create a Gemini Live session for the selected mode/persona/material."""
    persona = PERSONAS[persona_id]
    system_prompt = build_system_prompt(mode_id, persona_id, material)

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


async def send_audio_chunk(session, pcm_bytes: bytes):
    """Forward 16 kHz PCM audio to Gemini Live."""
    await session.send_realtime_input(
        audio=types.Blob(data=pcm_bytes, mime_type=f"audio/pcm;rate={SEND_SAMPLE_RATE}")
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


def extract_transcripts(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return lists of user and agent transcript text snippets."""
    user_candidates = [
        _nested_get(payload, "server_content", "input_transcription"),
        _nested_get(payload, "serverContent", "inputTranscription"),
    ]
    agent_candidates = [
        _nested_get(payload, "server_content", "output_transcription"),
        _nested_get(payload, "serverContent", "outputTranscription"),
    ]

    def collect(candidates: list[Any]) -> list[str]:
        items: list[str] = []
        for candidate in candidates:
            if not candidate:
                continue
            text = candidate.get("text") if isinstance(candidate, dict) else None
            if text:
                items.append(text.strip())
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
