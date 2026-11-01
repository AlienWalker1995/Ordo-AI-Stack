import { useCaptionStoreContext } from "../../store/StoreContext";
import { PRESET_NAMES, STYLE_PRESETS } from "../../constants";

export default function PresetSelector() {
  const presetName = useCaptionStoreContext((s) => s.presetName);
  const setPreset = useCaptionStoreContext((s) => s.setPreset);
  const updateStyle = useCaptionStoreContext((s) => s.updateStyle);

  const handleChange = (name: string) => {
    setPreset(name);
    if (name !== "Custom" && STYLE_PRESETS[name]) {
      updateStyle({ ...STYLE_PRESETS[name] });
      setPreset(name);
    }
  };

  return (
    <div className="jcs:flex jcs:items-center jcs:gap-2">
      <label className="jcs:text-text-muted jcs:text-[10px] jcs:min-w-[80px] jcs:text-right jcs:shrink-0">
        Preset
      </label>
      <select
        className="jcs:bg-surface-raised jcs:text-text jcs:border jcs:border-border-light jcs:rounded jcs:px-1 jcs:py-0.5 jcs:text-[10px] jcs:flex-1 jcs:min-w-0"
        value={presetName}
        onChange={(e) => handleChange(e.target.value)}
      >
        {PRESET_NAMES.map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </select>
    </div>
  );
}
