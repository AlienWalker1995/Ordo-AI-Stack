import type { WordTiming } from "../../types";
import { TRACK_COLORS, SEGMENT_COLORS } from "../../constants";
import WordBlock from "./WordBlock";

interface TrackLaneProps {
  trackId: number;
  segments: Map<number, { words: WordTiming[]; indices: number[] }>;
  minTime: number;
  pps: number;
  contentWidth: number;
}

const TRACK_HEIGHT = 32;

export default function TrackLane({
  trackId,
  segments,
  minTime,
  pps,
  contentWidth,
}: TrackLaneProps) {
  const trackColor = TRACK_COLORS[trackId % TRACK_COLORS.length];
  const segKeys = [...segments.keys()].sort((a, b) => a - b);

  return (
    <div className="jcs:mb-1">
      {/* Track header */}
      <div
        className="jcs:flex jcs:items-center jcs:pl-1.5 jcs:text-[9px] jcs:font-semibold jcs:h-5 jcs:border-l-[3px]"
        style={{ borderLeftColor: trackColor, color: trackColor }}
      >
        Track {trackId + 1}
      </div>

      {segKeys.length === 0 ? (
        <div
          className="jcs:border-b jcs:border-border/50 jcs:mb-1"
          style={{ height: TRACK_HEIGHT, width: contentWidth }}
        />
      ) : (
        segKeys.map((segIdx) => {
          const seg = segments.get(segIdx)!;
          const color = SEGMENT_COLORS[segIdx % SEGMENT_COLORS.length];
          return (
            <div
              key={segIdx}
              className="jcs:relative jcs:mb-1"
              style={{ height: TRACK_HEIGHT, width: contentWidth }}
            >
              <div
                className="jcs:absolute jcs:-left-0.5 jcs:top-0 jcs:text-[8px] jcs:opacity-60"
                style={{ color }}
              >
                {segIdx >= 0 ? `seg ${segIdx}` : "ungrouped"}
              </div>
              {seg.words.map((w, wi) => (
                <WordBlock
                  key={seg.indices[wi]}
                  word={w}
                  index={seg.indices[wi]}
                  minTime={minTime}
                  pps={pps}
                  segmentIdx={segIdx}
                />
              ))}
            </div>
          );
        })
      )}
    </div>
  );
}
