export default function ModePicker({ modes, selectedId, onSelect }) {
  return (
    <div className="grid gap-4 md:grid-cols-2">
      {modes.map((mode) => {
        const selected = selectedId === mode.id;
        return (
          <button
            key={mode.id}
            type="button"
            onClick={() => onSelect(mode.id)}
            className={`group rounded-[1.75rem] border p-5 text-left transition ${
              selected
                ? 'border-orange bg-white shadow-glow'
                : 'border-white/70 bg-white/70 hover:-translate-y-0.5 hover:border-orange/50'
            }`}
          >
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="text-2xl">{mode.emoji}</p>
                <h3 className="mt-3 font-display text-2xl text-ink">{mode.name}</h3>
              </div>
              <span className="rounded-full bg-mist px-3 py-1 text-xs uppercase tracking-[0.18em] text-moss">
                {mode.technique}
              </span>
            </div>
            <p className="mt-4 text-sm leading-6 text-ink/70">{mode.description}</p>
          </button>
        );
      })}
    </div>
  );
}
