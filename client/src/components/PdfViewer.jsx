import { useEffect, useRef, useState } from 'react';
import { GlobalWorkerOptions, getDocument } from 'pdfjs-dist';
import pdfWorkerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';

GlobalWorkerOptions.workerSrc = pdfWorkerUrl;

export default function PdfViewer({ url, label }) {
  const containerRef = useRef(null);
  const canvasRef = useRef(null);
  const pdfRef = useRef(null);
  const renderTaskRef = useRef(null);
  const [pageNumber, setPageNumber] = useState(1);
  const [numPages, setNumPages] = useState(0);
  const [containerWidth, setContainerWidth] = useState(0);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState('');

  useEffect(() => {
    setPageNumber(1);
  }, [url]);

  useEffect(() => {
    if (!containerRef.current) {
      return undefined;
    }

    const updateWidth = () => {
      const width = containerRef.current?.clientWidth || 0;
      if (width > 0) {
        setContainerWidth(width);
      }
    };

    updateWidth();

    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', updateWidth);
      return () => window.removeEventListener('resize', updateWidth);
    }

    const observer = new ResizeObserver(updateWidth);
    observer.observe(containerRef.current);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    let cancelled = false;
    const loadingTask = getDocument(url);

    setIsLoading(true);
    setLoadError('');
    setNumPages(0);

    loadingTask.promise
      .then((pdf) => {
        if (cancelled) {
          void pdf.destroy();
          return;
        }
        pdfRef.current = pdf;
        setNumPages(pdf.numPages);
        setPageNumber((current) => Math.min(Math.max(current, 1), pdf.numPages || 1));
      })
      .catch((error) => {
        if (cancelled) {
          return;
        }
        console.error(error);
        setLoadError('Could not load this PDF in the booth viewer.');
        setIsLoading(false);
      });

    return () => {
      cancelled = true;
      loadingTask.destroy();
      const currentPdf = pdfRef.current;
      pdfRef.current = null;
      if (currentPdf) {
        void currentPdf.destroy();
      }
    };
  }, [url]);

  useEffect(() => {
    const pdf = pdfRef.current;
    const canvas = canvasRef.current;
    if (!pdf || !canvas || containerWidth === 0 || pageNumber < 1) {
      return undefined;
    }

    let cancelled = false;

    const renderPage = async () => {
      try {
        setIsLoading(true);
        const page = await pdf.getPage(pageNumber);
        if (cancelled) {
          return;
        }

        const unscaledViewport = page.getViewport({ scale: 1 });
        const scale = containerWidth / unscaledViewport.width;
        const viewport = page.getViewport({ scale });
        const context = canvas.getContext('2d');
        if (!context) {
          setLoadError('Could not draw this PDF page.');
          setIsLoading(false);
          return;
        }

        const outputScale = window.devicePixelRatio || 1;
        canvas.width = Math.floor(viewport.width * outputScale);
        canvas.height = Math.floor(viewport.height * outputScale);
        canvas.style.width = `${viewport.width}px`;
        canvas.style.height = `${viewport.height}px`;

        context.setTransform(outputScale, 0, 0, outputScale, 0, 0);

        renderTaskRef.current?.cancel();
        renderTaskRef.current = page.render({ canvasContext: context, viewport });
        await renderTaskRef.current.promise;
        if (!cancelled) {
          setIsLoading(false);
        }
      } catch (error) {
        if (cancelled || error?.name === 'RenderingCancelledException') {
          return;
        }
        console.error(error);
        setLoadError('Could not render this PDF page.');
        setIsLoading(false);
      }
    };

    void renderPage();

    return () => {
      cancelled = true;
      renderTaskRef.current?.cancel();
    };
  }, [containerWidth, numPages, pageNumber, url]);

  const showNavigation = numPages > 1 && !loadError;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3 text-sm text-ink/65">
        <p className="font-semibold text-ink">{label}</p>
        {showNavigation ? (
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setPageNumber((current) => Math.max(1, current - 1))}
              disabled={pageNumber <= 1}
              className="rounded-full border border-ink/10 bg-white px-3 py-1.5 font-semibold text-ink transition hover:border-orange hover:text-orange disabled:cursor-not-allowed disabled:opacity-50"
            >
              Prev
            </button>
            <span>
              Page {pageNumber} of {numPages}
            </span>
            <button
              type="button"
              onClick={() => setPageNumber((current) => Math.min(numPages, current + 1))}
              disabled={pageNumber >= numPages}
              className="rounded-full border border-ink/10 bg-white px-3 py-1.5 font-semibold text-ink transition hover:border-orange hover:text-orange disabled:cursor-not-allowed disabled:opacity-50"
            >
              Next
            </button>
          </div>
        ) : null}
      </div>

      <div
        ref={containerRef}
        className="overflow-hidden rounded-[1.5rem] border border-ink/10 bg-white p-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.35)]"
      >
        {loadError ? (
          <p className="rounded-[1rem] bg-paper px-4 py-6 text-sm leading-6 text-[#9c3f20]">{loadError}</p>
        ) : (
          <div className="relative min-h-[18rem] rounded-[1rem] bg-paper/80 p-2">
            {isLoading ? (
              <div className="absolute inset-0 flex items-center justify-center rounded-[1rem] bg-paper/85 text-sm font-semibold text-ink/60">
                Loading PDF…
              </div>
            ) : null}
            <canvas ref={canvasRef} className="mx-auto block max-w-full rounded-[0.9rem]" />
          </div>
        )}
      </div>
    </div>
  );
}
