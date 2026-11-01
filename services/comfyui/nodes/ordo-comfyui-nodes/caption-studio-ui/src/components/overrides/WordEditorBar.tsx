import { useCaptionStoreContext } from "../../store/StoreContext";

export default function WordEditorBar() {
  const selectedWordIndex = useCaptionStoreContext((s) => s.selectedWordIndex);
  const timings = useCaptionStoreContext((s) => s.editedTimings);
  const setWordOverride = useCaptionStoreContext((s) => s.setWordOverride);
  const clearWordOverrides = useCaptionStoreContext((s) => s.clearWordOverrides);
  const selectWord = useCaptionStoreContext((s) => s.selectWord);

  if (selectedWordIndex === null || !timings[selectedWordIndex]) {
    return null;
  }

  const word = timings[selectedWordIndex];
  const idx = selectedWordIndex;

  const labelClass = "jcs:text-text-muted jcs:text-[9px] jcs:whitespace-nowrap";

  return (
    <div className="jcs:px-2 jcs:py-1.5 jcs:border-t jcs:border-border jcs:bg-panel">
      {/* Row 1: Word info + Text + Fill + Scale */}
      <div className="jcs:flex jcs:items-center jcs:gap-2 jcs:flex-wrap">
        <span className="jcs:text-[11px] jcs:font-semibold jcs:text-text jcs:whitespace-nowrap">
          "{word.word}"
          <span className="jcs:font-normal jcs:text-text-muted jcs:ml-1 jcs:text-[9px]">
            ({word.start.toFixed(3)}–{word.end.toFixed(3)}s)
          </span>
        </span>

        <div className="jcs:flex jcs:items-center jcs:gap-1">
          <span className={labelClass}>Text:</span>
          <input
            type="text"
            className="jcs:text-[10px] jcs:w-[72px]"
            value={word.override_text || ""}
            placeholder={word.word}
            onChange={(e) =>
              setWordOverride(idx, "override_text", e.target.value || undefined)
            }
          />
        </div>

        <div className="jcs:flex jcs:items-center jcs:gap-1">
          <span className={labelClass}>Fill:</span>
          <input
            type="color"
            value={word.override_color || "#FFFFFF"}
            onChange={(e) =>
              setWordOverride(
                idx,
                "override_color",
                e.target.value === "#ffffff" || e.target.value === "#FFFFFF" ? undefined : e.target.value
              )
            }
            className="jcs:w-5 jcs:h-5"
          />
        </div>

        <div className="jcs:flex jcs:items-center jcs:gap-1">
          <span className={labelClass}>Scale:</span>
          <input
            type="number"
            className="jcs:text-[10px] jcs:w-[40px]"
            value={word.override_scale ?? 1.0}
            min={0.5}
            max={3.0}
            step={0.1}
            onChange={(e) => {
              const v = parseFloat(e.target.value);
              setWordOverride(idx, "override_scale", v === 1.0 ? undefined : v);
            }}
          />
        </div>
      </div>

      {/* Row 2: Highlight, Stroke, Hidden, actions */}
      <div className="jcs:flex jcs:items-center jcs:gap-2 jcs:mt-1 jcs:flex-wrap">
        <div className="jcs:flex jcs:items-center jcs:gap-1">
          <span className={labelClass}>Highlight:</span>
          <input
            type="color"
            value={word.override_highlight || "#00FFFF"}
            onChange={(e) =>
              setWordOverride(
                idx,
                "override_highlight",
                e.target.value === "#00ffff" || e.target.value === "#00FFFF" ? undefined : e.target.value
              )
            }
            className="jcs:w-5 jcs:h-5"
          />
        </div>

        <div className="jcs:flex jcs:items-center jcs:gap-1">
          <span className={labelClass}>Stroke:</span>
          <input
            type="color"
            value={word.override_stroke_color || "#000000"}
            onChange={(e) =>
              setWordOverride(
                idx,
                "override_stroke_color",
                e.target.value === "#000000" ? undefined : e.target.value
              )
            }
            className="jcs:w-5 jcs:h-5"
          />
          <span className="jcs:text-text-dim jcs:text-[9px]">/</span>
          <input
            type="number"
            className="jcs:text-[10px] jcs:w-[36px]"
            value={word.override_stroke_width ?? ""}
            min={0}
            max={30}
            step={1}
            placeholder="—"
            onChange={(e) =>
              setWordOverride(idx, "override_stroke_width", e.target.value === "" ? undefined : parseFloat(e.target.value))
            }
          />
        </div>

        <div className="jcs:flex jcs:items-center jcs:gap-1">
          <span className={labelClass}>Hidden:</span>
          <input
            type="checkbox"
            checked={word.override_hidden || false}
            onChange={(e) =>
              setWordOverride(idx, "override_hidden", e.target.checked || undefined)
            }
          />
        </div>

        <div className="jcs:flex-1" />

        <button className="jcs:text-[10px]" onClick={() => clearWordOverrides(idx)}>
          Clear
        </button>
        <button className="jcs:text-[10px]" onClick={() => selectWord(null)}>
          Deselect
        </button>
      </div>
    </div>
  );
}
