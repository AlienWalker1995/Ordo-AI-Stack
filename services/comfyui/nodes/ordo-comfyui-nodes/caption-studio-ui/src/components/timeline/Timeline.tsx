import { useCallback, useEffect, useMemo, useRef } from "react";
import { useCaptionStoreContext } from "../../store/StoreContext";
import TimelineToolbar from "./TimelineToolbar";
import TimeRuler from "./TimeRuler";
import TrackLane from "./TrackLane";
import Playhead from "./Playhead";
import type { WordTiming } from "../../types";

const PADDING = 8;
const RULER_HEIGHT = 28;
const TRACK_HEADER_HEIGHT = 20;
const TRACK_HEIGHT = 32;

export default function Timeline() {
  const timings = useCaptionStoreContext((s) => s.editedTimings);
  const zoomLevel = useCaptionStoreContext((s) => s.zoomLevel);
  const scrollLeft = useCaptionStoreContext((s) => s.scrollLeft);
  const setScrollLeft = useCaptionStoreContext((s) => s.setScrollLeft);
  const setZoomLevel = useCaptionStoreContext((s) => s.setZoomLevel);
  const trackCount = useCaptionStoreContext((s) => s.trackCount);
  const addTrack = useCaptionStoreContext((s) => s.addTrack);
  const resetEdits = useCaptionStoreContext((s) => s.resetEdits);
  const previewTime = useCaptionStoreContext((s) => s.previewTime);
  const selectedWordIndex = useCaptionStoreContext((s) => s.selectedWordIndex);
  const editingWordIndex = useCaptionStoreContext((s) => s.editingWordIndex);
  const deleteWord = useCaptionStoreContext((s) => s.deleteWord);
  const addWord = useCaptionStoreContext((s) => s.addWord);

  const scrollRef = useRef<HTMLDivElement>(null);

  const { minTime, maxTime, basePps, pps, contentWidth, trackMap } =
    useMemo(() => {
      if (timings.length === 0) {
        return {
          minTime: 0,
          maxTime: 0,
          totalDuration: 0,
          basePps: 80,
          pps: 80,
          contentWidth: 380,
          trackMap: new Map<
            number,
            Map<number, { words: WordTiming[]; indices: number[] }>
          >(),
        };
      }

      const minT = Math.min(...timings.map((w) => w.start));
      const maxT = Math.max(...timings.map((w) => w.end));
      const dur = maxT - minT;
      const base = Math.max(80, 380 / (dur || 1));
      const pixelsPerSec = base * zoomLevel;
      const cw = Math.max(380, pixelsPerSec * (dur || 1));

      const tm = new Map<
        number,
        Map<number, { words: WordTiming[]; indices: number[] }>
      >();
      timings.forEach((w, i) => {
        const track = w.track || 0;
        const segIdx = w.segment_idx ?? -1;
        if (!tm.has(track)) tm.set(track, new Map());
        const segMap = tm.get(track)!;
        if (!segMap.has(segIdx)) segMap.set(segIdx, { words: [], indices: [] });
        const seg = segMap.get(segIdx)!;
        seg.words.push(w);
        seg.indices.push(i);
      });

      for (let t = 0; t < trackCount; t++) {
        if (!tm.has(t)) tm.set(t, new Map());
      }

      return {
        minTime: minT,
        maxTime: maxT,
        totalDuration: dur,
        basePps: base,
        pps: pixelsPerSec,
        contentWidth: cw,
        trackMap: tm,
      };
    }, [timings, zoomLevel, trackCount]);

  const trackKeys = useMemo(
    () => [...trackMap.keys()].sort((a, b) => a - b),
    [trackMap]
  );

  // Compute total content height for playhead
  const contentHeight = useMemo(() => {
    let h = RULER_HEIGHT;
    for (const trackId of trackKeys) {
      const segCount = trackMap.get(trackId)!.size;
      h += TRACK_HEADER_HEIGHT; // Track header
      h += Math.max(1, segCount) * (TRACK_HEIGHT + 4); // Segments
    }
    return h + 40; // Extra for add track button
  }, [trackKeys, trackMap]);

  // Delete key removes selected word (when not inline-editing)
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Delete" || e.key === "Backspace") {
        if (selectedWordIndex !== null && editingWordIndex === null) {
          e.preventDefault();
          deleteWord(selectedWordIndex);
        }
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [selectedWordIndex, editingWordIndex, deleteWord]);

  // Ctrl+wheel zoom
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;

    const handler = (e: WheelEvent) => {
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      e.stopPropagation();

      const rect = el.getBoundingClientRect();
      const mouseX = e.clientX - rect.left + el.scrollLeft;
      const mouseTime = mouseX / pps;

      let newZoom: number;
      if (e.deltaY < 0) {
        newZoom = Math.min(10.0, zoomLevel * 1.1);
      } else {
        newZoom = Math.max(0.5, zoomLevel / 1.1);
      }

      const newPps = basePps * newZoom;
      const newScroll = Math.max(
        0,
        mouseTime * newPps - (e.clientX - rect.left)
      );

      setZoomLevel(newZoom);
      setScrollLeft(newScroll);
    };

    el.addEventListener("wheel", handler, { passive: false });
    return () => el.removeEventListener("wheel", handler);
  }, [pps, basePps, zoomLevel, setZoomLevel, setScrollLeft]);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollLeft = scrollLeft;
    }
  }, [scrollLeft]);

  const handleScroll = () => {
    if (scrollRef.current) {
      setScrollLeft(scrollRef.current.scrollLeft);
    }
  };

  if (timings.length === 0) {
    return (
      <div className="jcs:p-3 jcs:text-text-dim jcs:text-center">
        Queue the workflow to populate the timeline.
      </div>
    );
  }

  return (
    <div className="jcs:border-b jcs:border-border">
      <TimelineToolbar />
      <div
        className="jcs:overflow-x-auto jcs:overflow-y-hidden jcs:w-full jcs:relative"
        ref={scrollRef}
        onScroll={handleScroll}
        style={{ padding: PADDING }}
      >
        <div
          className="jcs:relative"
          style={{ width: contentWidth, minHeight: 100 }}
        >
          {/* Playhead */}
          <Playhead
            time={previewTime}
            minTime={minTime}
            pps={pps}
            height={contentHeight}
          />

          <TimeRuler minTime={minTime} maxTime={maxTime} pps={pps} />
          {trackKeys.map((trackId) => (
            <TrackLane
              key={trackId}
              trackId={trackId}
              segments={trackMap.get(trackId)!}
              minTime={minTime}
              pps={pps}
              contentWidth={contentWidth}
            />
          ))}
          <button
            className="jcs:mt-1 jcs:text-[10px]"
            onClick={addTrack}
          >
            + Add Track
          </button>
        </div>
      </div>
      <div className="jcs:px-2 jcs:py-1 jcs:flex jcs:gap-2 jcs:items-center jcs:border-t jcs:border-border">
        <button className="jcs:text-[10px]" onClick={() => addWord(previewTime)}>
          + Add Word
        </button>
        <div className="jcs:flex-1" />
        <button className="jcs:text-[10px]" onClick={resetEdits}>
          Reset Edits
        </button>
      </div>
    </div>
  );
}
