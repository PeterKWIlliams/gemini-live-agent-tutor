import { useEffect, useMemo, useRef, useState } from 'react';
import AudioVisualizer from '../components/AudioVisualizer';
import Transcript from '../components/Transcript';
import useAudioStream from '../hooks/useAudioStream';
import useWebSocket from '../hooks/useWebSocket';
import { decodePCM16ToAudio, getOutputSampleRate } from '../lib/audio';
import { getWsBaseUrl } from '../lib/api';

export default function Session({ session, mode, persona, onComplete, onAbort }) {
  const [messages, setMessages] = useState([]);
  const [status, setStatus] = useState('Connecting...');
  const [scores, setScores] = useState(null);
  const [error, setError] = useState('');

  const playbackContextRef = useRef(null);
  const nextPlaybackTimeRef = useRef(0);
  const speakingTimeoutRef = useRef(null);
  const hasStartedRef = useRef(false);
  const scoresRef = useRef(null);

  const { isRecording, startRecording, stopRecording, audioLevel, error: audioError } = useAudioStream();

  const handleIncomingMessage = async (message) => {
    if (message.type === 'ready') {
      setStatus('Listening...');
      await startRecording((chunk) => send({ type: 'audio', data: chunk }));
      return;
    }

    if (message.type === 'audio') {
      await playIncomingAudio(message.data);
      return;
    }

    if (message.type === 'transcript_user') {
      pushTranscript('user', message.text);
      return;
    }

    if (message.type === 'transcript_agent') {
      pushTranscript('agent', message.text);
      return;
    }

    if (message.type === 'scores') {
      scoresRef.current = message.data;
      setScores(message.data);
      return;
    }

    if (message.type === 'session_end') {
      await stopRecording();
      disconnect();
      onComplete(message.data || scoresRef.current || {});
      return;
    }

    if (message.type === 'error') {
      setError(message.message || 'The live session disconnected unexpectedly.');
    }
  };

  const { send, connect, disconnect, readyState } = useWebSocket({
    onMessage: handleIncomingMessage,
  });

  const connectionLabel = useMemo(() => {
    if (error || audioError) {
      return 'Needs attention';
    }
    if (status === 'Speaking...') {
      return 'Speaking...';
    }
    if (isRecording) {
      return 'Listening...';
    }
    if (readyState === WebSocket.OPEN) {
      return status;
    }
    return 'Connecting...';
  }, [audioError, error, isRecording, readyState, status]);

  useEffect(() => {
    connect(`${getWsBaseUrl()}/ws/${session.session_id}`);

    return () => {
      window.clearTimeout(speakingTimeoutRef.current);
      stopRecording();
      disconnect();
      playbackContextRef.current?.close();
    };
  }, [connect, disconnect, session.session_id, stopRecording]);

  useEffect(() => {
    if (readyState !== WebSocket.OPEN || hasStartedRef.current) {
      return;
    }

    hasStartedRef.current = true;
    send({
      type: 'start',
      session_id: session.session_id,
      mode_id: mode.id,
      persona_id: persona.id,
    });
  }, [mode.id, persona.id, readyState, send, session.session_id]);

  useEffect(() => {
    if (audioError) {
      setError(audioError);
    }
  }, [audioError]);

  const pushTranscript = (speaker, text) => {
    setMessages((current) => {
      if (!text) {
        return current;
      }

      const last = current[current.length - 1];
      if (last && last.speaker === speaker && last.text === text) {
        return current;
      }

      return [
        ...current,
        {
          id: `${speaker}-${current.length}-${Date.now()}`,
          speaker,
          text,
        },
      ];
    });
  };

  const playIncomingAudio = async (base64Chunk) => {
    if (!playbackContextRef.current) {
      playbackContextRef.current = new window.AudioContext({
        sampleRate: getOutputSampleRate(),
      });
    }

    const context = playbackContextRef.current;
    await context.resume();

    const samples = decodePCM16ToAudio(base64Chunk);
    const buffer = context.createBuffer(1, samples.length, getOutputSampleRate());
    buffer.copyToChannel(samples, 0);

    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);

    const startAt = Math.max(context.currentTime + 0.03, nextPlaybackTimeRef.current);
    source.start(startAt);
    nextPlaybackTimeRef.current = startAt + buffer.duration;

    setStatus('Speaking...');
    window.clearTimeout(speakingTimeoutRef.current);
    speakingTimeoutRef.current = window.setTimeout(() => {
      setStatus(isRecording ? 'Listening...' : 'Ready');
    }, 450);
  };

  const endSession = async () => {
    await stopRecording();
    setStatus('Wrapping up...');
    send({ type: 'stop' });
  };

  return (
    <main className="mx-auto min-h-screen max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
      <div className="grid gap-6 xl:grid-cols-[1.1fr_0.9fr]">
        <section className="space-y-6">
          <div className="rounded-[2.5rem] border border-white/60 bg-white/75 p-6 shadow-glow backdrop-blur">
            <div className="flex flex-wrap items-center justify-between gap-4">
              <div>
                <p className="text-xs uppercase tracking-[0.32em] text-moss/70">Live Session</p>
                <h1 className="mt-3 font-display text-5xl text-ink">
                  {persona.emoji} {persona.name}
                </h1>
                <p className="mt-2 text-sm leading-6 text-ink/70">
                  {mode.name} • {mode.technique}
                </p>
              </div>
              <div className="rounded-full border border-ink/10 bg-sand/80 px-4 py-2 text-sm font-semibold text-ink">
                {connectionLabel}
              </div>
            </div>

            <div className="mt-6 flex flex-wrap items-center gap-4">
              <AudioVisualizer audioLevel={audioLevel} isRecording={isRecording} status={connectionLabel} />
              <button
                type="button"
                onClick={endSession}
                className="rounded-full bg-ink px-5 py-3 text-sm font-semibold text-sand transition hover:bg-moss"
              >
                End session
              </button>
              <button
                type="button"
                onClick={onAbort}
                className="rounded-full border border-ink/10 bg-white px-5 py-3 text-sm font-semibold text-ink transition hover:border-ink/30"
              >
                Back
              </button>
            </div>

            {error ? <p className="mt-4 text-sm text-[#9c3f20]">{error}</p> : null}
          </div>

          <Transcript messages={messages} />
        </section>

        <aside className="space-y-6">
          <div className="rounded-[2.5rem] border border-white/60 bg-ink p-6 text-sand shadow-glow">
            <p className="text-xs uppercase tracking-[0.3em] text-sand/60">Session notes</p>
            <h2 className="mt-3 font-display text-4xl">How to get the most out of this run</h2>
            <ul className="mt-5 space-y-3 text-sm leading-6 text-sand/75">
              <li>Explain your thinking step by step instead of rushing to the answer.</li>
              <li>If the persona pushes back, use that moment to clarify rather than defend.</li>
              <li>You can stop anytime with the button or by saying you are done out loud.</li>
            </ul>
          </div>

          <div className="rounded-[2.5rem] border border-white/60 bg-white/75 p-6 shadow-glow">
            <p className="text-xs uppercase tracking-[0.3em] text-moss/70">Prepared material</p>
            <p className="mt-3 text-sm leading-6 text-ink/70">{session.material_preview}</p>
          </div>

          {scores ? (
            <div className="rounded-[2.5rem] border border-white/60 bg-white/75 p-6 shadow-glow">
              <p className="text-xs uppercase tracking-[0.3em] text-moss/70">Scores received</p>
              <h2 className="mt-2 font-display text-4xl text-ink">{scores.overall_score}</h2>
              <p className="mt-3 text-sm leading-6 text-ink/70">
                The session summary is ready. Once the wrap-up finishes, we will take you to the results screen.
              </p>
            </div>
          ) : null}
        </aside>
      </div>
    </main>
  );
}
