"""Standalone multi-turn Gemini Live audio test.

This isolates whether Gemini Live can handle a second audio turn at all
outside the browser app. It can run with either:

1. A minimal config close to the known-good standalone test
2. An app-like config close to TeachBack's real session config
"""

from __future__ import annotations

import argparse
import asyncio
import os
import wave
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

from server.gemini_session import MODEL, build_score_function_declaration, build_system_prompt

load_dotenv()

DEFAULT_AUDIO_GLOB = "/tmp/teachback_debug_*.wav"
CHUNK_SIZE = 3200
CHUNK_DELAY_SECONDS = 0.05
RESPONSE_TIMEOUT_SECONDS = 20


def read_wav(path: str) -> tuple[bytes, int]:
    with wave.open(path, "rb") as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        frames = wf.getnframes()
        duration = frames / sample_rate
        print(
            f"  WAV {path}: ch={channels} width={sample_width} "
            f"rate={sample_rate} frames={frames} dur={duration:.2f}s"
        )
        if channels != 1 or sample_width != 2:
            raise ValueError(f"Expected mono 16-bit PCM WAV, got ch={channels} width={sample_width}")
        return wf.readframes(frames), sample_rate


def find_latest_debug_wav() -> str:
    candidates = sorted(Path("/tmp").glob("teachback_debug_*.wav"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise FileNotFoundError(f"No debug WAVs found matching {DEFAULT_AUDIO_GLOB}")
    return str(candidates[-1])


def build_minimal_config() -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )


def build_app_like_config() -> types.LiveConnectConfig:
    system_prompt = build_system_prompt(
        "explain",
        "curious_kid",
        "This is a debugging session. Listen to the user's spoken explanation and respond naturally.",
    )
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore")
            )
        ),
        system_instruction=system_prompt,
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        tools=[types.Tool(function_declarations=[build_score_function_declaration()])],
    )


async def send_audio_turn(session, audio_data: bytes, sample_rate: int, label: str):
    seconds = len(audio_data) / 2 / sample_rate
    print(f"  Sending {label}: {len(audio_data)} bytes ({seconds:.2f}s) at {sample_rate} Hz")
    for index in range(0, len(audio_data), CHUNK_SIZE):
        chunk = audio_data[index : index + CHUNK_SIZE]
        await session.send_realtime_input(
            media=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={sample_rate}")
        )
        await asyncio.sleep(CHUNK_DELAY_SECONDS)
    await session.send_realtime_input(audio_stream_end=True)
    print(f"  {label}: audio_stream_end sent")


async def wait_for_turn(session, label: str) -> dict[str, bool]:
    saw_input = False
    saw_output = False
    saw_audio = False
    saw_turn_complete = False

    print(f"  Waiting for {label} response...")
    try:
        async with asyncio.timeout(RESPONSE_TIMEOUT_SECONDS):
            async for response in session.receive():
                server_content = getattr(response, "server_content", None)
                if not server_content:
                    continue

                input_transcription = getattr(server_content, "input_transcription", None)
                if input_transcription and getattr(input_transcription, "text", None):
                    print(f"    INPUT: {input_transcription.text!r}")
                    saw_input = True

                output_transcription = getattr(server_content, "output_transcription", None)
                if output_transcription and getattr(output_transcription, "text", None):
                    print(f"    OUTPUT: {output_transcription.text!r}")
                    saw_output = True

                model_turn = getattr(server_content, "model_turn", None)
                if model_turn and getattr(model_turn, "parts", None):
                    saw_audio = True

                if getattr(server_content, "interrupted", False):
                    print("    INTERRUPTED")

                if getattr(server_content, "turn_complete", False):
                    print("    TURN COMPLETE")
                    saw_turn_complete = True
                    break
    except TimeoutError:
        print(f"    TIMEOUT waiting for {label} response")

    return {
        "input": saw_input,
        "output": saw_output,
        "audio": saw_audio,
        "turn_complete": saw_turn_complete,
    }


async def run_session(label: str, config: types.LiveConnectConfig, first_wav: str, second_wav: str):
    print(f"\n{'=' * 70}")
    print(f"{label}")
    print(f"{'=' * 70}")

    first_audio, first_rate = read_wav(first_wav)
    second_audio, second_rate = read_wav(second_wav)
    client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

    async with client.aio.live.connect(model=MODEL, config=config) as session:
        print("  Connected")

        await send_audio_turn(session, first_audio, first_rate, "turn 1")
        first_result = await wait_for_turn(session, "turn 1")
        print(f"  Turn 1 result: {first_result}")

        await asyncio.sleep(1)

        await send_audio_turn(session, second_audio, second_rate, "turn 2")
        second_result = await wait_for_turn(session, "turn 2")
        print(f"  Turn 2 result: {second_result}")


async def main():
    parser = argparse.ArgumentParser(description="Standalone multi-turn Gemini Live audio test")
    parser.add_argument("--first", help="Path to first WAV utterance")
    parser.add_argument("--second", help="Path to second WAV utterance")
    args = parser.parse_args()

    first_wav = args.first or find_latest_debug_wav()
    second_wav = args.second or first_wav

    print("Using audio files:")
    print(f"  first : {first_wav}")
    print(f"  second: {second_wav}")

    await run_session("Minimal config", build_minimal_config(), first_wav, second_wav)
    await run_session("App-like config", build_app_like_config(), first_wav, second_wav)


if __name__ == "__main__":
    asyncio.run(main())
