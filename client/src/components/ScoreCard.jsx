const METRICS = [
  { key: 'accuracy_score', label: 'Accuracy' },
  { key: 'completeness_score', label: 'Completeness' },
  { key: 'clarity_score', label: 'Clarity' },
  { key: 'depth_score', label: 'Depth' },
];

function tone(score) {
  if (score >= 80) return 'bg-sage text-white';
  if (score >= 50) return 'bg-orange text-white';
  return 'bg-ink text-white';
}

export default function ScoreCard({ scores }) {
  return (
    <div className="grid gap-6">
      <section className="grid gap-4 lg:grid-cols-4">
        {METRICS.map((metric) => (
          <article key={metric.key} className="rounded-[1.75rem] border border-white/70 bg-white/80 p-5 shadow-glow">
            <p className="text-xs uppercase tracking-[0.24em] text-moss/70">{metric.label}</p>
            <div className="mt-4 flex items-end justify-between gap-3">
              <div className={`rounded-2xl px-4 py-3 text-3xl font-black ${tone(scores[metric.key])}`}>
                {scores[metric.key]}
              </div>
              <div className="h-20 w-3 overflow-hidden rounded-full bg-mist">
                <div
                  className="w-full rounded-full bg-gradient-to-t from-orange to-sage transition-all duration-700"
                  style={{ height: `${scores[metric.key]}%`, marginTop: `${100 - scores[metric.key]}%` }}
                />
              </div>
            </div>
          </article>
        ))}
      </section>

      <section className="grid gap-4 lg:grid-cols-2">
        <ListCard title="Strengths" icon="✅" items={scores.strengths} />
        <ListCard title="Gaps" icon="⚠️" items={scores.gaps} />
        <ListCard
          title="Misconceptions"
          icon="❌"
          items={scores.misconceptions.length ? scores.misconceptions : ['None found. Nice work.']}
        />
        <ListCard title="Next Steps" icon="📚" items={scores.next_steps} />
      </section>
    </div>
  );
}

function ListCard({ title, icon, items }) {
  return (
    <article className="rounded-[1.75rem] border border-white/70 bg-white/80 p-5 shadow-glow">
      <div className="flex items-center gap-3">
        <span className="text-2xl">{icon}</span>
        <h3 className="font-display text-2xl text-ink">{title}</h3>
      </div>
      <ul className="mt-4 space-y-3 text-sm leading-6 text-ink/75">
        {items.map((item) => (
          <li key={item} className="rounded-2xl bg-sand px-4 py-3">
            {item}
          </li>
        ))}
      </ul>
    </article>
  );
}
