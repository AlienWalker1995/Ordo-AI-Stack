interface NumberInputProps {
  label: string;
  value: number | undefined;
  onChange: (value: number | undefined) => void;
  min?: number;
  max?: number;
  step?: number;
  placeholder?: string;
}

export default function NumberInput({
  label,
  value,
  onChange,
  min,
  max,
  step = 1,
  placeholder,
}: NumberInputProps) {
  return (
    <div className="jcs:flex jcs:items-center jcs:gap-2">
      <label className="jcs:text-text-muted jcs:text-[10px] jcs:min-w-[80px] jcs:text-right jcs:shrink-0">
        {label}
      </label>
      <input
        type="number"
        value={value ?? ""}
        min={min}
        max={max}
        step={step}
        placeholder={placeholder}
        onChange={(e) => {
          const v = e.target.value;
          onChange(v === "" ? undefined : parseFloat(v));
        }}
        className="jcs:text-[10px] jcs:w-20"
      />
    </div>
  );
}
