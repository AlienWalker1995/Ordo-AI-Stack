import { useCallback, useRef, useState, useEffect } from "react";
import { useCaptionStoreContext } from "../../store/StoreContext";
import type { WordTiming } from "../../types";
import { SEGMENT_COLORS } from "../../constants";

interface WordBlockProps {
  word: WordTiming;
  index: number;
  minTime: number;
  pps: number;
  segmentIdx: number;
}

const TRACK_HEIGHT = 32;
const HANDLE_WIDTH = 6;

export default function WordBlock({
  word,
  index,
  minTime,
  pps,
  segmentIdx,
}: WordBlockProps) {
  const selectedWordIndex = useCaptionStoreContext((s) => s.selectedWordIndex);
  const moveWord = useCaptionStoreContext((s) => s.moveWord);
  const resizeWord = useCaptionStoreContext((s) => s.resizeWord);
  const selectWord = useCaptionStoreContext((s) => s.selectWord);
  const moveWordToTrack = useCaptionStoreContext((s) => s.moveWordToTrack);
  const trackCount = useCaptionStoreContext((s) => s.trackCount);
  const seekToTime = useCaptionStoreContext((s) => s.seekToTime);
  const editingWordIndex = useCaptionStoreContext((s) => s.editingWordIndex);
  const editingWordText = useCaptionStoreContext((s) => s.editingWordText);
  const startEditingWord = useCaptionStoreContext((s) => s.startEditingWord);
  const commitEditingWord = useCaptionStoreContext((s) => s.commitEditingWord);
  const cancelEditingWord = useCaptionStoreContext((s) => s.cancelEditingWord);
  const setEditingWordText = useCaptionStoreContext((s) => s.setEditingWordText);
  const deleteWord = useCaptionStoreContext((s) => s.deleteWord);
  const duplicateWord = useCaptionStoreContext((s) => s.duplicateWord);

  const [contextMenu, setContextMenu] = useState<{
    x: number;
    y: number;
  } | null>(null);

  const inputRef = useRef<HTMLInputElement>(null);
  const isEditing = editingWordIndex === index;

  const isSelected = selectedWordIndex === index;
  const isHidden = word.override_hidden || false;
  const color = SEGMENT_COLORS[segmentIdx % SEGMENT_COLORS.length];
  const displayText = word.override_text || word.word;

  const hasOverrides =
    word.override_color ||
    word.override_highlight ||
    word.override_scale ||
    word.override_font_size ||
    word.override_stroke_width ||
    word.override_stroke_color ||
    word.override_text ||
    word.override_hidden;

  const left = (word.start - minTime) * pps;
  const width = Math.max((word.end - word.start) * pps, 12);

  const origLeftRef = useRef(0);
  const origStartRef = useRef(0);
  const origWidthRef = useRef(0);
  const wasDraggedRef = useRef(false);
  const clickCountRef = useRef(0);
  const clickTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Focus input when editing starts
  useEffect(() => {
    if (isEditing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [isEditing]);

  const handleBlockMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (e.button === 2) return;
      if ((e.target as HTMLElement).dataset.resize) return;
      if (isEditing) return;

      e.preventDefault();
      e.stopPropagation();

      wasDraggedRef.current = false;
      origLeftRef.current = left;
      origStartRef.current = word.start;
      const startX = e.clientX;
      const duration = word.end - word.start;

      const onMove = (ev: MouseEvent) => {
        const dx = ev.clientX - startX;
        if (Math.abs(dx) > 3) wasDraggedRef.current = true;
        const newLeft = Math.max(0, origLeftRef.current + dx);
        const newStart = newLeft / pps + minTime;
        moveWord(index, newStart, newStart + duration);
      };

      const onUp = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        if (!wasDraggedRef.current) {
          // Handle single/double click
          clickCountRef.current++;
          if (clickCountRef.current === 1) {
            clickTimerRef.current = setTimeout(() => {
              // Single click: select + jump preview
              selectWord(index);
              seekToTime(word.start);
              clickCountRef.current = 0;
            }, 250);
          } else if (clickCountRef.current >= 2) {
            // Double click: inline edit
            if (clickTimerRef.current) clearTimeout(clickTimerRef.current);
            clickCountRef.current = 0;
            selectWord(index);
            startEditingWord(index);
          }
        }
      };

      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    },
    [left, word.start, word.end, pps, minTime, index, moveWord, selectWord, seekToTime, startEditingWord, isEditing]
  );

  const handleResizeMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      e.stopPropagation();

      origWidthRef.current = width;
      origStartRef.current = word.start;
      const startX = e.clientX;

      const onMove = (ev: MouseEvent) => {
        const dx = ev.clientX - startX;
        const newWidth = Math.max(12, origWidthRef.current + dx);
        const newEnd = origStartRef.current + newWidth / pps;
        resizeWord(index, newEnd);
      };

      const onUp = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
      };

      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    },
    [width, word.start, pps, index, resizeWord]
  );

  const handleContextMenu = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setContextMenu({ x: e.clientX, y: e.clientY });
  }, []);

  const handleTrackMove = useCallback(
    (track: number) => {
      moveWordToTrack(index, track);
      setContextMenu(null);
    },
    [index, moveWordToTrack]
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter") {
        commitEditingWord();
      } else if (e.key === "Escape") {
        cancelEditingWord();
      }
    },
    [commitEditingWord, cancelEditingWord]
  );

  return (
    <>
      <div
        className={`jcs:absolute jcs:rounded jcs:flex jcs:items-center jcs:justify-center jcs:overflow-hidden jcs:select-none jcs:transition-shadow jcs:cursor-grab jcs:hover:shadow-[0_0_0_2px_rgba(255,255,255,0.4)] ${
          isSelected ? "jcs:outline jcs:outline-2 jcs:outline-white" : ""
        } ${isHidden ? "jcs:opacity-40" : ""}`}
        style={{
          left,
          width,
          height: TRACK_HEIGHT - 8,
          top: 4,
          background: isHidden ? "#333" : color,
        }}
        onMouseDown={handleBlockMouseDown}
        onContextMenu={handleContextMenu}
        title={`"${displayText}" ${word.start.toFixed(3)}s \u2192 ${word.end.toFixed(3)}s`}
      >
        {isEditing ? (
          <input
            ref={inputRef}
            type="text"
            value={editingWordText}
            onChange={(e) => setEditingWordText(e.target.value)}
            onKeyDown={handleKeyDown}
            onBlur={commitEditingWord}
            className="jcs:bg-black/50 jcs:text-white jcs:text-[10px] jcs:font-semibold jcs:w-full jcs:h-full jcs:border-none jcs:outline-none jcs:text-center jcs:px-1"
            onClick={(e) => e.stopPropagation()}
            onMouseDown={(e) => e.stopPropagation()}
          />
        ) : (
          <span className="jcs:text-white jcs:text-[10px] jcs:font-semibold jcs:pointer-events-none jcs:whitespace-nowrap jcs:text-ellipsis jcs:overflow-hidden jcs:px-1 jcs:pr-2">
            {displayText}
          </span>
        )}

        {hasOverrides && !isEditing && (
          <div className="jcs:absolute jcs:top-0.5 jcs:right-2 jcs:w-1.5 jcs:h-1.5 jcs:rounded-full jcs:bg-warning" />
        )}

        {!isEditing && (
          <div
            className="jcs:absolute jcs:right-0 jcs:top-0 jcs:h-full jcs:cursor-ew-resize jcs:bg-white/15 jcs:rounded-r"
            data-resize="true"
            style={{ width: HANDLE_WIDTH }}
            onMouseDown={handleResizeMouseDown}
          />
        )}
      </div>

      {contextMenu && (
        <>
          <div
            className="jcs:fixed jcs:inset-0 jcs:z-[9999]"
            onClick={() => setContextMenu(null)}
          />
          <div
            className="jcs:fixed jcs:z-[10000] jcs:bg-surface-raised jcs:border jcs:border-border-light jcs:rounded jcs:py-1 jcs:min-w-[120px] jcs:shadow-lg"
            style={{ left: contextMenu.x, top: contextMenu.y }}
          >
            <div className="jcs:px-3 jcs:py-1 jcs:text-text-muted jcs:text-[9px] jcs:border-b jcs:border-border">
              "{displayText}"
            </div>
            <div
              className="jcs:px-3 jcs:py-1 jcs:text-[11px] jcs:cursor-pointer jcs:text-text jcs:hover:bg-[#3a3a5e]"
              onClick={() => {
                duplicateWord(index);
                setContextMenu(null);
              }}
            >
              Duplicate
            </div>
            <div
              className="jcs:px-3 jcs:py-1 jcs:text-[11px] jcs:cursor-pointer jcs:text-red-400 jcs:hover:bg-[#5e3a3a]"
              onClick={() => {
                deleteWord(index);
                setContextMenu(null);
              }}
            >
              Delete
            </div>
            <div className="jcs:border-t jcs:border-border jcs:my-1" />
            <div className="jcs:px-3 jcs:py-0.5 jcs:text-text-muted jcs:text-[9px]">
              Move to:
            </div>
            {Array.from({ length: trackCount }, (_, t) => {
              const isCurrent = t === (word.track || 0);
              return (
                <div
                  key={t}
                  className={`jcs:px-3 jcs:py-1 jcs:text-[11px] jcs:cursor-pointer jcs:transition-colors ${
                    isCurrent
                      ? "jcs:text-text-dim jcs:italic jcs:cursor-default"
                      : "jcs:text-text jcs:hover:bg-[#3a3a5e]"
                  }`}
                  onClick={() => !isCurrent && handleTrackMove(t)}
                >
                  Track {t + 1}
                  {isCurrent ? " (current)" : ""}
                </div>
              );
            })}
          </div>
        </>
      )}
    </>
  );
}
