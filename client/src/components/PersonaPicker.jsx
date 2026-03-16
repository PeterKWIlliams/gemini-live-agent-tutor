export default function PersonaPicker({ personas, selectedId, onSelect }) {
  return (
    <div className="grid gap-4 lg:grid-cols-3">
      {personas.map((persona) => {
        const selected = selectedId === persona.id;
        return (
          <button
            key={persona.id}
            type="button"
            onClick={() => onSelect(persona.id)}
            className={`rounded-[1.75rem] border p-5 text-left transition ${
              selected
                ? 'border-sage bg-white shadow-glow'
                : 'border-white/70 bg-white/70 hover:-translate-y-0.5 hover:border-sage/50'
            }`}
          >
            <div className="flex items-center justify-between">
              <p className="text-3xl">{persona.emoji}</p>
              <span className="rounded-full bg-blush px-3 py-1 text-xs uppercase tracking-[0.24em] text-orange">
                {persona.voice}
              </span>
            </div>
            <h3 className="mt-4 font-display text-2xl text-ink">{persona.name}</h3>
            <p className="mt-3 text-sm leading-6 text-ink/70">{persona.description}</p>
          </button>
        );
      })}
    </div>
  );
}
