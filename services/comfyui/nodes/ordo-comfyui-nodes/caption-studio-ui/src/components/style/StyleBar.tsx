import { useCaptionStoreContext } from "../../store/StoreContext";
import { PRESET_NAMES, STYLE_PRESETS } from "../../constants";

export default function StyleBar() {
  const style = useCaptionStoreContext((s) => s.style);
  const fontList = useCaptionStoreContext((s) => s.fontList);
  const updateStyle = useCaptionStoreContext((s) => s.updateStyle);
  const presetName = useCaptionStoreContext((s) => s.presetName);
  const setPreset = useCaptionStoreContext((s) => s.setPreset);
  const styleExpanded = useCaptionStoreContext((s) => s.styleExpanded);
  const toggleStyleExpanded = useCaptionStoreContext((s) => s.toggleStyleExpanded);

  const fontDisplay = (path: string) => {
    const parts = path.replace(/\\/g, "/").split("/");
    return parts[parts.length - 1];
  };

  const handlePresetChange = (name: string) => {
    setPreset(name);
    if (name !== "Custom" && STYLE_PRESETS[name]) {
      updateStyle({ ...STYLE_PRESETS[name] });
      setPreset(name);
    }
  };

  return (
    <div className="jcs:px-2 jcs:py-1.5 jcs:border-b jcs:border-border jcs:bg-panel jcs:flex jcs:flex-col jcs:gap-1">
      {/* Row 1: Preset, Font, Size */}
      <div className="jcs:flex jcs:items-center jcs:gap-2 jcs:flex-wrap">
        <div className="jcs:flex jcs:items-center jcs:gap-1">
          <span className="jcs:text-text-muted jcs:text-[9px]">Preset:</span>
          <select
            className="jcs:text-[10px] jcs:w-[90px]"
            value={presetName}
            onChange={(e) => handlePresetChange(e.target.value)}
          >
            {PRESET_NAMES.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </div>

        <div className="jcs:flex jcs:items-center jcs:gap-1">
          <span className="jcs:text-text-muted jcs:text-[9px]">Font:</span>
          <select
            className="jcs:text-[10px] jcs:w-[100px]"
            value={style.font_path}
            onChange={(e) => updateStyle({ font_path: e.target.value })}
          >
            {fontList.map((f) => (
              <option key={f} value={f}>
                {fontDisplay(f)}
              </option>
            ))}
          </select>
        </div>

        <div className="jcs:flex jcs:items-center jcs:gap-1">
          <span className="jcs:text-text-muted jcs:text-[9px]">Size:</span>
          <input
            type="number"
            className="jcs:text-[10px] jcs:w-[48px]"
            value={style.font_size}
            min={20}
            max={400}
            step={4}
            onChange={(e) => updateStyle({ font_size: Math.round(parseFloat(e.target.value) || 20) })}
          />
        </div>
      </div>

      {/* Row 2: Fill, Stroke, Highlight colors, More toggle */}
      <div className="jcs:flex jcs:items-center jcs:gap-2 jcs:flex-wrap">
        <div className="jcs:flex jcs:items-center jcs:gap-1">
          <span className="jcs:text-text-muted jcs:text-[9px]">Fill:</span>
          <input
            type="color"
            value={style.fill_color}
            onChange={(e) => updateStyle({ fill_color: e.target.value })}
            className="jcs:w-6 jcs:h-5"
          />
        </div>

        <div className="jcs:flex jcs:items-center jcs:gap-1">
          <span className="jcs:text-text-muted jcs:text-[9px]">Stroke:</span>
          <input
            type="color"
            value={style.stroke_color}
            onChange={(e) => updateStyle({ stroke_color: e.target.value })}
            className="jcs:w-6 jcs:h-5"
          />
          <span className="jcs:text-text-dim jcs:text-[9px]">/</span>
          <input
            type="number"
            className="jcs:text-[10px] jcs:w-[36px]"
            value={style.stroke_width}
            min={0}
            max={30}
            step={1}
            onChange={(e) => updateStyle({ stroke_width: Math.round(parseFloat(e.target.value) || 0) })}
          />
        </div>

        <div className="jcs:flex jcs:items-center jcs:gap-1">
          <span className="jcs:text-text-muted jcs:text-[9px]">HL:</span>
          {style.highlight_colors.map((color, i) => (
            <input
              key={i}
              type="color"
              value={color}
              onChange={(e) => {
                const updated = [...style.highlight_colors] as [string, string, string, string];
                updated[i] = e.target.value;
                updateStyle({ highlight_colors: updated });
              }}
              className="jcs:w-5 jcs:h-5"
              title={`Highlight ${i + 1}`}
            />
          ))}
        </div>

        <div className="jcs:flex-1" />

        <button className="jcs:text-[10px]" onClick={toggleStyleExpanded}>
          {styleExpanded ? "\u25B2 Less" : "\u25BC More"}
        </button>
      </div>
    </div>
  );
}
