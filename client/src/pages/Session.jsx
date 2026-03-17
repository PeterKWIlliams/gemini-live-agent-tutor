import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react';
import AudioVisualizer from '../components/AudioVisualizer';
import Transcript from '../components/Transcript';
import useAudioStream from '../hooks/useAudioStream';
import useWebSocket from '../hooks/useWebSocket';
import { decodePCM16ToAudio, getOutputSampleRate } from '../lib/audio';
import { getApiBaseUrl, getWsBaseUrl } from '../lib/api';

const PdfViewer = lazy(() => import('../components/PdfViewer'));

function buildPanelPreview(text, limit = 220) {
  const compact = (text || '').replace(/\s+/g, ' ').trim();
  if (!compact) {
    return '';
  }
  return compact.length > limit ? `${compact.slice(0, limit)}...` : compact;
}

function buildIssueSignature(issue) {
  return `${issue?.claim || ''}|${issue?.suggestedCorrection || ''}`.trim().toLowerCase();
}

export default function Session({ session, mode, persona, onComplete, onAbort }) {
  const [messages, setMessages] = useState([]);
  const [status, setStatus] = useState('Connecting...');
  const [scores, setScores] = useState(null);
  const [error, setError] = useState('');
  const [sessionRun, setSessionRun] = useState(0);
  const [isLearningGoalsExpanded, setIsLearningGoalsExpanded] = useState(false);
  const [isMaterialExpanded, setIsMaterialExpanded] = useState(false);
  const [isPdfViewerExpanded, setIsPdfViewerExpanded] = useState(false);
  const [selectedPdfName, setSelectedPdfName] = useState('');
  const [activeInputChannel, setActiveInputChannel] = useState('main');
  const [isInterruptionActive, setIsInterruptionActive] = useState(false);
  const [correctionError, setCorrectionError] = useState('');
  const [correctionMessages, setCorrectionMessages] = useState([]);
  const [pendingIssues, setPendingIssues] = useState([]);
  const [issueCount, setIssueCount] = useState(0);
  const [activeCorrectionIssue, setActiveCorrectionIssue] = useState(null);
  const [isCorrectionConnecting, setIsCorrectionConnecting] = useState(false);
  const [isRelayInFlight, setIsRelayInFlight] = useState(false);
  const [autoInterruptEnabled, setAutoInterruptEnabled] = useState(false);

  const playbackContextRef = useRef(null);
  const nextPlaybackTimeRef = useRef(0);
  const speakingTimeoutRef = useRef(null);
  const isAgentSpeakingRef = useRef(false);
  const hasMicStartedRef = useRef(false);
  const scoresRef = useRef(null);
  const activeInputChannelRef = useRef('main');
  const interruptionActiveRef = useRef(false);
  const activePlaybackSourcesRef = useRef([]);
  const autoInterruptTimeoutRef = useRef(null);
  const lastIssueSignatureRef = useRef('');
  const lastIssueTimestampRef = useRef(0);

  const { isRecording, startRecording, stopRecording, audioLevel, error: audioError } = useAudioStream();

  useEffect(() => {
    hasMicStartedRef.current = isRecording;
  }, [isRecording]);

  useEffect(() => {
    interruptionActiveRef.current = isInterruptionActive;
  }, [isInterruptionActive]);

  useEffect(() => {
    activeInputChannelRef.current = activeInputChannel;
  }, [activeInputChannel]);

  const queueDetectedIssue = (issue) => {
    if (!issue || interruptionActiveRef.current || isRelayInFlight) {
      return;
    }

    const signature = buildIssueSignature(issue);
    const now = Date.now();
    if (
      signature &&
      signature === lastIssueSignatureRef.current &&
      now - lastIssueTimestampRef.current < 18000
    ) {
      return;
    }

    lastIssueSignatureRef.current = signature;
    lastIssueTimestampRef.current = now;
    setIssueCount((current) => current + 1);
    setPendingIssues((current) => {
      if (current.some((item) => buildIssueSignature(item) === signature)) {
        return current;
      }
      return [...current, issue];
    });
  };

  const beginRecording = async () => {
    if (hasMicStartedRef.current || readyState !== WebSocket.OPEN) {
      return;
    }
    if (interruptionActiveRef.current && activeInputChannelRef.current !== 'correction') {
      return;
    }

    hasMicStartedRef.current = true;
    setStatus(activeInputChannelRef.current === 'correction' ? 'Correction agent listening...' : 'Listening...');
    await startRecording(
      (chunk, sampleRate) =>
        send({
          type: 'audio',
          data: chunk,
          sampleRate,
          channel: activeInputChannelRef.current,
        }),
      {
        onSpeechStart: () =>
          setStatus(activeInputChannelRef.current === 'correction' ? 'Correction agent listening...' : 'Listening...'),
        onSpeechEnd: () => {
          send({ type: 'audio_stream_end', channel: activeInputChannelRef.current });
          setStatus(activeInputChannelRef.current === 'correction' ? 'Checking the correction...' : 'Ready for next thought');
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
      if (message.channel === 'correction') {
        pushCorrectionTranscript('user', message.text, message.finished !== false);
        return;
      }
      pushTranscript('user', message.text, message.finished !== false);
      return;
    }

    if (message.type === 'transcript_agent') {
      if (message.channel === 'correction') {
        pushCorrectionTranscript('agent', message.text, message.finished !== false);
        return;
      }
      pushTranscript('agent', message.text, message.finished !== false);
      return;
    }

    if (message.type === 'correction_signal') {
      console.info('TeachBack correction_signal received', message.data);
      queueDetectedIssue(message.data || null);
      return;
    }

    if (message.type === 'interruption_started') {
      setIsInterruptionActive(true);
      interruptionActiveRef.current = true;
      setIsCorrectionConnecting(true);
      setCorrectionError('');
      setStatus('Correction pause active');
      return;
    }

    if (message.type === 'correction_ready') {
      setActiveCorrectionIssue(message.data?.issue || activeCorrectionIssue);
      setIsCorrectionConnecting(false);
      setActiveInputChannel('correction');
      activeInputChannelRef.current = 'correction';
      setStatus('Correction agent listening...');
      void beginRecording();
      return;
    }

    if (message.type === 'correction_complete') {
      setIsRelayInFlight(true);
      setStatus('Handing back to the main tutor...');
      return;
    }

    if (message.type === 'interruption_resumed') {
      const resolvedSummary = message.data?.resolved_summary || '';
      const resumeReason = message.data?.reason || '';
      if (resolvedSummary) {
        pushTranscript('agent', `Correction resolved: ${resolvedSummary}`, true);
      }
      setIsCorrectionConnecting(false);
      setIsRelayInFlight(false);
      setActiveCorrectionIssue(null);
      setCorrectionMessages([]);
      setCorrectionError('');
      setIsInterruptionActive(false);
      interruptionActiveRef.current = false;
      setActiveInputChannel('main');
      activeInputChannelRef.current = 'main';
      setStatus(resumeReason === 'timeout' ? 'Correction timed out. Resuming lesson...' : 'Resuming lesson...');
      void beginRecording();
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
      const detail = message.message || 'The live session disconnected unexpectedly.';
      if (isInterruptionActive || isCorrectionConnecting) {
        setCorrectionError(detail);
      } else {
        setError(detail);
        setStatus('Needs attention');
        disconnect();
      }
    }
  };

  const { send, connect, disconnect, readyState } = useWebSocket({
    onMessage: handleIncomingMessage,
    onOpen: (socket) => {
      socket.send(
        JSON.stringify({
          type: 'start',
          session_id: session.session_id,
          mode_id: mode.id,
          persona_id: persona.id,
        }),
      );
    },
  });

  const connectionLabel = useMemo(() => {
    if (error || audioError) {
      return 'Needs attention';
    }
    if (status === 'Speaking...') {
      return 'Speaking...';
    }
    if (isRecording) {
      return activeInputChannel === 'correction' ? 'Correction listening...' : 'Listening...';
    }
    if (readyState === WebSocket.OPEN) {
      return status;
    }
    return 'Connecting...';
  }, [activeInputChannel, audioError, error, isRecording, readyState, status]);

  const materialText = session.material_text || session.material_preview || '';
  const materialPreview = session.material_preview || materialText;
  const learningGoalsText = session.learning_goals || '';
  const learningGoalsPreview = useMemo(
    () => buildPanelPreview(learningGoalsText, 240),
    [learningGoalsText],
  );
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
    return () => {
      window.clearTimeout(autoInterruptTimeoutRef.current);
    };
  }, []);

  useEffect(() => {
    connect(`${getWsBaseUrl()}/ws/${session.session_id}`);

    return () => {
      window.clearTimeout(speakingTimeoutRef.current);
      window.clearTimeout(autoInterruptTimeoutRef.current);
      stopRecording();
      disconnect();
      playbackContextRef.current?.close();
    };
  }, [connect, disconnect, session.session_id, sessionRun, stopRecording]);

  useEffect(() => {
    if (audioError) {
      setError(audioError);
    }
  }, [audioError]);

  const pushMessageList = (setList, speaker, text, finished = true) => {
    setList((current) => {
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

  const pushTranscript = (speaker, text, finished = true) => {
    pushMessageList(setMessages, speaker, text, finished);
  };

  const pushCorrectionTranscript = (speaker, text, finished = true) => {
    pushMessageList(setCorrectionMessages, speaker, text, finished);
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
    activePlaybackSourcesRef.current.push(source);
    source.onended = () => {
      activePlaybackSourcesRef.current = activePlaybackSourcesRef.current.filter((item) => item !== source);
    };

    const startAt = Math.max(context.currentTime + 0.03, nextPlaybackTimeRef.current);
    source.start(startAt);
    nextPlaybackTimeRef.current = startAt + buffer.duration;

    isAgentSpeakingRef.current = true;
    setStatus(interruptionActiveRef.current ? 'Correction agent speaking...' : 'Speaking...');
    window.clearTimeout(speakingTimeoutRef.current);
    speakingTimeoutRef.current = window.setTimeout(() => {
      isAgentSpeakingRef.current = false;
      if (hasMicStartedRef.current) {
        setStatus(activeInputChannelRef.current === 'correction' ? 'Correction agent listening...' : 'Listening...');
      } else {
        setStatus(activeInputChannelRef.current === 'correction' ? 'Awaiting correction reply...' : 'Ready to start');
      }
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
    hasMicStartedRef.current = false;
    scoresRef.current = null;
    setMessages([]);
    setScores(null);
    setError('');
    setStatus('Connecting...');
    setIsLearningGoalsExpanded(false);
    setIsMaterialExpanded(false);
    setIsPdfViewerExpanded(false);
    setSelectedPdfName(sourceDocuments[0]?.name || '');
    setActiveInputChannel('main');
    activeInputChannelRef.current = 'main';
    setIsInterruptionActive(false);
    interruptionActiveRef.current = false;
    setIsCorrectionConnecting(false);
    setCorrectionError('');
    setCorrectionMessages([]);
    window.clearTimeout(autoInterruptTimeoutRef.current);
    setPendingIssues([]);
    setIssueCount(0);
    setActiveCorrectionIssue(null);
    setIsRelayInFlight(false);
    lastIssueSignatureRef.current = '';
    lastIssueTimestampRef.current = 0;
    setSessionRun((current) => current + 1);
  };

  const clearMainPlayback = async () => {
    window.clearTimeout(speakingTimeoutRef.current);
    activePlaybackSourcesRef.current.forEach((source) => {
      try {
        source.stop();
      } catch (error) {
        console.debug('Playback source already ended.', error);
      }
    });
    activePlaybackSourcesRef.current = [];
    if (playbackContextRef.current?.state === 'running') {
      await playbackContextRef.current.suspend();
    }
    isAgentSpeakingRef.current = false;
  };

  async function startCorrectionFlow(issueOverride = null) {
    window.clearTimeout(autoInterruptTimeoutRef.current);
    autoInterruptTimeoutRef.current = null;
    const issue = issueOverride || pendingIssues[0];
    if (!issue) {
      return;
    }
    const issueSignature = buildIssueSignature(issue);
    setPendingIssues((current) => {
      let removed = false;
      return current.filter((item) => {
        if (!removed && buildIssueSignature(item) === issueSignature) {
          removed = true;
          return false;
        }
        return true;
      });
    });
    setCorrectionError('');
    setCorrectionMessages([]);
    setIsRelayInFlight(false);
    setActiveCorrectionIssue(issue || null);
    setIsInterruptionActive(true);
    interruptionActiveRef.current = true;
    setIsCorrectionConnecting(true);
    console.info('TeachBack starting correction flow', issue);
    await stopRecording();
    await clearMainPlayback();
    hasMicStartedRef.current = false;
    send({
      type: 'start_correction',
      issue: {
        ...issue,
        issue_signature: issueSignature,
      },
    });
    setStatus('Starting correction agent...');
  }

  const forceResumeMain = async () => {
    await stopRecording();
    hasMicStartedRef.current = false;
    send({ type: 'cancel_correction' });
    setStatus('Forcing main lesson resume...');
  };

  useEffect(() => {
    if (
      !autoInterruptEnabled ||
      pendingIssues.length === 0 ||
      isInterruptionActive ||
      isRelayInFlight ||
      readyState !== WebSocket.OPEN
    ) {
      window.clearTimeout(autoInterruptTimeoutRef.current);
      autoInterruptTimeoutRef.current = null;
      return;
    }

    autoInterruptTimeoutRef.current = window.setTimeout(() => {
      autoInterruptTimeoutRef.current = null;
      void startCorrectionFlow(pendingIssues[0]);
    }, 450);

    return () => {
      window.clearTimeout(autoInterruptTimeoutRef.current);
      autoInterruptTimeoutRef.current = null;
    };
  }, [autoInterruptEnabled, pendingIssues, isInterruptionActive, isRelayInFlight, readyState]);

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
                disabled={readyState !== WebSocket.OPEN || isRecording || isInterruptionActive}
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
              <button
                type="button"
                onClick={() => setAutoInterruptEnabled((current) => !current)}
                className={`rounded-full border px-5 py-3 text-sm font-semibold transition ${
                  autoInterruptEnabled
                    ? 'border-[#d85d28] bg-[#fff0e7] text-[#9c3f20] hover:bg-[#ffe7d7]'
                    : 'border-ink/10 bg-white text-ink hover:border-ink/30'
                }`}
              >
                {autoInterruptEnabled ? 'Auto pause on' : 'Alert light only'}
              </button>
              <button
                type="button"
                onClick={() => void startCorrectionFlow(pendingIssues[0] || null)}
                disabled={isInterruptionActive || readyState !== WebSocket.OPEN || pendingIssues.length === 0}
                className={`inline-flex items-center gap-3 rounded-full border px-4 py-3 text-sm font-semibold transition disabled:cursor-not-allowed disabled:opacity-60 ${
                  pendingIssues.length
                    ? 'border-[#d85d28] bg-[#fff0e7] text-[#9c3f20] hover:bg-[#ffe7d7]'
                    : 'border-[#80b48c] bg-[#eff8f1] text-[#2f6d3b] hover:border-[#5e9a69]'
                }`}
              >
                <span className={`size-3 rounded-full ${pendingIssues.length ? 'bg-[#d85d28] animate-pulse' : 'bg-[#5e9a69]'}`} />
                <span>{pendingIssues.length ? 'Correction alert' : 'No active alerts'}</span>
                <span
                  className={`rounded-full px-2.5 py-1 text-xs font-bold ${
                    pendingIssues.length ? 'bg-white/80 text-[#9c3f20]' : 'bg-white/80 text-[#2f6d3b]'
                  }`}
                >
                  {issueCount}
                </span>
              </button>
              <button
                type="button"
                onClick={() =>
                  startCorrectionFlow(
                    pendingIssues[0] || {
                      claim: 'A clarification is needed before the lesson continues.',
                      cue: 'Manual correction fallback triggered.',
                      prompt:
                        'A correction agent is stepping in to resolve a clarification before the main tutor continues.',
                      suggestedCorrection:
                        'Provide the corrected idea clearly, then let the correction agent return control to the main lesson.',
                    },
                  )
                }
                disabled={isInterruptionActive || readyState !== WebSocket.OPEN}
                className={`rounded-full px-5 py-3 text-sm font-semibold transition disabled:cursor-not-allowed disabled:opacity-60 ${
                  pendingIssues.length
                    ? 'border border-[#d85d28] bg-[#fff0e7] text-[#9c3f20] shadow-[0_0_0_3px_rgba(216,93,40,0.1)] hover:bg-[#ffe7d7]'
                    : 'border border-orange/25 bg-[#fff2ea] text-[#9c3f20] hover:border-orange'
                }`}
              >
                Test interruption
              </button>
            </div>

            {error ? <p className="mt-4 text-sm text-[#9c3f20]">{error}</p> : null}
            {pendingIssues.length ? (
              <div className="mt-4 rounded-[1.25rem] border border-[#f0b28f] bg-[#fff2ea] px-4 py-3 text-sm leading-6 text-[#7a3a1f]">
                <p className="text-[0.68rem] font-semibold uppercase tracking-[0.24em] text-[#c66435]">
                  Correction alerts
                </p>
                <p className="mt-2">
                  {pendingIssues.length} correction {pendingIssues.length === 1 ? 'alert is' : 'alerts are'} waiting. Click the alert light to open the next one, or turn on auto pause.
                </p>
                <p className="mt-2 text-xs uppercase tracking-[0.2em] text-[#c66435]/80">
                  Next up
                </p>
                <p className="mt-1">{pendingIssues[0]?.cue}</p>
              </div>
            ) : null}
          </div>

          <Transcript messages={messages} />
        </section>

        <aside className="space-y-6">
          <div className="rounded-[2.5rem] border border-white/60 bg-ink p-6 text-sand shadow-glow">
            <p className="text-xs uppercase tracking-[0.3em] text-sand/60">Session notes</p>
            <h2 className="mt-3 font-display text-4xl">How to get the most out of this run</h2>
            <ul className="mt-5 space-y-3 text-sm leading-6 text-sand/75">
              <li>Click `Start talking` when you are ready to begin this run.</li>
              <li>Use `Learning goals` to show the session plan and must-cover checkpoints for this run.</li>
              <li>Use `Restart` to wipe this attempt and reopen a fresh live session with the same setup.</li>
              <li>Use `End session` only when you want scoring and a wrap-up.</li>
              <li>Use the interruption button when the session needs a clarification or correction pass.</li>
            </ul>
          </div>

          {learningGoalsText ? (
            <div className="rounded-[2.5rem] border border-white/60 bg-white/75 p-6 shadow-glow">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <p className="text-xs uppercase tracking-[0.3em] text-moss/70">Learning goals</p>
                  <p className="mt-2 text-sm leading-6 text-ink/60">
                    Judges can expand this to see the checkpoint-style goals that frame the session.
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => setIsLearningGoalsExpanded((current) => !current)}
                  className="rounded-full border border-ink/10 bg-sand px-4 py-2 text-sm font-semibold text-ink transition hover:border-orange hover:text-orange"
                >
                  {isLearningGoalsExpanded ? 'Collapse' : 'Expand'}
                </button>
              </div>
              <div className="mt-4 rounded-[1.5rem] bg-paper p-4">
                <p className="whitespace-pre-wrap text-sm leading-6 text-ink/70">
                  {isLearningGoalsExpanded ? learningGoalsText : learningGoalsPreview}
                </p>
              </div>
            </div>
          ) : null}

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

      {isInterruptionActive ? (
        <div className="fixed inset-0 z-40 flex items-end justify-end bg-ink/20 p-4 sm:p-6">
          <div className="w-full max-w-md rounded-[2rem] border border-white/70 bg-white/95 p-5 shadow-[0_28px_80px_rgba(34,38,35,0.24)] backdrop-blur">
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="text-xs uppercase tracking-[0.28em] text-moss/70">Correction agent</p>
                <h2 className="mt-2 font-display text-3xl text-ink">Main tutor paused</h2>
                <p className="mt-2 text-sm leading-6 text-ink/65">
                  The main mic is disabled while this sidecar resolves the correction and relays it back into the lesson.
                </p>
              </div>
              <div className="rounded-full border border-orange/20 bg-[#fff2ea] px-3 py-1.5 text-xs font-semibold text-[#9c3f20]">
                Pause active
              </div>
            </div>

            {activeCorrectionIssue?.claim ? (
              <div className="mt-4 rounded-[1.2rem] border border-[#f0b28f] bg-[#fff7f1] px-4 py-3 text-sm leading-6 text-[#7a3a1f]">
                <p className="text-[0.68rem] font-semibold uppercase tracking-[0.24em] text-[#c66435]">Flagged claim</p>
                <p className="mt-2">{activeCorrectionIssue.claim}</p>
              </div>
            ) : null}

            <div className="mt-4 space-y-3 rounded-[1.4rem] bg-paper p-4">
              {correctionMessages.length === 0 ? (
                <div className="rounded-[1.1rem] border border-dashed border-ink/10 bg-white px-4 py-5 text-sm leading-6 text-ink/65">
                  {isCorrectionConnecting
                    ? 'Starting the correction agent and switching the mic over now...'
                    : 'The correction agent transcript will appear here as the exchange unfolds.'}
                </div>
              ) : null}
              {correctionMessages.map((message) => (
                <div
                  key={message.id}
                  className={`rounded-[1.1rem] px-4 py-3 text-sm leading-6 ${
                    message.speaker === 'agent' ? 'bg-white text-ink' : 'bg-orange/10 text-ink'
                  }`}
                >
                  <p className="mb-1 text-[0.65rem] uppercase tracking-[0.24em] opacity-70">
                    {message.speaker === 'agent' ? 'Correction agent' : 'You'}
                  </p>
                  {message.text}
                </div>
              ))}
            </div>

            <div className="mt-4 space-y-3">
              {correctionError ? <p className="text-sm text-[#9c3f20]">{correctionError}</p> : null}

              <p className="text-sm leading-6 text-ink/65">
                {isRelayInFlight
                  ? 'The correction agent has finished and TeachBack is handing the summary back to the main tutor.'
                  : activeInputChannel === 'correction'
                    ? 'Speak normally. Your mic is now routed to the correction agent until the issue is resolved.'
                    : 'The correction agent is taking over this part of the lesson.'}
              </p>

              {correctionError ? (
                <button
                  type="button"
                  onClick={forceResumeMain}
                  disabled={isRelayInFlight}
                  className="w-full rounded-full border border-ink/10 bg-white px-5 py-3 text-sm font-semibold text-ink transition hover:border-ink/30 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  Force resume main lesson
                </button>
              ) : null}
            </div>
          </div>
        </div>
      ) : null}
    </main>
  );
}
