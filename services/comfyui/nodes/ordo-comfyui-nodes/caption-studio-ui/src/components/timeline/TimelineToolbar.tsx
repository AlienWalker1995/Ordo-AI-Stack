import { useCaptionStoreContext } from "../../store/StoreContext";

export default function TimelineToolbar() {
  const zoomLevel = useCaptionStoreContext((s) => s.zoomLevel);
  const setZoomLevel = useCaptionStoreContext((s) => s.setZoomLevel);
  const setScrollLeft = useCaptionStoreContext((s) => s.setScrollLeft);
  const timings = useCaptionStoreContext((s) => s.editedTimings);

  const wordCount = timings.length;
  const segCount = new Set(timings.map((w) => w.segment_idx ?? -1)).size;
  const minTime = timings.length > 0 ? Math.min(...timings.map((w) => w.start)) : 0;
  const maxTime = timings.length > 0 ? Math.max(...timings.map((w) => w.end)) : 0;
  const duration = maxTime - minTime;

  return (
    <div className="jcs:flex jcs:items-center jcs:gap-1.5 jcs:px-2 jcs:py-1 jcs:border-b jcs:border-border jcs:bg-panel">
      <span className="jcs:text-text-muted jcs:text-[10px] jcs:mr-0.5">
        Zoom:
      </span>
      <button
        className="jcs:w-6 jcs:h-5 jcs:p-0 jcs:text-sm jcs:leading-[18px] jcs:text-center"
        onClick={() => setZoomLevel(zoomLevel / 1.2)}
      >
        −
      </button>
      <span className="jcs:text-text-muted jcs:text-[10px] jcs:min-w-[36px] jcs:text-center jcs:tabular-nums">
        {Math.round(zoomLevel * 100)}%
      </span>
      <button
        className="jcs:w-6 jcs:h-5 jcs:p-0 jcs:text-sm jcs:leading-[18px] jcs:text-center"
        onClick={() => setZoomLevel(zoomLevel * 1.2)}
      >
        +
      </button>
      <button
        className="jcs:text-[9px] jcs:px-1.5 jcs:py-0.5"
        onClick={() => {
          setZoomLevel(1.0);
          setScrollLeft(0);
        }}
      >
        Fit
      </button>
      <div className="jcs:flex-1" />
      <span className="jcs:text-text-dim jcs:text-[10px] jcs:tabular-nums">
        {wordCount} words · {segCount} segs · {duration.toFixed(1)}s
      </span>
    </div>
  );
}
