import { useState } from "react";

interface ColorFieldProps {
  label: string;
  value: string;
  onChange: (value: string) => void;
}

export default function ColorField({ label, value, onChange }: ColorFieldProps) {
  const [hexText, setHexText] = useState(value);

  const handlePickerChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const v = e.target.value;
    setHexText(v);
    onChange(v);
  };

  const handleTextChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const v = e.target.value;
    setHexText(v);
    if (/^#[0-9a-fA-F]{6}$/.test(v)) {
      onChange(v);
    }
  };

  if (value !== hexText && /^#[0-9a-fA-F]{6}$/.test(value)) {
    setHexText(value);
  }

  return (
    <div className="jcs:flex jcs:items-center jcs:gap-2">
      <label className="jcs:text-text-muted jcs:text-[10px] jcs:min-w-[80px] jcs:text-right jcs:shrink-0">
        {label}
      </label>
      <div className="jcs:flex jcs:items-center jcs:gap-1">
        <input
          type="color"
          value={value}
          onChange={handlePickerChange}
          className="jcs:w-7 jcs:h-5"
        />
        <input
          type="text"
          value={hexText}
          onChange={handleTextChange}
          className="jcs:text-[9px] jcs:w-[70px] jcs:font-mono"
        />
      </div>
    </div>
  );
}
