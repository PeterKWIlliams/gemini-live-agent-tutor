import { useMemo, useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import FileUpload from '../components/FileUpload';
import ModePicker from '../components/ModePicker';
import PersonaPicker from '../components/PersonaPicker';
import { getApiBaseUrl } from '../lib/api';

const MATERIAL_TABS = [
  { id: 'upload', label: 'Upload' },
  { id: 'text', label: 'Paste text' },
  { id: 'topic', label: 'Quick topic' },
];

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

  const loadSampleMaterial = async () => {
    try {
      setSampleLoading(true);
      setError('');

      const response = await fetch(`${getApiBaseUrl()}/api/sample-material`);
      if (!response.ok) {
        throw new Error((await response.json()).detail || 'Sample material is not configured.');
      }

      const data = await response.json();
      setMaterialTab('text');
      setSelectedFile(null);
      setTextMaterial(data.text || '');
    } catch (loadError) {
      setError(loadError.message);
    } finally {
      setSampleLoading(false);
    }
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
    },
    onError: (mutationError) => {
      setError(mutationError.message);
    },
  });

  const canStart = Boolean(preparedSession?.session_id && selectedModeId && selectedPersonaId);

  const summaryLabel = useMemo(() => {
    if (!preparedSession) {
      return 'No material loaded yet';
    }
    return `Ready: ${preparedSession.material_preview}`;
  }, [preparedSession]);

  return (
    <main className="mx-auto min-h-screen max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
      <section className="grid gap-8 lg:grid-cols-[1.05fr_0.95fr]">
        <div className="space-y-8">
          <div className="rounded-[2.5rem] border border-white/60 bg-white/70 p-7 shadow-glow backdrop-blur">
            <p className="text-xs uppercase tracking-[0.35em] text-moss/70">TeachBack</p>
            <h1 className="mt-4 max-w-3xl font-display text-5xl leading-[0.95] text-ink md:text-7xl">
              Learn by saying it back until it clicks.
            </h1>
            <p className="mt-5 max-w-2xl text-base leading-7 text-ink/70">
              Upload material, choose a learning style, and talk your way to deeper understanding with a live AI
              companion that pushes, probes, and adapts.
            </p>
            <div className="mt-6 flex flex-wrap gap-3">
              {['Voice-first', 'Live transcript', 'Persona-led', 'Scorecard feedback'].map((tag) => (
                <span key={tag} className="rounded-full border border-ink/10 bg-sand px-4 py-2 text-xs uppercase tracking-[0.2em] text-ink/65">
                  {tag}
                </span>
              ))}
            </div>
          </div>

          <section className="rounded-[2.5rem] border border-white/60 bg-white/70 p-7 shadow-glow backdrop-blur">
            <div className="flex flex-wrap items-center justify-between gap-4">
              <div>
                <p className="text-xs uppercase tracking-[0.3em] text-moss/70">Step 1</p>
                <h2 className="mt-2 font-display text-4xl text-ink">Provide material</h2>
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

            <div className="mt-6">
              <div className="mb-4 flex justify-end">
                <button
                  type="button"
                  onClick={loadSampleMaterial}
                  disabled={sampleLoading || materialMutation.isPending}
                  className="rounded-full border border-ink/10 bg-sand px-4 py-2 text-sm font-semibold text-ink transition hover:border-orange hover:text-orange disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {sampleLoading ? 'Loading sample...' : 'Use sample material'}
                </button>
              </div>

              {materialTab === 'upload' ? (
                <FileUpload disabled={materialMutation.isPending} onSelect={setSelectedFile} />
              ) : null}

              {materialTab === 'text' ? (
                <div className="rounded-[2rem] border border-white/70 bg-white/75 p-6 shadow-glow">
                  <label className="text-xs uppercase tracking-[0.28em] text-moss/70">Paste notes or transcript</label>
                  <textarea
                    value={textMaterial}
                    onChange={(event) => setTextMaterial(event.target.value)}
                    className="mt-4 min-h-52 w-full rounded-[1.5rem] border border-ink/10 bg-sand/80 p-4 text-sm leading-6 text-ink outline-none transition focus:border-orange"
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
                      placeholder="Quantum mechanics"
                    />
                  </div>
                  <div>
                    <label className="text-xs uppercase tracking-[0.28em] text-moss/70">Focus notes (optional)</label>
                    <textarea
                      value={topicDescription}
                      onChange={(event) => setTopicDescription(event.target.value)}
                      className="mt-3 min-h-40 w-full rounded-[1.5rem] border border-ink/10 bg-sand/80 p-4 text-sm leading-6 text-ink outline-none transition focus:border-orange"
                      placeholder="What part are you studying, and what do you want help understanding?"
                    />
                  </div>
                </div>
              ) : null}
            </div>

            <div className="mt-6 flex flex-wrap items-center justify-between gap-3 rounded-[1.75rem] bg-mist/70 p-4">
              <div className="text-sm leading-6 text-ink/70">
                <p className="font-semibold text-ink">Material status</p>
                <p>{summaryLabel}</p>
                {selectedFile ? <p className="text-xs text-ink/55">Selected file: {selectedFile.name}</p> : null}
              </div>
              <button
                type="button"
                onClick={() => materialMutation.mutate()}
                disabled={materialMutation.isPending}
                className="rounded-full bg-orange px-5 py-3 text-sm font-semibold text-white transition hover:bg-[#e3763b] disabled:cursor-not-allowed disabled:opacity-60"
              >
                {materialMutation.isPending ? 'Preparing...' : 'Prepare material'}
              </button>
            </div>
            {error ? <p className="mt-4 text-sm text-[#9c3f20]">{error}</p> : null}
          </section>
        </div>

        <div className="space-y-8">
          <section className="rounded-[2.5rem] border border-white/60 bg-white/70 p-7 shadow-glow backdrop-blur">
            <p className="text-xs uppercase tracking-[0.3em] text-moss/70">Step 2</p>
            <h2 className="mt-2 font-display text-4xl text-ink">Pick a learning mode</h2>
            <p className="mt-3 text-sm leading-6 text-ink/70">
              Each mode uses a different teaching technique, from Socratic questioning to active recall.
            </p>
            <div className="mt-6">
              <ModePicker modes={modes} selectedId={selectedModeId} onSelect={onSelectMode} />
            </div>
          </section>

          <section className="rounded-[2.5rem] border border-white/60 bg-white/70 p-7 shadow-glow backdrop-blur">
            <p className="text-xs uppercase tracking-[0.3em] text-moss/70">Step 3</p>
            <h2 className="mt-2 font-display text-4xl text-ink">Choose who you learn with</h2>
            <p className="mt-3 text-sm leading-6 text-ink/70">
              Persona changes the voice, tone, and pressure style without changing the learning objective.
            </p>
            <div className="mt-6">
              <PersonaPicker personas={personas} selectedId={selectedPersonaId} onSelect={onSelectPersona} />
            </div>
          </section>

          <section className="rounded-[2.5rem] border border-ink/10 bg-ink p-7 text-sand shadow-glow">
            <p className="text-xs uppercase tracking-[0.3em] text-sand/60">Launch</p>
            <div className="mt-3 flex flex-col gap-5 lg:flex-row lg:items-end lg:justify-between">
              <div>
                <h2 className="font-display text-4xl">Start your live session</h2>
                <p className="mt-3 max-w-xl text-sm leading-6 text-sand/70">
                  You will speak, the AI will respond in real time, and the session ends with structured feedback.
                </p>
              </div>
              <button
                type="button"
                disabled={!canStart}
                onClick={onStart}
                className="rounded-full bg-sage px-6 py-4 text-sm font-semibold text-white transition hover:bg-moss disabled:cursor-not-allowed disabled:opacity-50"
              >
                Start session
              </button>
            </div>
          </section>
        </div>
      </section>
    </main>
  );
}
