import { useEffect, useRef, useMemo, useCallback } from "react";
import { useCaptionStoreContext } from "../../store/StoreContext";
import { formatTimecode } from "../../utils/time";

export default function TransportControls() {
  const previewTime = useCaptionStoreContext((s) => s.previewTime);
  const setPreviewTime = useCaptionStoreContext((s) => s.setPreviewTime);
  const isPlaying = useCaptionStoreContext((s) => s.isPlaying);
  const togglePlay = useCaptionStoreContext((s) => s.togglePlay);
  const setIsPlaying = useCaptionStoreContext((s) => s.setIsPlaying);
  const stepFrame = useCaptionStoreContext((s) => s.stepFrame);
  const duration = useCaptionStoreContext((s) => s.duration);
  const timings = useCaptionStoreContext((s) => s.editedTimings);
  const audioDataUrl = useCaptionStoreContext((s) => s.audioDataUrl);

  const animRef = useRef<number>(0);
  const lastTimeRef = useRef<number>(0);
  const previewTimeRef = useRef(previewTime);
  const audioRef = useRef<HTMLAudioElement>(null);

  const hasAudio = !!audioDataUrl;

  useEffect(() => {
    previewTimeRef.current = previewTime;
  }, [previewTime]);

  // Load audio source when URL changes
  useEffect(() => {
    if (audioRef.current) {
      if (audioDataUrl) {
        audioRef.current.src = audioDataUrl;
        audioRef.current.load();
      } else {
        audioRef.current.removeAttribute("src");
      }
    }
  }, [audioDataUrl]);

  // Seek audio when previewTime changes externally (scrub, step, click)
  const seekAudio = useCallback((time: number) => {
    if (audioRef.current && hasAudio) {
      audioRef.current.currentTime = time;
    }
  }, [hasAudio]);

  useEffect(() => {
    if (!isPlaying) {
      // Paused — stop everything
      if (animRef.current) cancelAnimationFrame(animRef.current);
      if (audioRef.current && hasAudio) {
        audioRef.current.pause();
      }
      return;
    }

    if (hasAudio) {
      // Audio-driven playback
      const audio = audioRef.current!;
      audio.currentTime = previewTimeRef.current;
      audio.play().catch(() => {});

      const animate = () => {
        const t = audio.currentTime;
        if (t >= maxTime) {
          setPreviewTime(0);
          setIsPlaying(false);
          audio.pause();
          return;
        }
        setPreviewTime(t);
        animRef.current = requestAnimationFrame(animate);
      };

      animRef.current = requestAnimationFrame(animate);
      return () => {
        if (animRef.current) cancelAnimationFrame(animRef.current);
        audio.pause();
      };
    } else {
      // No audio — existing rAF dt-based loop
      lastTimeRef.current = performance.now();

      const animate = (now: number) => {
        const dt = (now - lastTimeRef.current) / 1000;
        lastTimeRef.current = now;

        const newTime = previewTimeRef.current + dt;
        if (newTime > maxTime) {
          setPreviewTime(0);
          setIsPlaying(false);
          return;
        }
        setPreviewTime(newTime);
        animRef.current = requestAnimationFrame(animate);
      };

      animRef.current = requestAnimationFrame(animate);
      return () => {
        if (animRef.current) cancelAnimationFrame(animRef.current);
      };
    }
  }, [isPlaying, hasAudio]);

  const maxTime = useMemo(() => {
    if (duration > 0) return duration;
    if (timings.length > 0) return Math.max(...timings.map((w) => w.end));
    return 0;
  }, [duration, timings]);

  // Compute fill percentage for the CSS variable trick
  const scrubPct = maxTime > 0 ? (previewTime / maxTime) * 100 : 0;

  const handleScrub = (e: React.ChangeEvent<HTMLInputElement>) => {
    const t = parseFloat(e.target.value);
    setPreviewTime(t);
    seekAudio(t);
  };

  const handleJumpToStart = () => {
    setPreviewTime(0);
    seekAudio(0);
  };

  const handleJumpToEnd = () => {
    setPreviewTime(maxTime);
    seekAudio(maxTime);
  };

  const handleStepBack = () => {
    stepFrame(-1);
    // stepFrame updates previewTime synchronously; read from store after
    requestAnimationFrame(() => seekAudio(previewTimeRef.current));
  };

  const handleStepForward = () => {
    stepFrame(1);
    requestAnimationFrame(() => seekAudio(previewTimeRef.current));
  };

  return (
    <div className="jcs:flex jcs:items-center jcs:gap-1.5 jcs:w-full jcs:px-2">
      {/* Hidden audio element for synced playback */}
      <audio ref={audioRef} preload="auto" style={{ display: "none" }} />

      {/* Skip to start */}
      <button
        className="jcs-transport-btn"
        onClick={handleJumpToStart}
        title="Jump to start"
      >
        {"\u23EE"}
      </button>

      {/* Step back 1 frame */}
      <button
        className="jcs-transport-btn"
        onClick={handleStepBack}
        title="Step back 1 frame"
      >
        {"\u23F4"}
      </button>

      {/* Play / Pause — accent colored */}
      <button
        className="jcs-transport-btn jcs-play-btn"
        onClick={togglePlay}
        title={isPlaying ? "Pause" : "Play"}
      >
        {isPlaying ? "\u23F8" : "\u25B6"}
      </button>

      {/* Step forward 1 frame */}
      <button
        className="jcs-transport-btn"
        onClick={handleStepForward}
        title="Step forward 1 frame"
      >
        {"\u23F5"}
      </button>

      {/* Skip to end */}
      <button
        className="jcs-transport-btn"
        onClick={handleJumpToEnd}
        title="Jump to end"
      >
        {"\u23ED"}
      </button>

      {/* Scrubber — uses jcs-scrubber class for wider track + accent fill */}
      <input
        type="range"
        className="jcs-scrubber jcs:flex-1"
        min={0}
        max={maxTime || 1}
        step={0.001}
        value={previewTime}
        onChange={handleScrub}
        style={{ "--jcs-scrub-pct": `${scrubPct}%` } as React.CSSProperties}
      />

      {/* Timecode */}
      <span className="jcs:text-text-muted jcs:text-[10px] jcs:min-w-[80px] jcs:text-right jcs:tabular-nums jcs:whitespace-nowrap">
        {formatTimecode(previewTime)} / {formatTimecode(maxTime)}
      </span>
    </div>
  );
}
