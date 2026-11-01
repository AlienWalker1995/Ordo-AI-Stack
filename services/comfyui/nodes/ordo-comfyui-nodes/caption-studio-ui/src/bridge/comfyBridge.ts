import React from "react";
import ReactDOM from "react-dom/client";
import App from "../App";
import { createCaptionStore, type CaptionStore } from "../store/useCaptionStore";
import { StoreContext } from "../store/StoreContext";
import type { WordTiming, CaptionStyle, FrameAtlasData } from "../types";
import { DEFAULT_STYLE } from "../constants";

// Per-instance stores keyed by nodeId
const stores = new Map<string, CaptionStore>();
const roots = new Map<string, ReactDOM.Root>();

// Widget references for syncing back to ComfyUI
let _editedWidget: { value: string } | null = null;
let _styleWidget: { value: string } | null = null;

// Debounce timer for widget sync
let _syncTimer: ReturnType<typeof setTimeout> | null = null;

// Currently active store (last one that received data)
let _activeNodeId: string | null = null;

function getOrCreateStore(nodeId: string): CaptionStore {
  let store = stores.get(nodeId);
  if (!store) {
    store = createCaptionStore();
    stores.set(nodeId, store);
  }
  return store;
}

/** Set widget references for syncing back to ComfyUI hidden widgets. */
export function setWidgetRefs(
  editedWidget: { value: string } | null,
  styleWidget: { value: string } | null
) {
  _editedWidget = editedWidget;
  _styleWidget = styleWidget;
}

/** Mount the React app inside a container for a specific node instance. */
export function mountCaptionStudio(container: HTMLElement, nodeId: string) {
  if (roots.has(nodeId)) return;

  const store = getOrCreateStore(nodeId);
  _activeNodeId = nodeId;

  // Clear placeholder content
  container.innerHTML = "";

  const root = ReactDOM.createRoot(container);
  roots.set(nodeId, root);

  // Subscribe to store changes and sync to widgets with debounce
  store.subscribe(() => {
    scheduleSyncToWidgets(nodeId);
  });

  root.render(
    React.createElement(
      StoreContext.Provider,
      { value: store },
      React.createElement(App)
    )
  );
}

/** Receive output data from Python execution and push into the store. */
export function receiveFromPython(output: Record<string, unknown[]>) {
  if (!_activeNodeId) return;
  const store = stores.get(_activeNodeId);
  if (!store) return;

  try {
    const timingsRaw = (output.timings_json as string[])?.[0];
    const editedRaw = (output.edited_timings_json as string[])?.[0];
    const styleRaw = (output.style_json as string[])?.[0];
    const presetsRaw = (output.style_presets as string[])?.[0];
    const fps = (output.fps as number[])?.[0] ?? 30;
    const frameWidth = (output.frame_width as number[])?.[0] ?? 1920;
    const frameHeight = (output.frame_height as number[])?.[0] ?? 1080;
    const frameCount = (output.frame_count as number[])?.[0] ?? 0;
    const previewFrame = (output.preview_frame as string[])?.[0] ?? "";
    const fontListRaw = (output.font_list as string[])?.[0];
    const frameAtlasRaw = (output.frame_atlas as string[])?.[0];
    const audioData = (output.audio_data as string[])?.[0] ?? "";

    const originalTimings: WordTiming[] = timingsRaw
      ? JSON.parse(timingsRaw)
      : [];

    let editedTimings: WordTiming[] = [];
    if (editedRaw && editedRaw.trim()) {
      try {
        editedTimings = JSON.parse(editedRaw);
      } catch {
        editedTimings = [];
      }
    }

    const style: CaptionStyle = styleRaw
      ? JSON.parse(styleRaw)
      : { ...DEFAULT_STYLE };

    // Detect preset name from style
    let presetName = "Custom";
    if (presetsRaw) {
      try {
        const presets = JSON.parse(presetsRaw);
        for (const [name, preset] of Object.entries(presets)) {
          if (JSON.stringify(preset) === JSON.stringify(style)) {
            presetName = name;
            break;
          }
        }
      } catch {
        // ignore
      }
    }

    const fontList: string[] = fontListRaw ? JSON.parse(fontListRaw) : [];

    // Parse frame atlas if available
    let frameAtlas: FrameAtlasData | null = null;
    if (frameAtlasRaw && frameAtlasRaw.trim()) {
      try {
        frameAtlas = JSON.parse(frameAtlasRaw);
      } catch {
        // ignore
      }
    }

    store.getState().initFromPython({
      originalTimings,
      editedTimings,
      style,
      presetName,
      fps,
      frameWidth,
      frameHeight,
      frameCount,
      previewFrameBase64: previewFrame,
      fontList,
      frameAtlas,
      audioDataUrl: audioData ? `data:audio/wav;base64,${audioData}` : "",
    });
  } catch (err) {
    console.error("[Ordo] Caption Studio: failed to parse Python output", err);
  }
}

/** Sync store state to ComfyUI hidden widgets (debounced). */
function scheduleSyncToWidgets(nodeId: string) {
  if (_syncTimer) clearTimeout(_syncTimer);
  _syncTimer = setTimeout(() => {
    syncToWidgets(nodeId);
  }, 300);
}

function syncToWidgets(nodeId: string) {
  const store = stores.get(nodeId);
  if (!store) return;

  const state = store.getState();

  if (_editedWidget) {
    _editedWidget.value = state.getEditedTimingsJSON();
  }
  if (_styleWidget) {
    _styleWidget.value = state.getStyleConfigJSON();
  }
}
