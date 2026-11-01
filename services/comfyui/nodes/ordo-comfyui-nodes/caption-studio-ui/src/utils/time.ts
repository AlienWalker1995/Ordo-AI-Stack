/** Format seconds as "1.2s" or "1:02.3". */
export function formatTime(seconds: number): string {
  if (seconds < 60) return seconds.toFixed(1) + "s";
  const m = Math.floor(seconds / 60);
  const s = (seconds % 60).toFixed(1);
  return `${m}:${s.padStart(4, "0")}`;
}

/** Format seconds as timecode "MM:SS.F" (e.g. "01.2s" or "1:02.3"). */
export function formatTimecode(seconds: number): string {
  const s = Math.max(0, seconds);
  if (s < 60) return s.toFixed(1) + "s";
  const m = Math.floor(s / 60);
  const rem = (s % 60).toFixed(1);
  return `${m}:${rem.padStart(4, "0")}`;
}

/** Compute tick interval for time ruler based on total duration. */
export function computeTickInterval(duration: number): number {
  if (duration <= 2) return 0.1;
  if (duration <= 5) return 0.25;
  if (duration <= 15) return 0.5;
  if (duration <= 60) return 1;
  if (duration <= 300) return 5;
  return 10;
}
