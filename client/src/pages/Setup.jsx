import { useEffect, useMemo, useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import FileUpload from '../components/FileUpload';
import ModePicker from '../components/ModePicker';
import PersonaPicker from '../components/PersonaPicker';
import { getApiBaseUrl } from '../lib/api';

const MATERIAL_TABS = [
  { id: 'upload', label: 'Upload file' },
  { id: 'text', label: 'Paste text' },
  { id: 'topic', label: 'Quick topic' },
  { id: 'preset', label: 'Preset' },
];

const STEP_LABELS = [
  { id: 1, eyebrow: 'Step 1', title: 'Choose material' },
  { id: 2, eyebrow: 'Step 2', title: 'Pick a learning mode' },
  { id: 3, eyebrow: 'Step 3', title: 'Choose a persona' },
  { id: 4, eyebrow: 'Step 4', title: 'Launch the session' },
];

function getMaterialStatus(materialTab, selectedFile, textMaterial, topic) {
  if (materialTab === 'upload') {
    return selectedFile ? selectedFile.name : 'Pick a file to prepare';
  }
  if (materialTab === 'text') {
    return textMaterial.trim() ? 'Text material loaded' : 'Paste notes or use a preset';
  }
  if (materialTab === 'preset') {
    return textMaterial.trim() ? 'Preset material loaded' : 'Choose a ready-made preset';
  }
  return topic.trim() ? topic.trim() : 'Enter a topic to generate study material';
}

export default function Setup({
  modes,
  personas,
  preparedSession,
  selectedModeId,
  selectedPersonaId,
  onPrepared,
  onSelectMode,
  onSelectPersona,
  onStart,
}) {
  const [materialTab, setMaterialTab] = useState('upload');
  const [selectedFile, setSelectedFile] = useState(null);
  const [textMaterial, setTextMaterial] = useState('');
  const [topic, setTopic] = useState('');
  const [topicDescription, setTopicDescription] = useState('');
  const [error, setError] = useState('');
  const [sampleLoading, setSampleLoading] = useState(false);
  const [sampleOptions, setSampleOptions] = useState([]);
  const [activeStep, setActiveStep] = useState(1);

  useEffect(() => {
    let cancelled = false;

    const loadSampleOptions = async () => {
      try {
        setSampleLoading(true);
        const response = await fetch(`${getApiBaseUrl()}/api/sample-material`);
        if (!response.ok) {
          if (response.status !== 404) {
            const payload = await response.json().catch(() => ({}));
            throw new Error(payload.detail || 'Could not load sample material.');
          }
          if (!cancelled) {
            setSampleOptions([]);
          }
          return;
        }

        const data = await response.json();
        if (!cancelled) {
          setSampleOptions(Array.isArray(data.options) ? data.options : []);
        }
      } catch (loadError) {
        if (!cancelled) {
          setError(loadError.message);
        }
      } finally {
        if (!cancelled) {
          setSampleLoading(false);
        }
      }
    };

    loadSampleOptions();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!preparedSession?.session_id) {
      setActiveStep(1);
      return;
    }
    if (!selectedModeId) {
      setActiveStep(2);
      return;
    }
    if (!selectedPersonaId) {
      setActiveStep(3);
      return;
    }
    setActiveStep(4);
  }, [preparedSession?.session_id, selectedModeId, selectedPersonaId]);

  const loadSampleMaterial = (sampleText) => {
    setError('');
    setMaterialTab('preset');
    setSelectedFile(null);
    setTextMaterial(sampleText || '');
  };

  const materialMutation = useMutation({
    mutationFn: async () => {
      const apiBaseUrl = getApiBaseUrl();

      if (materialTab === 'upload') {
        if (!selectedFile) {
          throw new Error('Choose a file first.');
        }

        const formData = new FormData();
        formData.append('file', selectedFile);

        const response = await fetch(`${apiBaseUrl}/api/upload`, {
          method: 'POST',
          body: formData,
        });
        if (!response.ok) {
          throw new Error((await response.json()).detail || 'Upload failed.');
        }
        return response.json();
      }

      if (materialTab === 'text') {
        if (!textMaterial.trim()) {
          throw new Error('Paste some study material first.');
        }

        const formData = new FormData();
        formData.append('text', textMaterial);

        const response = await fetch(`${apiBaseUrl}/api/upload`, {
          method: 'POST',
          body: formData,
        });
        if (!response.ok) {
          throw new Error((await response.json()).detail || 'Text preparation failed.');
        }
        return response.json();
      }

      if (materialTab === 'preset') {
        if (!textMaterial.trim()) {
          throw new Error('Choose a preset first.');
        }

        const formData = new FormData();
        formData.append('text', textMaterial);

        const response = await fetch(`${apiBaseUrl}/api/upload`, {
          method: 'POST',
          body: formData,
        });
        if (!response.ok) {
          throw new Error((await response.json()).detail || 'Preset preparation failed.');
        }
        return response.json();
      }

      if (!topic.trim()) {
        throw new Error('Enter a topic name first.');
      }

      const response = await fetch(`${apiBaseUrl}/api/topic`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          topic,
          description: topicDescription,
        }),
      });
      if (!response.ok) {
        throw new Error((await response.json()).detail || 'Topic generation failed.');
      }
      return response.json();
    },
    onSuccess: (data) => {
      setError('');
      onPrepared(data);
      setActiveStep(2);
    },
    onError: (mutationError) => {
      setError(mutationError.message);
    },
  });

  const handleModeSelect = (modeId) => {
    onSelectMode(modeId);
    setActiveStep(3);
  };

  const handlePersonaSelect = (personaId) => {
    onSelectPersona(personaId);
    setActiveStep(4);
  };

  const canStart = Boolean(preparedSession?.session_id && selectedModeId && selectedPersonaId);
  const selectedMode = modes.find((item) => item.id === selectedModeId) || null;
  const selectedPersona = personas.find((item) => item.id === selectedPersonaId) || null;

  const materialStatus = useMemo(
    () => getMaterialStatus(materialTab, selectedFile, textMaterial, topic),
    [materialTab, selectedFile, textMaterial, topic],
  );

  const summaryLabel = useMemo(() => {
    if (!preparedSession) {
      return 'Nothing prepared yet';
    }
    return preparedSession.material_preview;
  }, [preparedSession]);

  return (
    <main className="min-h-screen bg-[radial-gradient(circle_at_top_left,_rgba(255,236,217,0.95),_rgba(250,247,238,0.82)_42%,_rgba(221,237,225,0.78)_100%)] px-4 py-8 sm:px-6 lg:px-8">
      <section className="mx-auto grid max-w-7xl gap-8 xl:grid-cols-[0.88fr_1.12fr]">
        <aside className="space-y-6">
          <div className="overflow-hidden rounded-[2.75rem] border border-white/60 bg-[linear-gradient(135deg,rgba(26,41,34,0.98),rgba(53,78,58,0.95))] p-7 text-sand shadow-glow">
            <p className="text-xs uppercase tracking-[0.38em] text-sand/55">TeachBack</p>
            <h1 className="mt-4 max-w-md font-display text-5xl leading-[0.94] text-white md:text-6xl">
              Build your session one smart choice at a time.
            </h1>
            <p className="mt-5 max-w-xl text-sm leading-7 text-sand/78">
              First choose the source material, then the teaching technique, then the personality you want pushing
              back. The flow only shows the decision you need right now.
            </p>

            <div className="mt-8 space-y-4">
              {STEP_LABELS.map((step) => {
                const unlocked = step.id === 1 || activeStep >= step.id;
                const complete =
                  (step.id === 1 && preparedSession?.session_id) ||
                  (step.id === 2 && selectedModeId) ||
                  (step.id === 3 && selectedPersonaId);
                const current = activeStep === step.id;

                return (
                  <button
                    key={step.id}
                    type="button"
                    onClick={() => unlocked && setActiveStep(step.id)}
                    className={`flex w-full items-center gap-4 rounded-[1.6rem] border px-4 py-4 text-left transition ${
                      current
                        ? 'border-white/60 bg-white/12'
                        : complete
                          ? 'border-sand/15 bg-white/6'
                          : 'border-white/8 bg-transparent'
                    } ${unlocked ? 'cursor-pointer' : 'cursor-default opacity-55'}`}
                  >
                    <div
                      className={`flex h-11 w-11 shrink-0 items-center justify-center rounded-full text-sm font-semibold ${
                        complete ? 'bg-sage text-white' : current ? 'bg-orange text-white' : 'bg-white/10 text-sand'
                      }`}
                    >
                      {complete ? '✓' : `0${step.id}`}
                    </div>
                    <div>
                      <p className="text-[0.68rem] uppercase tracking-[0.28em] text-sand/50">{step.eyebrow}</p>
                      <p className="mt-1 text-base font-semibold text-white">{step.title}</p>
                    </div>
                  </button>
                );
              })}
            </div>
          </div>

          <div className="rounded-[2.5rem] border border-white/60 bg-white/75 p-6 shadow-glow backdrop-blur">
            <p className="text-xs uppercase tracking-[0.28em] text-moss/70">Session snapshot</p>
            <div className="mt-4 space-y-4 text-sm leading-6 text-ink/72">
              <div className="rounded-[1.4rem] bg-paper p-4">
                <p className="text-[0.68rem] uppercase tracking-[0.24em] text-moss/65">Material</p>
                <p className="mt-2 font-semibold text-ink">{preparedSession ? 'Prepared' : materialStatus}</p>
                <p className="mt-2 text-ink/65">{summaryLabel}</p>
              </div>
              <div className="rounded-[1.4rem] bg-paper p-4">
                <p className="text-[0.68rem] uppercase tracking-[0.24em] text-moss/65">Mode</p>
                <p className="mt-2 font-semibold text-ink">
                  {selectedMode ? `${selectedMode.emoji} ${selectedMode.name}` : 'Choose after material is ready'}
                </p>
              </div>
              <div className="rounded-[1.4rem] bg-paper p-4">
                <p className="text-[0.68rem] uppercase tracking-[0.24em] text-moss/65">Persona</p>
                <p className="mt-2 font-semibold text-ink">
                  {selectedPersona ? `${selectedPersona.emoji} ${selectedPersona.name}` : 'Choose after mode'}
                </p>
              </div>
            </div>
          </div>
        </aside>

        <section className="space-y-6">
          {activeStep === 1 ? (
            <section className="rounded-[2.75rem] border border-white/60 bg-white/78 p-7 shadow-glow backdrop-blur">
              <div className="flex flex-wrap items-center justify-between gap-4">
                <div>
                  <p className="text-xs uppercase tracking-[0.3em] text-moss/70">Step 1</p>
                  <h2 className="mt-2 font-display text-4xl text-ink md:text-5xl">What should TeachBack ground itself on?</h2>
                  <p className="mt-3 max-w-2xl text-sm leading-7 text-ink/70">
                    Start with the source of truth. Once your material is prepared, we will move you forward to the
                    teaching style.
                  </p>
                </div>
                <div className="flex rounded-full border border-ink/10 bg-sand/80 p-1">
                  {MATERIAL_TABS.map((tab) => (
                    <button
                      key={tab.id}
                      type="button"
                      onClick={() => setMaterialTab(tab.id)}
                      className={`rounded-full px-4 py-2 text-sm transition ${
                        materialTab === tab.id ? 'bg-ink text-sand' : 'text-ink/65'
                      }`}
                    >
                      {tab.label}
                    </button>
                  ))}
                </div>
              </div>

              {sampleLoading ? (
                <div className="mt-6 rounded-[1.5rem] border border-ink/10 bg-sand/70 px-4 py-3 text-sm text-ink/65">
                  Loading sample material options...
                </div>
              ) : null}

              <div className="mt-6">
                {materialTab === 'upload' ? (
                  <FileUpload disabled={materialMutation.isPending} onSelect={setSelectedFile} />
                ) : null}

                {materialTab === 'text' ? (
                  <div className="rounded-[2rem] border border-white/70 bg-white/75 p-6 shadow-glow">
                    <label className="text-xs uppercase tracking-[0.28em] text-moss/70">Paste notes or transcript</label>
                    <textarea
                      value={textMaterial}
                      onChange={(event) => setTextMaterial(event.target.value)}
                      className="mt-4 min-h-64 w-full rounded-[1.5rem] border border-ink/10 bg-sand/80 p-4 text-sm leading-6 text-ink outline-none transition focus:border-orange"
                      placeholder="Paste textbook notes, lecture transcript, or a rough explanation of the topic..."
                    />
                  </div>
                ) : null}

                {materialTab === 'topic' ? (
                  <div className="grid gap-4 rounded-[2rem] border border-white/70 bg-white/75 p-6 shadow-glow">
                    <div>
                      <label className="text-xs uppercase tracking-[0.28em] text-moss/70">Topic</label>
                      <input
                        value={topic}
                        onChange={(event) => setTopic(event.target.value)}
                        className="mt-3 w-full rounded-full border border-ink/10 bg-sand/80 px-5 py-3 text-sm text-ink outline-none transition focus:border-orange"
                        placeholder="System design for software engineering interviews"
                      />
                    </div>
                    <div>
                      <label className="text-xs uppercase tracking-[0.28em] text-moss/70">Focus notes (optional)</label>
                      <textarea
                        value={topicDescription}
                        onChange={(event) => setTopicDescription(event.target.value)}
                        className="mt-3 min-h-44 w-full rounded-[1.5rem] border border-ink/10 bg-sand/80 p-4 text-sm leading-6 text-ink outline-none transition focus:border-orange"
                        placeholder="What part are you studying, and what do you want help understanding?"
                      />
                    </div>
                  </div>
                ) : null}

                {materialTab === 'preset' ? (
                  <div className="rounded-[2rem] border border-white/70 bg-white/75 p-6 shadow-glow">
                    <div className="flex flex-wrap items-end justify-between gap-3">
                      <div>
                        <label className="text-xs uppercase tracking-[0.28em] text-moss/70">Choose a preset topic</label>
                        <p className="mt-2 text-sm leading-6 text-ink/70">
                          Ready-made material for quick demos and judge testing.
                        </p>
                      </div>
                    </div>
                    <div className="mt-4 grid gap-3 sm:grid-cols-2">
                      {sampleOptions.map((option) => (
                        <button
                          key={option.id}
                          type="button"
                          onClick={() => loadSampleMaterial(option.text)}
                          disabled={materialMutation.isPending}
                          className={`rounded-[1.6rem] border p-4 text-left transition disabled:cursor-not-allowed disabled:opacity-60 ${
                            textMaterial === option.text
                              ? 'border-orange bg-[#fff5ed]'
                              : 'border-ink/10 bg-sand/70 hover:-translate-y-0.5 hover:border-orange hover:bg-[#fff5ed]'
                          }`}
                        >
                          <p className="font-semibold text-ink">{option.label}</p>
                          <p className="mt-2 text-sm leading-6 text-ink/65">{option.preview}</p>
                        </button>
                      ))}
                    </div>
                    {!sampleOptions.length && !sampleLoading ? (
                      <p className="mt-4 text-sm text-ink/60">No presets are configured right now.</p>
                    ) : null}
                  </div>
                ) : null}
              </div>

              <div className="mt-6 flex flex-wrap items-center justify-between gap-4 rounded-[1.75rem] bg-mist/70 p-4">
                <div className="text-sm leading-6 text-ink/70">
                  <p className="font-semibold text-ink">Current material input</p>
                  <p>{materialStatus}</p>
                  {selectedFile ? <p className="text-xs text-ink/55">Selected file: {selectedFile.name}</p> : null}
                </div>
                <button
                  type="button"
                  onClick={() => materialMutation.mutate()}
                  disabled={materialMutation.isPending}
                  className="rounded-full bg-orange px-6 py-3 text-sm font-semibold text-white transition hover:bg-[#e3763b] disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {materialMutation.isPending ? 'Preparing...' : 'Prepare material'}
                </button>
              </div>
            </section>
          ) : null}

          {activeStep === 2 ? (
            <section className="rounded-[2.75rem] border border-white/60 bg-white/78 p-7 shadow-glow backdrop-blur">
              <p className="text-xs uppercase tracking-[0.3em] text-moss/70">Step 2</p>
              <h2 className="mt-2 font-display text-4xl text-ink md:text-5xl">How should the session teach?</h2>
              <p className="mt-3 max-w-2xl text-sm leading-7 text-ink/70">
                Your material is ready. Now choose the learning technique that shapes the conversation.
              </p>
              <div className="mt-6">
                <ModePicker modes={modes} selectedId={selectedModeId} onSelect={handleModeSelect} />
              </div>
              <div className="mt-6 flex justify-between gap-3">
                <button
                  type="button"
                  onClick={() => setActiveStep(1)}
                  className="rounded-full border border-ink/10 bg-white px-5 py-3 text-sm font-semibold text-ink transition hover:border-ink/30"
                >
                  Back to material
                </button>
              </div>
            </section>
          ) : null}

          {activeStep === 3 ? (
            <section className="rounded-[2.75rem] border border-white/60 bg-white/78 p-7 shadow-glow backdrop-blur">
              <p className="text-xs uppercase tracking-[0.3em] text-moss/70">Step 3</p>
              <h2 className="mt-2 font-display text-4xl text-ink md:text-5xl">Who should challenge the learner?</h2>
              <p className="mt-3 max-w-2xl text-sm leading-7 text-ink/70">
                Pick the voice and pressure style. The objective stays the same, but the energy changes completely.
              </p>
              <div className="mt-6">
                <PersonaPicker personas={personas} selectedId={selectedPersonaId} onSelect={handlePersonaSelect} />
              </div>
              <div className="mt-6 flex justify-between gap-3">
                <button
                  type="button"
                  onClick={() => setActiveStep(2)}
                  className="rounded-full border border-ink/10 bg-white px-5 py-3 text-sm font-semibold text-ink transition hover:border-ink/30"
                >
                  Back to modes
                </button>
              </div>
            </section>
          ) : null}

          {activeStep === 4 ? (
            <section className="rounded-[2.75rem] border border-white/60 bg-[linear-gradient(135deg,rgba(255,255,255,0.86),rgba(248,235,226,0.86))] p-7 shadow-glow backdrop-blur">
              <p className="text-xs uppercase tracking-[0.3em] text-moss/70">Step 4</p>
              <h2 className="mt-2 font-display text-4xl text-ink md:text-5xl">Everything is lined up.</h2>
              <p className="mt-3 max-w-2xl text-sm leading-7 text-ink/70">
                Start the live session when you are ready. You can always come back and adjust the material, mode, or
                persona before launch.
              </p>

              <div className="mt-8 grid gap-4 md:grid-cols-3">
                <div className="rounded-[1.7rem] bg-white/80 p-5">
                  <p className="text-[0.68rem] uppercase tracking-[0.24em] text-moss/65">Material</p>
                  <p className="mt-2 font-semibold text-ink">{summaryLabel}</p>
                </div>
                <div className="rounded-[1.7rem] bg-white/80 p-5">
                  <p className="text-[0.68rem] uppercase tracking-[0.24em] text-moss/65">Mode</p>
                  <p className="mt-2 font-semibold text-ink">{selectedMode ? `${selectedMode.emoji} ${selectedMode.name}` : 'Not selected'}</p>
                </div>
                <div className="rounded-[1.7rem] bg-white/80 p-5">
                  <p className="text-[0.68rem] uppercase tracking-[0.24em] text-moss/65">Persona</p>
                  <p className="mt-2 font-semibold text-ink">
                    {selectedPersona ? `${selectedPersona.emoji} ${selectedPersona.name}` : 'Not selected'}
                  </p>
                </div>
              </div>

              <div className="mt-8 flex flex-wrap gap-3">
                <button
                  type="button"
                  disabled={!canStart}
                  onClick={onStart}
                  className="rounded-full bg-ink px-7 py-4 text-sm font-semibold text-sand transition hover:bg-moss disabled:cursor-not-allowed disabled:opacity-50"
                >
                  Start session
                </button>
                <button
                  type="button"
                  onClick={() => setActiveStep(3)}
                  className="rounded-full border border-ink/10 bg-white px-5 py-4 text-sm font-semibold text-ink transition hover:border-ink/30"
                >
                  Back to persona
                </button>
                <button
                  type="button"
                  onClick={() => setActiveStep(1)}
                  className="rounded-full border border-ink/10 bg-white px-5 py-4 text-sm font-semibold text-ink transition hover:border-ink/30"
                >
                  Edit material
                </button>
              </div>
            </section>
          ) : null}

          {error ? (
            <p className="rounded-[1.5rem] border border-[#d8b3a1] bg-[#fff2ec] px-4 py-3 text-sm text-[#9c3f20]">
              {error}
            </p>
          ) : null}
        </section>
      </section>
    </main>
  );
}
