interface CheckboxFieldProps {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}

export default function CheckboxField({
  label,
  checked,
  onChange,
}: CheckboxFieldProps) {
  return (
    <div className="jcs:flex jcs:items-center jcs:gap-2">
      <label className="jcs:text-text-muted jcs:text-[10px] jcs:min-w-[80px] jcs:text-right jcs:shrink-0">
        {label}
      </label>
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
      />
    </div>
  );
}
