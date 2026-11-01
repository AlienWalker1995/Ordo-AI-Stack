import { useCallback, useRef } from "react";

interface UseTimelineZoomOpts {
  zoomLevel: number;
  setZoomLevel: (z: number) => void;
  scrollLeft: number;
  setScrollLeft: (s: number) => void;
  basePps: number;
}

export function useTimelineZoom({
  zoomLevel,
  setZoomLevel,
  setScrollLeft,
  basePps,
}: UseTimelineZoomOpts) {
  const scrollRef = useRef<HTMLDivElement>(null);

  const handleWheel = useCallback(
    (e: React.WheelEvent<HTMLDivElement>) => {
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      e.stopPropagation();

      const el = scrollRef.current;
      if (!el) return;

      const rect = el.getBoundingClientRect();
      const mouseX = e.clientX - rect.left + el.scrollLeft;
      const pps = basePps * zoomLevel;
      const mouseTime = mouseX / pps;

      let newZoom: number;
      if (e.deltaY < 0) {
        newZoom = Math.min(10.0, zoomLevel * 1.1);
      } else {
        newZoom = Math.max(0.5, zoomLevel / 1.1);
      }

      const newPps = basePps * newZoom;
      const newScroll = Math.max(0, mouseTime * newPps - (e.clientX - rect.left));

      setZoomLevel(newZoom);
      setScrollLeft(newScroll);
    },
    [zoomLevel, basePps, setZoomLevel, setScrollLeft]
  );

  return { scrollRef, handleWheel };
}
