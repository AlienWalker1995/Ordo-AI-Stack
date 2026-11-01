/** Word-level timing entry matching Python's ORDO_WORD_TIMINGS format. */
export interface WordTiming {
  word: string;
  start: number;
  end: number;
  segment_idx: number;
  track: number;
  // Per-word overrides
  override_text?: string;
  override_color?: string;
  override_highlight?: string;
  override_scale?: number;
  override_font_size?: number;
  override_stroke_width?: number;
  override_stroke_color?: string;
  override_hidden?: boolean;
}

/** Caption style matching Python's ORDO_CAPTION_STYLE format. */
export interface CaptionStyle {
  font_path: string;
  font_size: number;
  fill_color: string;
  highlight_colors: [string, string, string, string];
  stroke_color: string;
  stroke_width: number;
  shadow_color: string;
  shadow_opacity: number;
  shadow_offset_x: number;
  shadow_offset_y: number;
  shadow_blur: number;
  anim_duration: number;
  y_position: number;
  uppercase: boolean;
}

/** Style config sent back to Python (includes _preset key). */
export interface StyleConfig extends CaptionStyle {
  _preset: string;
}

/** Frame atlas data from Python for video scrubbing. */
export interface FrameAtlasData {
  frames: string[];   // base64 JPEG strings
  interval: number;   // every Nth source frame
  fps: number;
  width: number;
  height: number;
}

/** Grouped words for preview rendering. */
export interface WordGroup {
  words: WordTiming[];
  start: number;
  end: number;
  segmentIdx: number;
  track: number;
}
