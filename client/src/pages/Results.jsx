import ScoreCard from '../components/ScoreCard';

export default function Results({ scores, mode, persona, onNewSession, onTryDifferentMode }) {
  return (
    <main className="mx-auto min-h-screen max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
      <div className="rounded-[2.75rem] border border-white/60 bg-white/75 p-7 shadow-glow backdrop-blur">
        <div className="grid gap-6 lg:grid-cols-[0.85fr_1.15fr] lg:items-end">
          <div>
            <p className="text-xs uppercase tracking-[0.35em] text-moss/70">Session complete</p>
            <h1 className="mt-4 font-display text-6xl leading-none text-ink md:text-7xl">
              {scores.overall_score}
            </h1>
            <p className="mt-5 text-sm leading-6 text-ink/70">
              {mode.name} with {persona.name} {persona.emoji}
            </p>
            <p className="mt-4 max-w-xl text-base leading-7 text-ink/75">
              TeachBack turned your session into a performance snapshot so you can see what landed, what still feels
              fuzzy, and what to practice next.
            </p>
            <div className="mt-8 flex flex-wrap gap-3">
              <button
                type="button"
                onClick={onNewSession}
                className="rounded-full bg-ink px-5 py-3 text-sm font-semibold text-sand transition hover:bg-moss"
              >
                New session
              </button>
              <button
                type="button"
                onClick={onTryDifferentMode}
                className="rounded-full border border-ink/10 bg-white px-5 py-3 text-sm font-semibold text-ink transition hover:border-ink/30"
              >
                Try different mode
              </button>
            </div>
          </div>

          <div className="rounded-[2rem] bg-paper p-6">
            <p className="text-xs uppercase tracking-[0.3em] text-moss/70">Read this as momentum, not judgment</p>
            <p className="mt-4 text-sm leading-7 text-ink/75">
              Strong learning sessions are messy on the way through. The point is to expose what you understand, where
              you are bluffing, and what deserves one more pass.
            </p>
          </div>
        </div>

        <div className="mt-8">
          <ScoreCard scores={scores} />
        </div>
      </div>
    </main>
  );
}
