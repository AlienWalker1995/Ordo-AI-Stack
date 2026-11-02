import { create } from "zustand";
import { immer } from "zustand/middleware/immer";
import type { WordTiming, CaptionStyle, StyleConfig, FrameAtlasData } from "../types";
import { DEFAULT_STYLE } from "../constants";

export interface CaptionStoreState {
  // Data from Python
  originalTimings: WordTiming[];
  editedTimings: WordTiming[];
  style: CaptionStyle;
  presetName: string;
  fps: number;
  frameWidth: number;
  frameHeight: number;
  frameCount: number;
  previewFrameBase64: string;
  fontList: string[];

  // Frame atlas for video scrubbing
  frameAtlas: FrameAtlasData | null;
  duration: number;

  // Audio preview
  audioDataUrl: string;

  // UI state
  selectedWordIndex: number | null;
  zoomLevel: number;
  scrollLeft: number;
  trackCount: number;
  previewTime: number;
  isPlaying: boolean;
  styleExpanded: boolean;
  editingWordIndex: number | null;
  editingWordText: string;

  // Actions
  initFromPython: (data: {
    originalTimings: WordTiming[];
    editedTimings: WordTiming[];
    style: CaptionStyle;
    presetName: string;
    fps: number;
    frameWidth: number;
    frameHeight: number;
    frameCount: number;
    previewFrameBase64: string;
    fontList: string[];
    frameAtlas?: FrameAtlasData | null;
    audioDataUrl?: string;
  }) => void;

  moveWord: (index: number, newStart: number, newEnd: number) => void;
  resizeWord: (index: number, newEnd: number) => void;
  selectWord: (index: number | null) => void;
  moveWordToTrack: (index: number, track: number) => void;
  addTrack: () => void;
  setPreset: (name: string) => void;
  updateStyle: (partial: Partial<CaptionStyle>) => void;
  setWordOverride: (
    index: number,
    field: keyof WordTiming,
    value: unknown
  ) => void;
  clearWordOverrides: (index: number) => void;
  addWord: (atTime: number) => void;
  deleteWord: (index: number) => void;
  duplicateWord: (index: number) => void;
  resetEdits: () => void;
  setZoomLevel: (zoom: number) => void;
  setScrollLeft: (scroll: number) => void;
  setPreviewTime: (time: number) => void;
  togglePlay: () => void;
  setIsPlaying: (playing: boolean) => void;
  toggleStyleExpanded: () => void;
  stepFrame: (delta: number) => void;
  seekToTime: (time: number) => void;
  startEditingWord: (index: number) => void;
  commitEditingWord: () => void;
  cancelEditingWord: () => void;
  setEditingWordText: (text: string) => void;

  // Serialization
  getEditedTimingsJSON: () => string;
  getStyleConfigJSON: () => string;
}

export const createCaptionStore = () =>
  create<CaptionStoreState>()(
    immer((set, get) => ({
      // Initial state
      originalTimings: [],
      editedTimings: [],
      style: { ...DEFAULT_STYLE },
      presetName: "Custom",
      fps: 30,
      frameWidth: 1920,
      frameHeight: 1080,
      frameCount: 0,
      previewFrameBase64: "",
      fontList: [],

      frameAtlas: null,
      duration: 0,
      audioDataUrl: "",

      selectedWordIndex: null,
      zoomLevel: 1.0,
      scrollLeft: 0,
      trackCount: 1,
      previewTime: 0,
      isPlaying: false,
      styleExpanded: false,
      editingWordIndex: null,
      editingWordText: "",

      initFromPython: (data) =>
        set((state) => {
          state.originalTimings = data.originalTimings;
          if (state.editedTimings.length === 0) {
            state.editedTimings = data.editedTimings.length > 0
              ? data.editedTimings
              : data.originalTimings.map((w) => ({ ...w }));
          }
          state.style = data.style;
          state.presetName = data.presetName;
          state.fps = data.fps;
          state.frameWidth = data.frameWidth;
          state.frameHeight = data.frameHeight;
          state.frameCount = data.frameCount;
          state.previewFrameBase64 = data.previewFrameBase64;
          state.fontList = data.fontList;

          if (data.frameAtlas) {
            state.frameAtlas = data.frameAtlas;
          }

          state.audioDataUrl = data.audioDataUrl || "";

          // Compute duration from frame count or timings
          if (data.frameCount > 0 && data.fps > 0) {
            state.duration = data.frameCount / data.fps;
          } else {
            const timings = state.editedTimings;
            state.duration = timings.length > 0
              ? Math.max(...timings.map((w) => w.end))
              : 0;
          }

          const maxTrack = Math.max(
            0,
            ...state.editedTimings.map((w) => w.track || 0)
          );
          if (maxTrack + 1 > state.trackCount) {
            state.trackCount = maxTrack + 1;
          }
        }),

      moveWord: (index, newStart, newEnd) =>
        set((state) => {
          if (state.editedTimings[index]) {
            state.editedTimings[index].start = Math.round(newStart * 1000) / 1000;
            state.editedTimings[index].end = Math.round(newEnd * 1000) / 1000;
          }
        }),

      resizeWord: (index, newEnd) =>
        set((state) => {
          if (state.editedTimings[index]) {
            state.editedTimings[index].end = Math.round(newEnd * 1000) / 1000;
          }
        }),

      selectWord: (index) =>
        set((state) => {
          state.selectedWordIndex = index;
        }),

      moveWordToTrack: (index, track) =>
        set((state) => {
          if (state.editedTimings[index]) {
            state.editedTimings[index].track = track;
          }
        }),

      addTrack: () =>
        set((state) => {
          state.trackCount++;
        }),

      setPreset: (name) =>
        set((state) => {
          state.presetName = name;
        }),

      updateStyle: (partial) =>
        set((state) => {
          Object.assign(state.style, partial);
          state.presetName = "Custom";
        }),

      setWordOverride: (index, field, value) =>
        set((state) => {
          if (state.editedTimings[index]) {
            (state.editedTimings[index] as Record<string, unknown>)[field] =
              value;
          }
        }),

      clearWordOverrides: (index) =>
        set((state) => {
          const w = state.editedTimings[index];
          if (w) {
            delete w.override_text;
            delete w.override_color;
            delete w.override_highlight;
            delete w.override_scale;
            delete w.override_font_size;
            delete w.override_stroke_width;
            delete w.override_stroke_color;
            delete w.override_hidden;
          }
        }),

      addWord: (atTime) =>
        set((state) => {
          const defaultDuration = 0.3;
          const newWord: WordTiming = {
            word: "new",
            start: Math.round(atTime * 1000) / 1000,
            end: Math.round((atTime + defaultDuration) * 1000) / 1000,
            segment_idx: -1,
            track: 0,
          };
          state.editedTimings.push(newWord);
          const newIndex = state.editedTimings.length - 1;
          state.selectedWordIndex = newIndex;
          state.editingWordIndex = newIndex;
          state.editingWordText = "new";
        }),

      deleteWord: (index) =>
        set((state) => {
          if (index >= 0 && index < state.editedTimings.length) {
            state.editedTimings.splice(index, 1);
            if (state.selectedWordIndex === index) {
              state.selectedWordIndex = null;
            } else if (
              state.selectedWordIndex !== null &&
              state.selectedWordIndex > index
            ) {
              state.selectedWordIndex--;
            }
            if (state.editingWordIndex === index) {
              state.editingWordIndex = null;
              state.editingWordText = "";
            } else if (
              state.editingWordIndex !== null &&
              state.editingWordIndex > index
            ) {
              state.editingWordIndex--;
            }
          }
        }),

      duplicateWord: (index) =>
        set((state) => {
          const src = state.editedTimings[index];
          if (!src) return;
          const duration = src.end - src.start;
          const clone: WordTiming = {
            ...src,
            start: Math.round(src.end * 1000) / 1000,
            end: Math.round((src.end + duration) * 1000) / 1000,
          };
          state.editedTimings.splice(index + 1, 0, clone);
          state.selectedWordIndex = index + 1;
        }),

      resetEdits: () =>
        set((state) => {
          state.editedTimings = state.originalTimings.map((w) => ({ ...w }));
          state.selectedWordIndex = null;
          state.trackCount = 1;
          state.presetName = "Custom";
          state.style = { ...DEFAULT_STYLE };
        }),

      setZoomLevel: (zoom) =>
        set((state) => {
          state.zoomLevel = Math.max(0.5, Math.min(10.0, zoom));
        }),

      setScrollLeft: (scroll) =>
        set((state) => {
          state.scrollLeft = scroll;
        }),

      setPreviewTime: (time) =>
        set((state) => {
          state.previewTime = time;
        }),

      togglePlay: () =>
        set((state) => {
          state.isPlaying = !state.isPlaying;
        }),

      setIsPlaying: (playing) =>
        set((state) => {
          state.isPlaying = playing;
        }),

      toggleStyleExpanded: () =>
        set((state) => {
          state.styleExpanded = !state.styleExpanded;
        }),

      stepFrame: (delta) =>
        set((state) => {
          const frameDuration = 1 / (state.fps || 30);
          const newTime = Math.max(0, Math.min(state.duration, state.previewTime + delta * frameDuration));
          state.previewTime = Math.round(newTime * 1000) / 1000;
          state.isPlaying = false;
        }),

      seekToTime: (time) =>
        set((state) => {
          state.previewTime = Math.max(0, Math.min(state.duration, time));
          state.isPlaying = false;
        }),

      startEditingWord: (index) =>
        set((state) => {
          if (state.editedTimings[index]) {
            state.editingWordIndex = index;
            state.editingWordText = state.editedTimings[index].override_text || state.editedTimings[index].word;
          }
        }),

      commitEditingWord: () =>
        set((state) => {
          if (state.editingWordIndex !== null && state.editedTimings[state.editingWordIndex]) {
            const original = state.editedTimings[state.editingWordIndex].word;
            const newText = state.editingWordText.trim();
            if (newText && newText !== original) {
              state.editedTimings[state.editingWordIndex].override_text = newText;
            } else if (newText === original) {
              delete state.editedTimings[state.editingWordIndex].override_text;
            }
          }
          state.editingWordIndex = null;
          state.editingWordText = "";
        }),

      cancelEditingWord: () =>
        set((state) => {
          state.editingWordIndex = null;
          state.editingWordText = "";
        }),

      setEditingWordText: (text) =>
        set((state) => {
          state.editingWordText = text;
        }),

      getEditedTimingsJSON: () => {
        const { editedTimings } = get();
        const compact = editedTimings.map((w) => {
          const entry: Record<string, unknown> = {
            word: w.word,
            start: Math.round(w.start * 1000) / 1000,
            end: Math.round(w.end * 1000) / 1000,
            segment_idx: w.segment_idx ?? -1,
            track: w.track || 0,
          };
          if (w.override_text) entry.override_text = w.override_text;
          if (w.override_color) entry.override_color = w.override_color;
          if (w.override_highlight)
            entry.override_highlight = w.override_highlight;
          if (w.override_scale && w.override_scale !== 1.0)
            entry.override_scale = w.override_scale;
          if (w.override_font_size)
            entry.override_font_size = w.override_font_size;
          if (w.override_stroke_width != null)
            entry.override_stroke_width = w.override_stroke_width;
          if (w.override_stroke_color)
            entry.override_stroke_color = w.override_stroke_color;
          if (w.override_hidden) entry.override_hidden = w.override_hidden;
          return entry;
        });
        return JSON.stringify(compact);
      },

      getStyleConfigJSON: () => {
        const { style, presetName } = get();
        const config: StyleConfig = {
          ...style,
          _preset: presetName,
        };
        return JSON.stringify(config);
      },
    }))
  );

export type CaptionStore = ReturnType<typeof createCaptionStore>;
