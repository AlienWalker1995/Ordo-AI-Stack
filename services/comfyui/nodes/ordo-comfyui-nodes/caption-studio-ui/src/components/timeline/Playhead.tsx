interface PlayheadProps {
  time: number;
  minTime: number;
  pps: number;
  height: number;
}

export default function Playhead({ time, minTime, pps, height }: PlayheadProps) {
  const x = (time - minTime) * pps;

  if (x < 0) return null;

  return (
    <div
      className="jcs:absolute jcs:top-0 jcs:pointer-events-none jcs:z-10"
      style={{ left: x, height }}
    >
      {/* Triangle head */}
      <div
        className="jcs:absolute"
        style={{
          left: -5,
          top: 0,
          width: 0,
          height: 0,
          borderLeft: "5px solid transparent",
          borderRight: "5px solid transparent",
          borderTop: "7px solid #e53935",
          filter: "drop-shadow(0 0 2px rgba(229,57,53,0.6))",
        }}
      />
      {/* Vertical line */}
      <div
        className="jcs:absolute"
        style={{
          left: -0.5,
          top: 7,
          width: 1,
          height: height - 7,
          background: "#e53935",
          boxShadow: "0 0 4px rgba(229,57,53,0.4)",
        }}
      />
    </div>
  );
}
