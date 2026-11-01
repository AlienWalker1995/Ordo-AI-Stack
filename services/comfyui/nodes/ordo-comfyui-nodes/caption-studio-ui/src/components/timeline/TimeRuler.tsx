import { useCallback } from "react";
import { formatTime, computeTickInterval } from "../../utils/time";
import { useCaptionStoreContext } from "../../store/StoreContext";

interface TimeRulerProps {
  minTime: number;
  maxTime: number;
  pps: number;
}

const RULER_HEIGHT = 28;

export default function TimeRuler({ minTime, maxTime, pps }: TimeRulerProps) {
  const seekToTime = useCaptionStoreContext((s) => s.seekToTime);

  const tickInterval = computeTickInterval(maxTime - minTime);
  const ticks: { x: number; label: string }[] = [];

  for (
    let t = Math.ceil(minTime / tickInterval) * tickInterval;
    t <= maxTime;
    t += tickInterval
  ) {
    ticks.push({
      x: (t - minTime) * pps,
      label: formatTime(t),
    });
  }

  const handleClick = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      const rect = e.currentTarget.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const time = x / pps + minTime;
      seekToTime(Math.max(minTime, Math.min(maxTime, time)));
    },
    [pps, minTime, maxTime, seekToTime]
  );

  return (
    <div
      className="jcs:relative jcs:border-b jcs:border-border jcs:mb-1 jcs:cursor-pointer"
      style={{ height: RULER_HEIGHT }}
      onClick={handleClick}
    >
      {ticks.map((tick, i) => (
        <div key={i}>
          <div
            className="jcs:absolute jcs:w-px jcs:bg-border-light"
            style={{ left: tick.x, top: 14, height: 12 }}
          />
          <div
            className="jcs:absolute jcs:top-0 jcs:-translate-x-1/2 jcs:text-[9px] jcs:text-text-muted jcs:whitespace-nowrap"
            style={{ left: tick.x }}
          >
            {tick.label}
          </div>
        </div>
      ))}
    </div>
  );
}
