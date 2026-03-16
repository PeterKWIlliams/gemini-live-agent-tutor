import asyncio
import base64
import json
import os
import wave
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

async def main():
    client = genai.Client()
    
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )
    
    print("Connecting...")
    async with client.aio.live.connect(model="gemini-2.5-flash-native-audio-preview-12-2025", config=config) as session:
        print("Connected.")
        
        # Send a simple text message to trigger a response
        await session.send_client_content(
            turns=[{"role": "user", "parts": [{"text": "Hello! Say exactly: 'testing audio transcription'."}]}],
            turn_complete=True,
        )
        
        async for response in session.receive():
            if response.server_content:
                if response.server_content.model_turn:
                    pass # ignore for brevity
                if response.server_content.input_transcription:
                    print(f"GOT INPUT TRANSCRIPTION: {response.server_content.input_transcription}")
                if response.server_content.output_transcription:
                    print(f"GOT OUTPUT TRANSCRIPTION: {response.server_content.output_transcription}")
                if response.server_content.turn_complete:
                    print("Turn complete.")
                    break
        
        print("Sending audio...")
        # Create a silent audio or some noise
        audio_data = b'\x00' * 32000 # 1 second of silence
        
        await session.send_realtime_input(audio=types.Blob(data=audio_data, mime_type="audio/pcm;rate=16000"))
        await asyncio.sleep(1)
        await session.send_realtime_input(audio=types.Blob(data=audio_data, mime_type="audio/pcm;rate=16000"))
        
        # We need actual speech to trigger transcription?
        # Maybe text triggers it? No, input_transcription is for audio.

asyncio.run(main())
