import { useEffect, useRef } from 'react';

export default function Transcript({ messages }) {
  const containerRef = useRef(null);
  const endRef = useRef(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) {
      return;
    }
    container.scrollTo({
      top: container.scrollHeight,
      behavior: 'smooth',
    });
  }, [messages]);

  return (
    <div className="rounded-[2rem] border border-white/65 bg-white/80 p-5 shadow-glow backdrop-blur">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-xs uppercase tracking-[0.3em] text-moss/70">Live transcript</p>
          <h2 className="mt-2 font-display text-3xl text-ink">Think out loud.</h2>
        </div>
        <span className="rounded-full border border-ink/10 bg-sand px-3 py-1 text-xs text-ink/65">
          Auto-updating
        </span>
      </div>

      <div ref={containerRef} className="mt-6 flex max-h-[52vh] flex-col gap-3 overflow-y-auto pr-1">
        {messages.length === 0 ? (
          <div className="rounded-[1.5rem] border border-dashed border-ink/10 bg-sand/70 p-5 text-sm leading-6 text-ink/65">
            Once the session starts, TeachBack will stream both your words and the AI's responses here.
          </div>
        ) : null}

        {messages.map((message) => (
          <div
            key={message.id}
            className={`max-w-[85%] rounded-[1.5rem] px-4 py-3 text-sm leading-6 shadow-sm ${
              message.speaker === 'user'
                ? 'ml-auto bg-ink text-sand'
                : 'mr-auto border border-orange/20 bg-blush text-ink'
            }`}
          >
            <p className="mb-1 text-[0.65rem] uppercase tracking-[0.24em] opacity-70">
              {message.speaker === 'user' ? 'You' : 'TeachBack'}
            </p>
            <p>{message.text}</p>
          </div>
        ))}
        <div ref={endRef} />
      </div>
    </div>
  );
}
