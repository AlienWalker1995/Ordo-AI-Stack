interface SliderFieldProps {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (value: number) => void;
  decimals?: number;
}

export default function SliderField({
  label,
  value,
  min,
  max,
  step,
  onChange,
  decimals = 1,
}: SliderFieldProps) {
  return (
    <div className="jcs:flex jcs:items-center jcs:gap-2">
      <label className="jcs:text-text-muted jcs:text-[10px] jcs:min-w-[80px] jcs:text-right jcs:shrink-0">
        {label}
      </label>
      <div className="jcs:flex jcs:items-center jcs:gap-1.5 jcs:flex-1">
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={value}
          onChange={(e) => onChange(parseFloat(e.target.value))}
          className="jcs:flex-1"
        />
        <span className="jcs:text-text-muted jcs:text-[10px] jcs:min-w-[32px] jcs:text-right jcs:tabular-nums">
          {value.toFixed(decimals)}
        </span>
      </div>
    </div>
  );
}
