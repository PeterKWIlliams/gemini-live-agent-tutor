import { useRef } from 'react';

export default function FileUpload({ disabled, onSelect }) {
  const inputRef = useRef(null);

  const handleDrop = (event) => {
    event.preventDefault();
    if (disabled) {
      return;
    }

    const [file] = event.dataTransfer.files;
    if (file) {
      onSelect(file);
    }
  };

  return (
    <div
      className="relative overflow-hidden rounded-[2rem] border border-white/70 bg-white/75 p-6 shadow-glow backdrop-blur"
      onDragOver={(event) => event.preventDefault()}
      onDrop={handleDrop}
    >
      <input
        ref={inputRef}
        type="file"
        accept=".pdf,.png,.jpg,.jpeg,.txt,.webp"
        className="hidden"
        onChange={(event) => {
          const [file] = event.target.files;
          if (file) {
            onSelect(file);
          }
        }}
      />
      <div className="absolute inset-x-8 top-0 h-px bg-gradient-to-r from-transparent via-orange/60 to-transparent" />
      <p className="text-xs uppercase tracking-[0.3em] text-moss/70">Upload material</p>
      <h3 className="mt-3 font-display text-3xl text-ink">Drop notes, PDFs, or screenshots.</h3>
      <p className="mt-3 max-w-xl text-sm leading-6 text-ink/70">
        TeachBack can ground the session on PDFs, slides, notes, textbook excerpts, and image-based study
        material.
      </p>
      <button
        type="button"
        disabled={disabled}
        onClick={() => inputRef.current?.click()}
        className="mt-6 rounded-full bg-ink px-5 py-3 text-sm font-semibold text-sand transition hover:bg-moss disabled:cursor-not-allowed disabled:opacity-60"
      >
        Choose a file
      </button>
      <div className="mt-5 flex flex-wrap gap-2 text-xs text-ink/55">
        {['PDF', 'PNG', 'JPG', 'WEBP', 'TXT'].map((tag) => (
          <span key={tag} className="rounded-full border border-ink/10 bg-sand px-3 py-1">
            {tag}
          </span>
        ))}
      </div>
    </div>
  );
}
