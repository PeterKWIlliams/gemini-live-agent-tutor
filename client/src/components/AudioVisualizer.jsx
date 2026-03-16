export default function AudioVisualizer({ audioLevel = 0, isRecording, status }) {
  const scaledLevel = Math.max(0.25, Math.min(1, audioLevel * 3));

  return (
    <div className="flex items-center gap-4 rounded-full border border-white/60 bg-white/75 px-4 py-3 backdrop-blur">
      <div className="relative flex h-12 w-12 items-center justify-center">
        <span
          className={`absolute inset-0 rounded-full bg-orange/25 transition ${
            isRecording ? 'animate-ping' : 'opacity-0'
          }`}
        />
        <span
          className="absolute inset-1 rounded-full bg-sage/20 transition duration-200"
          style={{ transform: `scale(${scaledLevel})` }}
        />
        <span className="relative h-5 w-5 rounded-full bg-ink" />
      </div>
      <div className="flex items-end gap-1">
        {[0.4, 0.65, 0.9, 0.65, 0.4].map((bar, index) => (
          <span
            key={`${bar}-${index}`}
            className="w-1 rounded-full bg-moss transition-all duration-150"
            style={{ height: `${18 + audioLevel * 34 * bar}px` }}
          />
        ))}
      </div>
      <div>
        <p className="text-xs uppercase tracking-[0.24em] text-moss/75">Audio</p>
        <p className="text-sm font-semibold text-ink">{status}</p>
      </div>
    </div>
  );
}
