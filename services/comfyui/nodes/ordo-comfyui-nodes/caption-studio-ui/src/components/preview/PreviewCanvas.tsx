import { useEffect, useRef, useCallback } from "react";
import { useCaptionStoreContext } from "../../store/StoreContext";
import { groupWordsForPreview } from "../../utils/groupWords";
import { hexToRgb } from "../../utils/color";

export default function PreviewCanvas() {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  const previewFrameBase64 = useCaptionStoreContext(
    (s) => s.previewFrameBase64
  );
  const frameAtlas = useCaptionStoreContext((s) => s.frameAtlas);
  const frameWidth = useCaptionStoreContext((s) => s.frameWidth);
  const frameHeight = useCaptionStoreContext((s) => s.frameHeight);
  const timings = useCaptionStoreContext((s) => s.editedTimings);
  const style = useCaptionStoreContext((s) => s.style);
  const previewTime = useCaptionStoreContext((s) => s.previewTime);
  const fps = useCaptionStoreContext((s) => s.fps);

  // Lazy image cache for atlas frames
  const atlasImageCache = useRef<Map<number, HTMLImageElement>>(new Map());
  const bgImageRef = useRef<HTMLImageElement | null>(null);

  // Load single preview frame fallback
  useEffect(() => {
    if (!previewFrameBase64) return;
    const img = new Image();
    img.src = `data:image/jpeg;base64,${previewFrameBase64}`;
    img.onload = () => {
      bgImageRef.current = img;
    };
  }, [previewFrameBase64]);

  // Get the atlas frame image for a given time (lazy loaded)
  const getAtlasFrame = useCallback(
    (time: number): HTMLImageElement | null => {
      if (!frameAtlas || frameAtlas.frames.length === 0) return null;

      const frameIdx = Math.floor(time * fps);
      const atlasIdx = Math.min(
        Math.floor(frameIdx / frameAtlas.interval),
        frameAtlas.frames.length - 1
      );
      const safeIdx = Math.max(0, atlasIdx);

      // Check cache
      const cached = atlasImageCache.current.get(safeIdx);
      if (cached && cached.complete) return cached;

      // Start loading if not cached
      if (!cached) {
        const img = new Image();
        img.src = `data:image/jpeg;base64,${frameAtlas.frames[safeIdx]}`;
        atlasImageCache.current.set(safeIdx, img);
        img.onload = () => {
          // Trigger re-render by updating the canvas
          const canvas = canvasRef.current;
          if (canvas) renderFrame(previewTime);
        };
      }

      return null;
    },
    [frameAtlas, fps]
  );

  const backEaseOut = (t: number): number => {
    const c1 = 1.70158;
    const c3 = c1 + 1;
    return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2);
  };

  const renderFrame = useCallback(
    (time: number) => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      const ctx = canvas.getContext("2d");
      if (!ctx) return;

      const displayW = canvas.width;
      const displayH = canvas.height;

      ctx.clearRect(0, 0, displayW, displayH);

      // Draw background: atlas frame > single preview frame > black
      const atlasImg = getAtlasFrame(time);
      if (atlasImg && atlasImg.complete) {
        ctx.drawImage(atlasImg, 0, 0, displayW, displayH);
      } else if (bgImageRef.current) {
        ctx.drawImage(bgImageRef.current, 0, 0, displayW, displayH);
      } else {
        ctx.fillStyle = "#000";
        ctx.fillRect(0, 0, displayW, displayH);
      }

      if (timings.length === 0) return;

      const groups = groupWordsForPreview(timings);
      const scale = displayW / (frameWidth || 1920);

      for (const group of groups) {
        if (time < group.start || time > group.end) continue;

        const visibleWords = group.words.filter((w) => !w.override_hidden);
        if (visibleWords.length === 0) continue;

        const baseFontSize = Math.round(style.font_size * scale);
        const uppercase = style.uppercase;

        const animDuration = style.anim_duration;
        const progress =
          animDuration > 0
            ? Math.min(1.0, (time - group.start) / animDuration)
            : 1.0;
        const animScale = backEaseOut(progress);

        let activeWordIdx = 0;
        for (let i = 0; i < visibleWords.length; i++) {
          if (visibleWords[i].start <= time) activeWordIdx = i;
        }

        const fontFamily = "Impact, Arial Black, sans-serif";

        const wordMeasures: {
          text: string;
          width: number;
          fontSize: number;
          word: (typeof visibleWords)[0];
        }[] = [];

        for (const w of visibleWords) {
          const raw = w.override_text || w.word;
          const text = uppercase ? raw.toUpperCase() : raw;
          let fontSize = baseFontSize;
          if (w.override_font_size) {
            fontSize = Math.round(w.override_font_size * scale);
          } else if (w.override_scale) {
            fontSize = Math.round(baseFontSize * w.override_scale);
          }

          ctx.font = `bold ${fontSize}px ${fontFamily}`;
          const metrics = ctx.measureText(text);
          wordMeasures.push({ text, width: metrics.width, fontSize, word: w });
        }

        ctx.font = `bold ${baseFontSize}px ${fontFamily}`;
        const spaceWidth = ctx.measureText(" ").width;
        const totalWidth =
          wordMeasures.reduce((sum, m) => sum + m.width, 0) +
          spaceWidth * Math.max(0, wordMeasures.length - 1);

        const maxWidth = displayW * 0.9;
        const fitScale = totalWidth > maxWidth ? maxWidth / totalWidth : 1.0;
        const combinedScale = animScale * fitScale;

        const trackSpacing = 0.12;
        const yPos = style.y_position + group.track * trackSpacing;
        const centerY = displayH * Math.min(0.95, yPos);

        ctx.save();
        ctx.translate(displayW / 2, centerY);
        ctx.scale(combinedScale, combinedScale);

        let x = -totalWidth / 2;

        for (let i = 0; i < wordMeasures.length; i++) {
          const m = wordMeasures[i];
          const isActive = i === activeWordIdx;

          ctx.font = `bold ${m.fontSize}px ${fontFamily}`;

          let fillColor: string;
          if (isActive) {
            if (m.word.override_highlight) {
              fillColor = m.word.override_highlight;
            } else {
              const colorIdx =
                (hashCode(m.word.word.toLowerCase()) & 0x7fffffff) %
                style.highlight_colors.length;
              fillColor = style.highlight_colors[colorIdx];
            }
          } else {
            fillColor = m.word.override_color || style.fill_color;
          }

          const strokeColor =
            m.word.override_stroke_color || style.stroke_color;
          const strokeWidth = Math.round(
            (m.word.override_stroke_width ?? style.stroke_width) * scale
          );

          if (style.shadow_opacity > 0) {
            const [sr, sg, sb] = hexToRgb(style.shadow_color);
            ctx.save();
            ctx.shadowColor = `rgba(${sr},${sg},${sb},${style.shadow_opacity})`;
            ctx.shadowBlur = style.shadow_blur * scale;
            ctx.shadowOffsetX = style.shadow_offset_x * scale;
            ctx.shadowOffsetY = style.shadow_offset_y * scale;
            ctx.fillStyle = fillColor;
            ctx.fillText(m.text, x, 0);
            ctx.restore();
          }

          if (strokeWidth > 0) {
            ctx.strokeStyle = strokeColor;
            ctx.lineWidth = strokeWidth;
            ctx.lineJoin = "round";
            ctx.strokeText(m.text, x, 0);
          }

          ctx.fillStyle = fillColor;
          ctx.fillText(m.text, x, 0);

          x += m.width + spaceWidth;
        }

        ctx.restore();
      }
    },
    [timings, style, frameWidth, frameHeight, getAtlasFrame]
  );

  useEffect(() => {
    renderFrame(previewTime);
  }, [previewTime, renderFrame]);

  const canvasDisplayWidth = Math.min(480, frameWidth || 480);
  const aspectRatio = (frameHeight || 1080) / (frameWidth || 1920);
  const canvasDisplayHeight = Math.round(canvasDisplayWidth * aspectRatio);

  return (
    <canvas
      ref={canvasRef}
      width={canvasDisplayWidth}
      height={canvasDisplayHeight}
      className="jcs:border jcs:border-border jcs:rounded jcs:max-w-full jcs:h-auto"
    />
  );
}

function hashCode(str: string): number {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    const char = str.charCodeAt(i);
    hash = (hash << 5) - hash + char;
    hash |= 0;
  }
  return hash;
}
