# TeachBack - Gemini Live API Debug Summary

## The Bug (Updated)

~~Gemini never hears user speech~~ → ~~First user turn NOW WORKS. Subsequent turns after the agent responds do NOT.~~

**Latest diagnosis:** Gemini Live itself is fine, multi-turn audio is fine, and the captured browser audio is fine. The remaining bug is in the app's live orchestration path during real-time browser streaming.

In the app:
- the first user turn works
- Gemini responds with audio/transcript
- subsequent user turns in the same live session stall or are ignored

Outside the app:
- the same captured browser audio works for turn 1 **and** turn 2 in a standalone multi-turn test
- this is true for both a minimal config and an app-like config

## Architecture

```
Browser (mic) → WebSocket → FastAPI server → Gemini Live API (bidirectional streaming)
                                            ← audio/transcripts/tool_calls
```

- **Model**: `gemini-2.5-flash-native-audio-preview-12-2025`
- **SDK**: `google-genai==1.67.0`
- **Session**: `client.aio.live.connect(model=MODEL, config=config)`
- **Audio send method**: `session.send_realtime_input(media=types.Blob(...))`
- **Turn flush**: `session.send_realtime_input(audio_stream_end=True)`

## What Works

1. **First user turn**: User clicks "Start talking", speaks → Gemini transcribes and responds with audio
2. **Agent intro**: Server sends kickoff text instruction → Gemini speaks, audio plays in browser, `output_transcription` arrives
3. **Standalone test** (`test_live_audio.py`): Sending a WAV file directly to Gemini produces `input_transcription` AND model audio response — **with all three model variants**
4. **Browser-captured audio re-sent via standalone test**: Saved browser audio to `/tmp/teachback_debug_*.wav`, sent through standalone test at rate=48000 → **Gemini transcribes it perfectly**
5. **Standalone multi-turn test now works**: The same saved browser audio file works for turn 1 and turn 2 in the same Gemini Live session
6. **App-like config also works in standalone multi-turn test**: `system_instruction`, `speech_config`, and `tools` are not the root cause by themselves

## What Doesn't Work

**Real-time app multi-turn audio**: After Gemini responds to the first user turn, all subsequent user audio in the browser app may be ignored or stall. The server logs show audio chunks ARE being forwarded to Gemini, but the live app path does not reliably produce the next response.

## Fixed Issues (All Solved)

### Audio Format
- Browser `AudioContext({ sampleRate: 16000 })` is **silently ignored on macOS** — always runs at 48kHz
- Original code resampled 48k→16k with linear interpolation (no anti-aliasing) — corrupted speech
- **Fix**: Removed resampling, send at native 48kHz with `audio/pcm;rate=48000` MIME type

### SDK Wire Format
- `send_realtime_input(audio=...)` → `{"realtime_input": {"audio": {...}}}` wire format
- `send_realtime_input(media=...)` → `{"realtime_input": {"mediaChunks": [...]}}` wire format
- **`media=` is the correct one**

### Response Parsing
- `response.model_dump(exclude_none=True)` silently drops fields like `input_transcription`
- **Fix**: `extract_from_response()` reads SDK objects directly via `getattr`

### VAD Configuration
- Tried explicit `realtime_input_config` with `START_SENSITIVITY_LOW` / `END_SENSITIVITY_LOW` — didn't help
- Removed it to match standalone test (which uses defaults) — didn't change behavior

## Current Session Config

```python
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
```

## Current Client Flow

1. WebSocket connects → `start` message sent
2. Server sends kickoff text instruction (`send_client_content` with `turn_complete=True`) → agent intro plays
3. User clicks "Start talking" → mic starts (`getUserMedia` with `echoCancellation: true`)
4. Client-side speech gating: only sends audio when `peak >= 0.035` for 3+ consecutive frames
5. Audio chunks sent as base64 PCM over WebSocket → server decodes → `send_realtime_input(media=...)`
6. `audio_stream_end` is **NOT** sent on speech pause (was removed during debugging)
7. `audio_stream_end` is only sent on explicit "End session" click

## Current Audio Send Path

```python
# Server receives base64 PCM from browser WebSocket
pcm_bytes = base64.b64decode(message.get("data", ""))
sample_rate = int(message.get("sampleRate"))  # 48000 from browser

# Forward to Gemini
await session.send_realtime_input(
    media=types.Blob(data=pcm_bytes, mime_type=f"audio/pcm;rate={sample_rate}")
)
```

## The Multi-Turn Mystery

The **first turn works** but subsequent turns after the agent responds do NOT. The server logs confirm audio chunks are being forwarded for subsequent turns.

### Key Differences: Standalone vs App

| | Standalone Multi-Turn Test | Browser App |
|---|---|---|
| Audio source | Saved browser WAV | Live browser mic stream |
| Chunking | Fixed 3200-byte chunks with 50ms pacing | Live `ScriptProcessorNode` cadence |
| Turn end | Explicit `audio_stream_end=True` after each utterance | Has varied during debugging |
| Receive pattern | Send turn, then wait for response | Full duplex while browser/session/UI stay live |
| Result | Turn 1 + Turn 2 both work | First turn may work, later turns may fail |

### Top Hypotheses

**1. Live chunking / pacing mismatch**
- The standalone test works with fixed 3200-byte chunks and 50ms pacing
- The browser app uses live `ScriptProcessorNode`-driven chunk timing
- The remaining bug may be caused by chunk cadence or burstiness rather than audio content
- **Test**: Make the app's live send path mimic the standalone test more closely

**2. Real-time duplex session state issue**
- The standalone test sends a turn, then waits for a response
- The app keeps the full duplex session/UI/audio pipeline active the whole time
- Something about the app's real-time orchestration after the first round-trip may be leaving the session in a bad state
- **Test**: simplify the live app flow around turn completion and response handling

**3. Browser playback / mic interaction still contaminates later turns**
- Even though the audio file itself is fine, live playback + live mic capture may still be affecting later turns in a way the saved WAV cannot reproduce
- **Test**: continue suppressing mic send during agent playback and compare logs with/without that guard

**4. Turn completion signaling in the app is still not matching the standalone test**
- The standalone test explicitly sends `audio_stream_end=True` after every utterance and then waits
- The browser app has had several different turn-end behaviors during debugging
- **Test**: match standalone turn boundaries exactly in the live app

## Suggested Next Steps (Priority Order)

1. **Make the browser app mimic the standalone test more closely**
   - fixed 3200-byte chunking
   - ~50ms pacing
   - explicit `audio_stream_end` after each utterance
   - avoid extra live-session complexity during the turn

2. **Keep the mic-send suppression during agent playback**
   - this remains a sensible guard even though it did not fully solve the issue yet

3. **Inspect real-time app logs around turn 2 only**
   - `input_transcription`
   - `turn_complete`
   - any interruption/state flags

4. **If needed, simplify the app flow further**
   - remove kickoff
   - reduce duplex complexity
   - keep the live session as close as possible to the standalone working path

## Files

- `server/gemini_session.py` — Session config, audio send, response parsing
- `server/main.py` — WebSocket handler, audio routing, debug logging
- `client/src/hooks/useAudioStream.js` — Browser mic capture, speech gating
- `client/src/pages/Session.jsx` — Session UI, WebSocket messaging, "Start talking" button
- `client/src/lib/audio.js` — PCM encode/decode helpers
- `test_live_audio.py` — Standalone multi-turn test that now proves turn 1 + turn 2 both work outside the app
