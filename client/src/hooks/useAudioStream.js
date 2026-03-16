import { useCallback, useRef, useState } from 'react';
import { encodeAudioToPCM16 } from '../lib/audio';

const SPEECH_THRESHOLD = 0.035;
const SPEECH_START_FRAMES = 3;
const SILENCE_HOLD_MS = 700;

export default function useAudioStream() {
  const audioContextRef = useRef(null);
  const mediaStreamRef = useRef(null);
  const processorRef = useRef(null);
  const sourceRef = useRef(null);
  const sinkRef = useRef(null);
  const speechActiveRef = useRef(false);
  const speechFrameCountRef = useRef(0);
  const lastSpeechAtRef = useRef(0);

  const [isRecording, setIsRecording] = useState(false);
  const [audioLevel, setAudioLevel] = useState(0);
  const [error, setError] = useState('');

  const stopRecording = useCallback(async () => {
    processorRef.current?.disconnect();
    sourceRef.current?.disconnect();
    sinkRef.current?.disconnect();
    mediaStreamRef.current?.getTracks().forEach((track) => track.stop());

    if (audioContextRef.current) {
      await audioContextRef.current.close();
    }

    processorRef.current = null;
    sourceRef.current = null;
    sinkRef.current = null;
    mediaStreamRef.current = null;
    audioContextRef.current = null;
    speechActiveRef.current = false;
    speechFrameCountRef.current = 0;
    lastSpeechAtRef.current = 0;

    setAudioLevel(0);
    setIsRecording(false);
  }, []);

  const startRecording = useCallback(
    async (sendChunk, { onSpeechStart, onSpeechEnd, allowSend } = {}) => {
      try {
        setError('');
        const mediaStream = await navigator.mediaDevices.getUserMedia({
          audio: {
            channelCount: 1,
            echoCancellation: true,
            noiseSuppression: true,
          },
        });

        // Use the system's native sample rate. Gemini accepts any rate and
        // resamples internally, so we avoid doing our own (lossy) resample.
        const audioContext = new window.AudioContext();
        await audioContext.resume();
        console.log('[useAudioStream] AudioContext sampleRate:', audioContext.sampleRate);
        const source = audioContext.createMediaStreamSource(mediaStream);
        const processor = audioContext.createScriptProcessor(4096, 1, 1);
        const sink = audioContext.createGain();
        sink.gain.value = 0;

        processor.onaudioprocess = (event) => {
          const inputData = event.inputBuffer.getChannelData(0);

          let peak = 0;
          for (let index = 0; index < inputData.length; index += 1) {
            peak = Math.max(peak, Math.abs(inputData[index]));
          }
          setAudioLevel(peak);
          const now = performance.now();
          const canSend = allowSend ? allowSend() : true;

          if (!canSend) {
            speechActiveRef.current = false;
            speechFrameCountRef.current = 0;
            lastSpeechAtRef.current = 0;
            return;
          }

          // Speech-gated sending: only forward audio when speech is detected.
          // On silence after speech, fire onSpeechEnd so the caller can send
          // audio_stream_end to flush Gemini's buffer and finalize the turn.
          if (peak >= SPEECH_THRESHOLD) {
            lastSpeechAtRef.current = now;
            speechFrameCountRef.current += 1;

            if (!speechActiveRef.current && speechFrameCountRef.current >= SPEECH_START_FRAMES) {
              speechActiveRef.current = true;
              onSpeechStart?.();
            }
          } else {
            speechFrameCountRef.current = 0;

            if (
              speechActiveRef.current &&
              now - lastSpeechAtRef.current >= SILENCE_HOLD_MS
            ) {
              speechActiveRef.current = false;
              onSpeechEnd?.();
            }
          }

          // Send audio at the native AudioContext rate (no resampling).
          // Gemini accepts any rate and resamples internally with proper
          // anti-aliasing, so this avoids the quality loss from our own
          // linear interpolation resampler.
          if (speechActiveRef.current || peak >= SPEECH_THRESHOLD) {
            sendChunk?.(encodeAudioToPCM16(inputData), audioContext.sampleRate);
          }
        };

        source.connect(processor);
        processor.connect(sink);
        sink.connect(audioContext.destination);

        audioContextRef.current = audioContext;
        mediaStreamRef.current = mediaStream;
        processorRef.current = processor;
        sourceRef.current = source;
        sinkRef.current = sink;
        setIsRecording(true);
      } catch (err) {
        setError('Microphone access was denied or unavailable.');
        console.error(err);
        await stopRecording();
      }
    },
    [stopRecording],
  );

  return {
    isRecording,
    startRecording,
    stopRecording,
    audioLevel,
    error,
  };
}
