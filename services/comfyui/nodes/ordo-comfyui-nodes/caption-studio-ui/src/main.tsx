/**
 * Ordo Caption Studio — entry point.
 *
 * In library mode (ComfyUI): exports bridge functions for the extension loader.
 * In dev mode (standalone): mounts into #root with mock data.
 */

export {
  mountCaptionStudio,
  receiveFromPython,
  setWidgetRefs,
} from "./bridge/comfyBridge";

/**
 * Generate a mock WAV file (sine tone) as a base64 string for dev audio preview.
 * Pure math — no Web Audio API needed.
 */
function generateMockWav(durationSec: number, freq: number, sampleRate: number): string {
  const numSamples = Math.floor(durationSec * sampleRate);
  const dataBytes = numSamples * 2; // 16-bit = 2 bytes per sample

  // WAV header (44 bytes) + PCM data
  const buffer = new ArrayBuffer(44 + dataBytes);
  const view = new DataView(buffer);

  // Helper to write ASCII string
  const writeStr = (offset: number, str: string) => {
    for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i));
  };

  // RIFF header
  writeStr(0, "RIFF");
  view.setUint32(4, 36 + dataBytes, true); // file size - 8
  writeStr(8, "WAVE");

  // fmt chunk
  writeStr(12, "fmt ");
  view.setUint32(16, 16, true);        // chunk size
  view.setUint16(20, 1, true);         // PCM format
  view.setUint16(22, 1, true);         // mono
  view.setUint32(24, sampleRate, true); // sample rate
  view.setUint32(28, sampleRate * 2, true); // byte rate
  view.setUint16(32, 2, true);         // block align
  view.setUint16(34, 16, true);        // bits per sample

  // data chunk
  writeStr(36, "data");
  view.setUint32(40, dataBytes, true);

  // Generate sine wave samples
  for (let i = 0; i < numSamples; i++) {
    const t = i / sampleRate;
    const sample = Math.sin(2 * Math.PI * freq * t) * 0.3; // 30% volume
    const int16 = Math.max(-32768, Math.min(32767, Math.floor(sample * 32767)));
    view.setInt16(44 + i * 2, int16, true);
  }

  // Convert to base64
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

/**
 * Generate a tiny gradient frame as a data URL for the mock frame atlas.
 * Each frame has a slightly different hue to simulate video scrubbing.
 */
function generateMockFrame(
  width: number,
  height: number,
  hue: number
): string {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d")!;

  // Gradient background shifting by hue
  const grad = ctx.createLinearGradient(0, 0, width, height);
  grad.addColorStop(0, `hsl(${hue}, 60%, 25%)`);
  grad.addColorStop(0.5, `hsl(${(hue + 60) % 360}, 50%, 35%)`);
  grad.addColorStop(1, `hsl(${(hue + 120) % 360}, 40%, 20%)`);
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, width, height);

  // Time indicator bar at bottom
  const progress = hue / 360;
  ctx.fillStyle = "rgba(255,255,255,0.15)";
  ctx.fillRect(0, height - 4, width * progress, 4);

  return canvas.toDataURL("image/jpeg", 0.6).split(",")[1];
}

// Dev mode: mount into #root if it exists and we're not in library mode
if (typeof window !== "undefined" && document.getElementById("root")) {
  import("./bridge/comfyBridge").then(({ mountCaptionStudio, receiveFromPython }) => {
    const root = document.getElementById("root")!;
    mountCaptionStudio(root, "dev-0");

    // Realistic 19-word timing data
    const mockTimings = [
      { word: "Caption", start: 0.0, end: 0.28, segment_idx: 0, track: 0 },
      { word: "Studio", start: 0.30, end: 0.58, segment_idx: 0, track: 0 },
      { word: "gives", start: 0.60, end: 0.82, segment_idx: 0, track: 0 },
      { word: "you", start: 0.84, end: 0.98, segment_idx: 1, track: 0 },
      { word: "complete", start: 1.00, end: 1.35, segment_idx: 1, track: 0 },
      { word: "control", start: 1.38, end: 1.72, segment_idx: 1, track: 0 },
      { word: "over", start: 1.75, end: 1.92, segment_idx: 2, track: 0 },
      { word: "every", start: 1.95, end: 2.18, segment_idx: 2, track: 0 },
      { word: "single", start: 2.20, end: 2.48, segment_idx: 2, track: 0 },
      { word: "word", start: 2.50, end: 2.72, segment_idx: 3, track: 0 },
      { word: "in", start: 2.74, end: 2.82, segment_idx: 3, track: 0 },
      { word: "your", start: 2.84, end: 2.98, segment_idx: 3, track: 0 },
      { word: "captions", start: 3.00, end: 3.42, segment_idx: 4, track: 0 },
      { word: "with", start: 3.45, end: 3.58, segment_idx: 4, track: 0 },
      { word: "real", start: 3.60, end: 3.82, segment_idx: 4, track: 0 },
      { word: "time", start: 3.85, end: 4.12, segment_idx: 5, track: 0 },
      { word: "preview", start: 4.15, end: 4.52, segment_idx: 5, track: 0 },
      { word: "and", start: 4.55, end: 4.65, segment_idx: 5, track: 0 },
      { word: "editing", start: 4.68, end: 5.10, segment_idx: 5, track: 0 },
    ];

    // Generate mock audio (5s 440Hz sine tone at 22050Hz)
    const mockAudioB64 = generateMockWav(5, 440, 22050);

    // Generate fake frame atlas (~30 frames for a 5s clip at 30fps, sampled every 5 frames)
    const atlasWidth = 480;
    const atlasHeight = 270;
    const totalFrames = 150; // 5s at 30fps
    const interval = 5;
    const atlasFrameCount = Math.ceil(totalFrames / interval);
    const mockFrames: string[] = [];
    for (let i = 0; i < atlasFrameCount; i++) {
      const hue = (i / atlasFrameCount) * 360;
      mockFrames.push(generateMockFrame(atlasWidth, atlasHeight, hue));
    }

    receiveFromPython({
      timings_json: [JSON.stringify(mockTimings)],
      edited_timings_json: [""],
      style_json: [JSON.stringify({
        font_path: "C:/Windows/Fonts/impact.ttf",
        font_size: 120,
        fill_color: "#FFFFFF",
        highlight_colors: ["#00FFFF", "#00FF00", "#FFFF00", "#FF4444"],
        stroke_color: "#000000",
        stroke_width: 8,
        shadow_color: "#000000",
        shadow_opacity: 0.6,
        shadow_offset_x: 4,
        shadow_offset_y: 4,
        shadow_blur: 6.0,
        anim_duration: 0.15,
        y_position: 0.45,
        uppercase: true,
      })],
      style_presets: ["{}"],
      fps: [30],
      frame_width: [1920],
      frame_height: [1080],
      frame_count: [totalFrames],
      preview_frame: [""],
      font_list: [JSON.stringify([
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/georgia.ttf",
        "C:/Windows/Fonts/impact.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
      ])],
      frame_atlas: [JSON.stringify({
        frames: mockFrames,
        interval,
        fps: 30,
        width: atlasWidth,
        height: atlasHeight,
      })],
      audio_data: [mockAudioB64],
    });
  });
}
