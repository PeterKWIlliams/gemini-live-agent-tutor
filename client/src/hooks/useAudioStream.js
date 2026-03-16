import { useCallback, useRef, useState } from 'react';
import { encodeAudioToPCM16, getInputSampleRate, resampleAudio } from '../lib/audio';

export default function useAudioStream() {
  const audioContextRef = useRef(null);
  const mediaStreamRef = useRef(null);
  const processorRef = useRef(null);
  const sourceRef = useRef(null);
  const sinkRef = useRef(null);

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

    setAudioLevel(0);
    setIsRecording(false);
  }, []);

  const startRecording = useCallback(
    async (sendChunk) => {
      try {
        setError('');
        const mediaStream = await navigator.mediaDevices.getUserMedia({
          audio: {
            channelCount: 1,
            echoCancellation: true,
            noiseSuppression: true,
          },
        });

        const audioContext = new window.AudioContext();
        const source = audioContext.createMediaStreamSource(mediaStream);
        const processor = audioContext.createScriptProcessor(4096, 1, 1);
        const sink = audioContext.createGain();
        sink.gain.value = 0;

        processor.onaudioprocess = (event) => {
          const inputData = event.inputBuffer.getChannelData(0);
          const resampled = resampleAudio(inputData, audioContext.sampleRate, getInputSampleRate());

          let peak = 0;
          for (let index = 0; index < resampled.length; index += 1) {
            peak = Math.max(peak, Math.abs(resampled[index]));
          }
          setAudioLevel(peak);

          sendChunk?.(encodeAudioToPCM16(resampled));
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
