import asyncio
import os
import sys

from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai._live_converters import _LiveConnectConfig_to_mldev, _LiveConnectConfig_to_vertex

class MockApiClient:
    vertexai = False

config = types.LiveConnectConfig(
    response_modalities=["AUDIO"],
    input_audio_transcription=types.AudioTranscriptionConfig(),
    output_audio_transcription=types.AudioTranscriptionConfig(),
)

print("MLDev Config:")
print(_LiveConnectConfig_to_mldev(from_object=config, api_client=MockApiClient()))

print("\nVertex Config:")
class MockApiClientVertex:
    vertexai = True
print(_LiveConnectConfig_to_vertex(from_object=config, api_client=MockApiClientVertex()))
