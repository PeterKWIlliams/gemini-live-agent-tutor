import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react';
import AudioVisualizer from '../components/AudioVisualizer';
import Transcript from '../components/Transcript';
import useAudioStream from '../hooks/useAudioStream';
import useWebSocket from '../hooks/useWebSocket';
import { decodePCM16ToAudio, getOutputSampleRate } from '../lib/audio';
import { getApiBaseUrl, getWsBaseUrl } from '../lib/api';

const PdfViewer = lazy(() => import('../components/PdfViewer'));

export default function Session({ session, mode, persona, onComplete, onAbort }) {
  const [messages, setMessages] = useState([]);
  const [status, setStatus] = useState('Connecting...');
  const [scores, setScores] = useState(null);
  const [error, setError] = useState('');
  const [sessionRun, setSessionRun] = useState(0);
  const [isMaterialExpanded, setIsMaterialExpanded] = useState(false);
  const [isPdfViewerExpanded, setIsPdfViewerExpanded] = useState(false);
  const [selectedPdfName, setSelectedPdfName] = useState('');

  const playbackContextRef = useRef(null);
  const nextPlaybackTimeRef = useRef(0);
  const speakingTimeoutRef = useRef(null);
  const isAgentSpeakingRef = useRef(false);
  const hasStartedRef = useRef(false);
  const hasMicStartedRef = useRef(false);
  const scoresRef = useRef(null);

  const { isRecording, startRecording, stopRecording, audioLevel, error: audioError } = useAudioStream();

  useEffect(() => {
    hasMicStartedRef.current = isRecording;
  }, [isRecording]);

  const beginRecording = async () => {
    if (hasMicStartedRef.current || readyState !== WebSocket.OPEN) {
      return;
    }

    hasMicStartedRef.current = true;
    setStatus('Listening...');
    await startRecording(
      (chunk, sampleRate) => send({ type: 'audio', data: chunk, sampleRate }),
      {
        onSpeechStart: () => setStatus('Listening...'),
        onSpeechEnd: () => {
          setStatus('Ready for next thought');
        },
        allowSend: () => !isAgentSpeakingRef.current,
      },
    );
  };

  const handleIncomingMessage = async (message) => {
    if (message.type === 'ready') {
      setStatus('Ready to start');
      return;
    }

    if (message.type === 'audio') {
      await playIncomingAudio(message.data);
      return;
    }

    if (message.type === 'transcript_user') {
      pushTranscript('user', message.text, message.finished !== false);
      return;
    }

    if (message.type === 'transcript_agent') {
      pushTranscript('agent', message.text, message.finished !== false);
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
      setStatus('Needs attention');
      disconnect();
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

  const materialText = session.material_text || session.material_preview || '';
  const materialPreview = session.material_preview || materialText;
  const sourceDocuments = useMemo(
    () =>
      (session.source_documents || []).map((document) => ({
        ...document,
        resolvedViewUrl: document.view_url?.startsWith('http')
          ? document.view_url
          : `${getApiBaseUrl()}${document.view_url}`,
      })),
    [session.source_documents],
  );
  const selectedSourceDocument = useMemo(
    () =>
      sourceDocuments.find((document) => document.name === selectedPdfName) ||
      sourceDocuments[0] ||
      null,
    [selectedPdfName, sourceDocuments],
  );

  useEffect(() => {
    setSelectedPdfName(sourceDocuments[0]?.name || '');
  }, [sourceDocuments]);

  useEffect(() => {
    connect(`${getWsBaseUrl()}/ws/${session.session_id}`);

    return () => {
      window.clearTimeout(speakingTimeoutRef.current);
      stopRecording();
      disconnect();
      playbackContextRef.current?.close();
    };
  }, [connect, disconnect, session.session_id, sessionRun, stopRecording]);

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

  const pushTranscript = (speaker, text, finished = true) => {
    setMessages((current) => {
      if (!text) {
        return current;
      }

      const last = current[current.length - 1];
      if (last && last.speaker === speaker && last.text === text && last.finished === finished) {
        return current;
      }

      if (last && last.speaker === speaker && last.finished === false) {
        const next = [...current];
        next[next.length - 1] = {
          ...last,
          text,
          finished,
        };
        return next;
      }

      return [
        ...current,
        {
          id: `${speaker}-${current.length}-${Date.now()}`,
          speaker,
          text,
          finished,
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

    isAgentSpeakingRef.current = true;
    setStatus('Speaking...');
    window.clearTimeout(speakingTimeoutRef.current);
    speakingTimeoutRef.current = window.setTimeout(() => {
      isAgentSpeakingRef.current = false;
      setStatus(hasMicStartedRef.current ? 'Listening...' : 'Ready to start');
    }, 450);
  };

  const endSession = async () => {
    await stopRecording();
    setStatus('Wrapping up...');
    send({ type: 'stop' });
  };

  const restartSession = async () => {
    await stopRecording();
    disconnect();
    playbackContextRef.current?.close();
    playbackContextRef.current = null;
    nextPlaybackTimeRef.current = 0;
    isAgentSpeakingRef.current = false;
    hasStartedRef.current = false;
    hasMicStartedRef.current = false;
    scoresRef.current = null;
    setMessages([]);
    setScores(null);
    setError('');
    setStatus('Connecting...');
    setIsMaterialExpanded(false);
    setIsPdfViewerExpanded(false);
    setSelectedPdfName(sourceDocuments[0]?.name || '');
    setSessionRun((current) => current + 1);
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
                onClick={beginRecording}
                disabled={readyState !== WebSocket.OPEN || isRecording}
                className="rounded-full bg-orange px-5 py-3 text-sm font-semibold text-white transition hover:bg-[#e3763b] disabled:cursor-not-allowed disabled:opacity-60"
              >
                {isRecording ? 'Listening now' : 'Start talking'}
              </button>
              <button
                type="button"
                onClick={endSession}
                className="rounded-full bg-ink px-5 py-3 text-sm font-semibold text-sand transition hover:bg-moss"
              >
                End session
              </button>
              <button
                type="button"
                onClick={restartSession}
                className="rounded-full border border-ink/10 bg-white px-5 py-3 text-sm font-semibold text-ink transition hover:border-ink/30"
              >
                Restart
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
              <li>Click `Start talking` when you are ready to begin this run.</li>
              <li>Use `Restart` to wipe this attempt and reopen a fresh live session with the same setup.</li>
              <li>Use `End session` only when you want scoring and a wrap-up.</li>
            </ul>
          </div>

          <div className="rounded-[2.5rem] border border-white/60 bg-white/75 p-6 shadow-glow">
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="text-xs uppercase tracking-[0.3em] text-moss/70">Prepared material</p>
                <p className="mt-2 text-sm leading-6 text-ink/60">
                  Judges can expand this to read the exact source text the session is grounded on.
                </p>
              </div>
              <button
                type="button"
                onClick={() => setIsMaterialExpanded((current) => !current)}
                className="rounded-full border border-ink/10 bg-sand px-4 py-2 text-sm font-semibold text-ink transition hover:border-orange hover:text-orange"
              >
                {isMaterialExpanded ? 'Collapse' : 'Expand'}
              </button>
            </div>
            <div className="mt-4 rounded-[1.5rem] bg-paper p-4">
              <p className="text-sm leading-6 text-ink/70 whitespace-pre-wrap">
                {isMaterialExpanded ? materialText : materialPreview}
              </p>
            </div>
          </div>

          {sourceDocuments.length ? (
            <div className="rounded-[2.5rem] border border-white/60 bg-white/75 p-6 shadow-glow">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <p className="text-xs uppercase tracking-[0.3em] text-moss/70">Original PDFs</p>
                  <p className="mt-2 text-sm leading-6 text-ink/60">
                    Judges can expand this to inspect the original preset documents directly in the booth.
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => setIsPdfViewerExpanded((current) => !current)}
                  className="rounded-full border border-ink/10 bg-sand px-4 py-2 text-sm font-semibold text-ink transition hover:border-orange hover:text-orange"
                >
                  {isPdfViewerExpanded ? 'Collapse' : 'Expand'}
                </button>
              </div>

              {isPdfViewerExpanded ? (
                <div className="mt-4 space-y-4">
                  {sourceDocuments.length > 1 ? (
                    <label className="block space-y-2">
                      <span className="text-xs font-semibold uppercase tracking-[0.22em] text-moss/70">
                        Choose document
                      </span>
                      <select
                        value={selectedSourceDocument?.name || ''}
                        onChange={(event) => setSelectedPdfName(event.target.value)}
                        className="w-full rounded-[1rem] border border-ink/10 bg-white px-4 py-3 text-sm font-medium text-ink outline-none transition focus:border-orange"
                      >
                        {sourceDocuments.map((document) => (
                          <option key={document.name} value={document.name}>
                            {document.label}
                          </option>
                        ))}
                      </select>
                    </label>
                  ) : null}

                  {selectedSourceDocument ? (
                    <Suspense
                      fallback={
                        <div className="rounded-[1.5rem] border border-ink/10 bg-paper px-4 py-6 text-sm font-semibold text-ink/60">
                          Loading viewer…
                        </div>
                      }
                    >
                      <PdfViewer
                        key={selectedSourceDocument.name}
                        url={selectedSourceDocument.resolvedViewUrl}
                        label={selectedSourceDocument.label}
                      />
                    </Suspense>
                  ) : null}
                </div>
              ) : null}
            </div>
          ) : null}

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
