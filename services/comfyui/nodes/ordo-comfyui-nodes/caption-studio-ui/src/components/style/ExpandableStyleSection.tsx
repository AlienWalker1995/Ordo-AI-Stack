import { useCaptionStoreContext } from "../../store/StoreContext";
import SliderField from "../shared/SliderField";
import CheckboxField from "../shared/CheckboxField";
import ColorField from "../shared/ColorField";

export default function ExpandableStyleSection() {
  const style = useCaptionStoreContext((s) => s.style);
  const updateStyle = useCaptionStoreContext((s) => s.updateStyle);

  return (
    <div className="jcs:px-2 jcs:py-1.5 jcs:border-b jcs:border-border jcs:bg-panel jcs:flex jcs:flex-col jcs:gap-1">
      <div className="jcs:text-[9px] jcs:text-text-dim jcs:uppercase jcs:tracking-wider jcs:mb-0.5">
        Shadow
      </div>

      <ColorField
        label="Shadow Color"
        value={style.shadow_color}
        onChange={(v) => updateStyle({ shadow_color: v })}
      />

      <SliderField
        label="Opacity"
        value={style.shadow_opacity}
        min={0}
        max={1}
        step={0.05}
        decimals={2}
        onChange={(v) => updateStyle({ shadow_opacity: v })}
      />

      <SliderField
        label="Blur"
        value={style.shadow_blur}
        min={0}
        max={30}
        step={0.5}
        onChange={(v) => updateStyle({ shadow_blur: v })}
      />

      <SliderField
        label="Offset X"
        value={style.shadow_offset_x}
        min={-20}
        max={20}
        step={1}
        decimals={0}
        onChange={(v) => updateStyle({ shadow_offset_x: Math.round(v) })}
      />

      <SliderField
        label="Offset Y"
        value={style.shadow_offset_y}
        min={-20}
        max={20}
        step={1}
        decimals={0}
        onChange={(v) => updateStyle({ shadow_offset_y: Math.round(v) })}
      />

      <div className="jcs:text-[9px] jcs:text-text-dim jcs:uppercase jcs:tracking-wider jcs:mt-1 jcs:mb-0.5">
        Animation & Layout
      </div>

      <SliderField
        label="Anim Duration"
        value={style.anim_duration}
        min={0}
        max={1}
        step={0.01}
        decimals={2}
        onChange={(v) => updateStyle({ anim_duration: v })}
      />

      <SliderField
        label="Y Position"
        value={style.y_position}
        min={0}
        max={1}
        step={0.01}
        decimals={2}
        onChange={(v) => updateStyle({ y_position: v })}
      />

      <CheckboxField
        label="Uppercase"
        checked={style.uppercase}
        onChange={(v) => updateStyle({ uppercase: v })}
      />
    </div>
  );
}
