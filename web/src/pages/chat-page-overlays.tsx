/** Non-interactive status overlay while a resumed session's PTY reattaches. */
export function ResumeLoadingOverlay({ text }: { text: string }) {
  return (
    <div
      className="pointer-events-none absolute inset-0 z-20 flex items-center justify-center"
      role="status"
      aria-live="polite"
      aria-label={text}
    >
      <div className="max-w-[min(28rem,calc(100vw-3rem))] border border-current/30 bg-black/80 px-4 py-3 text-center text-xs tracking-wide text-white/85 shadow-lg">
        {text}
      </div>
    </div>
  );
}
