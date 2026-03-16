import { useEffect, useMemo, useState } from 'react';
import { getApiBaseUrl } from './lib/api';
import Results from './pages/Results';
import Session from './pages/Session';
import Setup from './pages/Setup';

export default function App() {
  const [modes, setModes] = useState([]);
  const [personas, setPersonas] = useState([]);
  const [page, setPage] = useState('setup');
  const [preparedSession, setPreparedSession] = useState(null);
  const [selectedModeId, setSelectedModeId] = useState('');
  const [selectedPersonaId, setSelectedPersonaId] = useState('');
  const [results, setResults] = useState(null);
  const [loadingError, setLoadingError] = useState('');

  useEffect(() => {
    async function loadOptions() {
      try {
        const apiBaseUrl = getApiBaseUrl();
        const [modesResponse, personasResponse] = await Promise.all([
          fetch(`${apiBaseUrl}/api/modes`),
          fetch(`${apiBaseUrl}/api/personas`),
        ]);

        if (!modesResponse.ok || !personasResponse.ok) {
          throw new Error('Failed to load configuration.');
        }

        const [modesData, personasData] = await Promise.all([modesResponse.json(), personasResponse.json()]);
        setModes(modesData);
        setPersonas(personasData);
      } catch (error) {
        console.error(error);
        setLoadingError('Could not load modes or personas. Make sure the backend is running.');
      }
    }

    loadOptions();
  }, []);

  const selectedMode = useMemo(
    () => modes.find((item) => item.id === selectedModeId) || modes[0] || null,
    [modes, selectedModeId],
  );

  const selectedPersona = useMemo(
    () => personas.find((item) => item.id === selectedPersonaId) || personas[0] || null,
    [personas, selectedPersonaId],
  );

  const startSession = () => {
    if (!preparedSession?.session_id || !selectedMode || !selectedPersona) {
      return;
    }
    setResults(null);
    setPage('session');
  };

  const handleSessionComplete = (scoreData) => {
    setResults(scoreData);
    setPage('results');
  };

  const resetForNewSession = () => {
    setPreparedSession(null);
    setSelectedModeId('');
    setSelectedPersonaId('');
    setResults(null);
    setPage('setup');
  };

  const tryDifferentMode = () => {
    setSelectedModeId('');
    setSelectedPersonaId('');
    setResults(null);
    setPage('setup');
  };

  if (loadingError) {
    return (
      <main className="mx-auto flex min-h-screen max-w-3xl items-center px-6">
        <div className="rounded-[2rem] border border-white/60 bg-white/80 p-8 shadow-glow">
          <p className="text-xs uppercase tracking-[0.3em] text-moss/70">TeachBack</p>
          <h1 className="mt-3 font-display text-5xl text-ink">Setup issue</h1>
          <p className="mt-4 text-sm leading-6 text-ink/70">{loadingError}</p>
        </div>
      </main>
    );
  }

  return (
    <>
      {page === 'setup' ? (
        <Setup
          modes={modes}
          personas={personas}
          preparedSession={preparedSession}
          selectedModeId={selectedModeId}
          selectedPersonaId={selectedPersonaId}
          onPrepared={setPreparedSession}
          onSelectMode={setSelectedModeId}
          onSelectPersona={setSelectedPersonaId}
          onStart={startSession}
        />
      ) : null}

      {page === 'session' && preparedSession && selectedMode && selectedPersona ? (
        <Session
          session={preparedSession}
          mode={selectedMode}
          persona={selectedPersona}
          onComplete={handleSessionComplete}
          onAbort={() => setPage('setup')}
        />
      ) : null}

      {page === 'results' && results && selectedMode && selectedPersona ? (
        <Results
          scores={results}
          mode={selectedMode}
          persona={selectedPersona}
          onNewSession={resetForNewSession}
          onTryDifferentMode={tryDifferentMode}
        />
      ) : null}
    </>
  );
}
