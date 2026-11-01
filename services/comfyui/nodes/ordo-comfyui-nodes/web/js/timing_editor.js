const { app } = window.comfyAPI.app;

const SEGMENT_COLORS = [
    "#4a9eff", "#4adf7f", "#f0a040", "#e05577", "#aa66dd", "#44ccbb",
];

const TRACK_COLORS = [
    "#4a9eff", "#4adf7f", "#f0a040", "#e05577", "#aa66dd", "#44ccbb",
];

function chainCallback(object, property, callback) {
    if (object == undefined) {
        console.error("Tried to add callback to non-existent object");
        return;
    }
    if (property in object) {
        const orig = object[property];
        object[property] = function () {
            const r = orig.apply(this, arguments);
            callback.apply(this, arguments);
            return r;
        };
    } else {
        object[property] = callback;
    }
}

app.registerExtension({
    name: "Ordo.TimingEditor",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData?.name !== "OrdoTimingEditor") return;

        chainCallback(nodeType.prototype, "onNodeCreated", function () {
            // Hide the edited_timings widget
            const editedWidget = this.widgets.find(
                (w) => w.name === "edited_timings"
            );
            if (editedWidget) {
                editedWidget.hidden = true;
                editedWidget.computeSize = () => [0, -4];
                editedWidget.type = "converted-widget";
            }

            // Create container div
            const container = document.createElement("div");
            container.style.cssText = `
                width: 100%;
                background: #1a1a2e;
                border-radius: 6px;
                overflow: hidden;
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                font-size: 11px;
                color: #ccc;
            `;
            container.innerHTML = `<div style="padding:12px;color:#666;text-align:center;">
                Queue the workflow to populate the timeline.</div>`;

            // Store references on the node
            this._timingContainer = container;
            this._editedWidget = editedWidget;
            this._timingData = null;

            // Initialize persistent editor state
            this._editorState = {
                zoomLevel: 1.0,
                scrollLeft: 0,
                selectedWordIndex: null,
                trackCount: 1,
            };

            // Add as DOM widget
            this.addDOMWidget(
                "timing_editor",
                "TimingEditorWidget",
                container,
                {
                    serialize: false,
                    hideOnZoom: false,
                }
            );

            // Listen for execution results
            const self = this;
            chainCallback(this, "onExecuted", function (output) {
                if (!output) return;
                const timingsRaw = output.timings_json?.[0];
                const fps = output.fps?.[0] || 30;
                if (!timingsRaw) return;

                try {
                    const timings = JSON.parse(timingsRaw);
                    if (!Array.isArray(timings) || timings.length === 0) return;

                    // If user has edits, merge positions from edited into display
                    let displayTimings = timings;
                    if (
                        self._editedWidget &&
                        self._editedWidget.value &&
                        self._editedWidget.value.trim()
                    ) {
                        try {
                            const edited = JSON.parse(self._editedWidget.value);
                            if (Array.isArray(edited) && edited.length > 0) {
                                displayTimings = edited;
                            }
                        } catch (_) {
                            /* use original */
                        }
                    }

                    self._timingData = timings; // original from Python

                    // Update trackCount from data
                    const maxTrack = Math.max(0, ...displayTimings.map(w => w.track || 0));
                    if (maxTrack + 1 > self._editorState.trackCount) {
                        self._editorState.trackCount = maxTrack + 1;
                    }

                    renderTimeline(self, displayTimings, fps);
                } catch (e) {
                    console.error("Ordo TimingEditor: failed to parse timings", e);
                }
            });
        });
    },
});

// ────────────────────────────────────────────────────────────────────────
// Timeline rendering
// ────────────────────────────────────────────────────────────────────────

function renderTimeline(node, timings, fps) {
    const container = node._timingContainer;
    if (!container) return;

    const state = node._editorState;

    // Compute layout parameters
    const minTime = Math.min(...timings.map((w) => w.start));
    const maxTime = Math.max(...timings.map((w) => w.end));
    const totalDuration = maxTime - minTime;

    // Group by track → segment_idx
    const trackMap = new Map(); // track -> Map(segment_idx -> words[])
    timings.forEach((w, i) => {
        const track = w.track || 0;
        const segIdx = w.segment_idx ?? -1;
        if (!trackMap.has(track)) trackMap.set(track, new Map());
        const segMap = trackMap.get(track);
        if (!segMap.has(segIdx)) segMap.set(segIdx, []);
        segMap.get(segIdx).push({ ...w, _index: i });
    });

    // Ensure all tracks up to trackCount exist in the map
    for (let t = 0; t < state.trackCount; t++) {
        if (!trackMap.has(t)) trackMap.set(t, new Map());
    }

    const trackKeys = [...trackMap.keys()].sort((a, b) => a - b);

    const TRACK_HEIGHT = 32;
    const RULER_HEIGHT = 28;
    const PADDING = 8;
    const HANDLE_WIDTH = 6;
    const TRACK_HEADER_HEIGHT = 20;

    const containerWidth = container.clientWidth || 380;
    const basePps = Math.max(80, containerWidth / (totalDuration || 1));
    const pps = basePps * state.zoomLevel;
    const contentWidth = Math.max(containerWidth - PADDING * 2, pps * (totalDuration || 1));

    // Build HTML
    container.innerHTML = "";
    container.style.position = "relative";

    // ── Toolbar ──────────────────────────────────────────────────────
    const toolbar = document.createElement("div");
    toolbar.style.cssText = `
        display: flex; align-items: center; gap: 6px;
        padding: 4px 8px; border-bottom: 1px solid #333;
        background: #16162a;
    `;
    container.appendChild(toolbar);

    // Zoom controls
    const zoomLabel = document.createElement("span");
    zoomLabel.style.cssText = "color: #888; font-size: 10px; margin-right: 2px;";
    zoomLabel.textContent = "Zoom:";
    toolbar.appendChild(zoomLabel);

    const zoomOutBtn = createButton("\u2212", () => {
        state.zoomLevel = Math.max(0.5, state.zoomLevel / 1.2);
        renderTimeline(node, timings, fps);
    });
    zoomOutBtn.style.cssText += "width:24px; height:20px; padding:0; font-size:14px; line-height:18px;";
    toolbar.appendChild(zoomOutBtn);

    const zoomDisplay = document.createElement("span");
    zoomDisplay.style.cssText = "color: #aaa; font-size: 10px; min-width: 36px; text-align: center;";
    zoomDisplay.textContent = `${Math.round(state.zoomLevel * 100)}%`;
    toolbar.appendChild(zoomDisplay);

    const zoomInBtn = createButton("+", () => {
        state.zoomLevel = Math.min(10.0, state.zoomLevel * 1.2);
        renderTimeline(node, timings, fps);
    });
    zoomInBtn.style.cssText += "width:24px; height:20px; padding:0; font-size:14px; line-height:18px;";
    toolbar.appendChild(zoomInBtn);

    const zoomResetBtn = createButton("Reset", () => {
        state.zoomLevel = 1.0;
        state.scrollLeft = 0;
        renderTimeline(node, timings, fps);
    });
    zoomResetBtn.style.fontSize = "9px";
    toolbar.appendChild(zoomResetBtn);

    // Spacer
    const spacer = document.createElement("div");
    spacer.style.flex = "1";
    toolbar.appendChild(spacer);

    // Info label
    const infoLabel = document.createElement("span");
    infoLabel.style.cssText = "color: #666; font-size: 10px;";
    const totalSegs = new Set(timings.map(w => w.segment_idx ?? -1)).size;
    infoLabel.textContent = `${timings.length} words \u00b7 ${totalSegs} segments \u00b7 ${totalDuration.toFixed(1)}s`;
    toolbar.appendChild(infoLabel);

    // ── Scroll wrapper ───────────────────────────────────────────────
    const scrollWrap = document.createElement("div");
    scrollWrap.style.cssText = `
        overflow-x: auto; overflow-y: hidden;
        width: 100%; position: relative;
        padding: ${PADDING}px;
    `;
    container.appendChild(scrollWrap);

    // Ctrl+scroll for zoom
    scrollWrap.addEventListener("wheel", (e) => {
        if (e.ctrlKey || e.metaKey) {
            e.preventDefault();
            e.stopPropagation();

            const rect = scrollWrap.getBoundingClientRect();
            const mouseX = e.clientX - rect.left + scrollWrap.scrollLeft;
            const mouseTime = mouseX / pps;

            const oldZoom = state.zoomLevel;
            if (e.deltaY < 0) {
                state.zoomLevel = Math.min(10.0, state.zoomLevel * 1.1);
            } else {
                state.zoomLevel = Math.max(0.5, state.zoomLevel / 1.1);
            }

            // Keep cursor position centered
            const newPps = basePps * state.zoomLevel;
            state.scrollLeft = mouseTime * newPps - (e.clientX - rect.left);
            state.scrollLeft = Math.max(0, state.scrollLeft);

            renderTimeline(node, timings, fps);
        }
    }, { passive: false });

    // Save scroll position on scroll
    scrollWrap.addEventListener("scroll", () => {
        state.scrollLeft = scrollWrap.scrollLeft;
    });

    // Calculate total height for tracks
    let totalTrackHeight = RULER_HEIGHT + 4;
    trackKeys.forEach((trackId) => {
        const segMap = trackMap.get(trackId);
        const segCount = Math.max(1, segMap.size);
        totalTrackHeight += TRACK_HEADER_HEIGHT + segCount * (TRACK_HEIGHT + 4) + 4;
    });

    // Inner content sized to full timeline
    const inner = document.createElement("div");
    inner.style.cssText = `
        position: relative;
        width: ${contentWidth}px;
        min-height: ${totalTrackHeight + 60}px;
    `;
    scrollWrap.appendChild(inner);

    // ── Time ruler ──────────────────────────────────────────────────────
    const ruler = document.createElement("div");
    ruler.style.cssText = `
        position: relative; height: ${RULER_HEIGHT}px;
        border-bottom: 1px solid #333; margin-bottom: 4px;
    `;
    inner.appendChild(ruler);

    const tickInterval = computeTickInterval(maxTime - minTime);
    for (
        let t = Math.ceil(minTime / tickInterval) * tickInterval;
        t <= maxTime;
        t += tickInterval
    ) {
        const x = (t - minTime) * pps;
        const tick = document.createElement("div");
        tick.style.cssText = `
            position: absolute; left: ${x}px; top: 14px;
            width: 1px; height: 12px; background: #555;
        `;
        ruler.appendChild(tick);
        const label = document.createElement("div");
        label.style.cssText = `
            position: absolute; left: ${x}px; top: 0; transform: translateX(-50%);
            font-size: 9px; color: #888; white-space: nowrap;
        `;
        label.textContent = formatTime(t);
        ruler.appendChild(label);
    }

    // ── Track rows ──────────────────────────────────────────────────────
    const allBlocks = [];
    let currentY = RULER_HEIGHT + 4;

    trackKeys.forEach((trackId) => {
        const segMap = trackMap.get(trackId);
        const segKeys = [...segMap.keys()].sort((a, b) => a - b);
        const trackColor = TRACK_COLORS[trackId % TRACK_COLORS.length];

        // Track header
        const trackHeader = document.createElement("div");
        trackHeader.style.cssText = `
            position: absolute; left: 0; top: ${currentY}px;
            width: ${contentWidth}px; height: ${TRACK_HEADER_HEIGHT}px;
            border-left: 3px solid ${trackColor};
            padding-left: 6px;
            display: flex; align-items: center;
            font-size: 9px; color: ${trackColor}; font-weight: 600;
        `;
        trackHeader.textContent = `Track ${trackId + 1}`;
        inner.appendChild(trackHeader);
        currentY += TRACK_HEADER_HEIGHT;

        if (segKeys.length === 0) {
            // Empty track — render empty lane
            const emptyRow = document.createElement("div");
            emptyRow.style.cssText = `
                position: absolute; left: 0; top: ${currentY}px;
                width: ${contentWidth}px; height: ${TRACK_HEIGHT}px;
                border-bottom: 1px solid #222;
                margin-bottom: 4px;
            `;
            inner.appendChild(emptyRow);
            currentY += TRACK_HEIGHT + 4;
        } else {
            segKeys.forEach((segIdx, rowIdx) => {
                const words = segMap.get(segIdx);
                const color = SEGMENT_COLORS[segIdx % SEGMENT_COLORS.length];

                const row = document.createElement("div");
                row.style.cssText = `
                    position: absolute; left: 0; top: ${currentY}px;
                    width: ${contentWidth}px; height: ${TRACK_HEIGHT}px;
                    margin-bottom: 4px;
                `;
                inner.appendChild(row);

                // Segment label
                const segLabel = document.createElement("div");
                segLabel.style.cssText = `
                    position: absolute; left: -2px; top: 0;
                    font-size: 8px; color: ${color}; opacity: 0.6;
                `;
                segLabel.textContent = segIdx >= 0 ? `seg ${segIdx}` : "ungrouped";
                row.appendChild(segLabel);

                words.forEach((w) => {
                    const block = createWordBlock(
                        w, minTime, pps, TRACK_HEIGHT, HANDLE_WIDTH,
                        color, node, allBlocks, timings, fps, state
                    );
                    allBlocks.push({ el: block, data: w });
                    row.appendChild(block);
                });

                currentY += TRACK_HEIGHT + 4;
            });
        }

        currentY += 4; // gap between tracks
    });

    // ── Add Track button ─────────────────────────────────────────────
    const addTrackBtn = createButton("+ Add Track", () => {
        state.trackCount++;
        renderTimeline(node, timings, fps);
    });
    addTrackBtn.style.cssText += `
        position: absolute; left: 0; top: ${currentY}px;
        font-size: 10px; padding: 2px 10px;
    `;
    inner.appendChild(addTrackBtn);
    currentY += 28;

    // ── Tooltip ─────────────────────────────────────────────────────
    if (!node._tooltip) {
        const tooltip = document.createElement("div");
        tooltip.style.cssText = `
            display: none; position: fixed;
            background: #222; color: #fff; padding: 4px 8px;
            border-radius: 4px; font-size: 10px; pointer-events: none;
            z-index: 9999; white-space: nowrap;
            box-shadow: 0 2px 8px rgba(0,0,0,0.5);
        `;
        document.body.appendChild(tooltip);
        node._tooltip = tooltip;
    }

    // ── Property Panel ───────────────────────────────────────────────
    const propertyPanel = document.createElement("div");
    propertyPanel.style.cssText = `
        display: none; padding: 8px; border-top: 1px solid #333;
        background: #16162a;
    `;
    propertyPanel.id = "ordo-property-panel";
    container.appendChild(propertyPanel);
    node._propertyPanel = propertyPanel;

    // If a word is selected, show it
    if (state.selectedWordIndex !== null) {
        const selWord = timings[state.selectedWordIndex];
        if (selWord) {
            showPropertyPanel(node, selWord, state.selectedWordIndex, timings, fps, allBlocks);
            // Highlight selected block
            const selBlock = allBlocks.find(b => b.data._index === state.selectedWordIndex);
            if (selBlock) {
                selBlock.el.style.outline = "2px solid #fff";
            }
        }
    }

    // ── Bottom buttons ───────────────────────────────────────────────
    const btnRow = document.createElement("div");
    btnRow.style.cssText = `
        padding: 4px 8px; display: flex; gap: 8px; align-items: center;
        border-top: 1px solid #333;
    `;
    container.appendChild(btnRow);

    const resetBtn = createButton("Reset Edits", () => {
        if (node._editedWidget) {
            node._editedWidget.value = "";
        }
        state.selectedWordIndex = null;
        state.trackCount = 1;
        if (node._timingData) {
            renderTimeline(node, node._timingData, fps);
        }
    });
    btnRow.appendChild(resetBtn);

    // ── Context menu (hidden, shown on right-click) ──────────────────
    let contextMenu = document.getElementById("ordo-context-menu");
    if (!contextMenu) {
        contextMenu = document.createElement("div");
        contextMenu.id = "ordo-context-menu";
        contextMenu.style.cssText = `
            display: none; position: fixed;
            background: #2a2a3e; border: 1px solid #555;
            border-radius: 4px; padding: 4px 0;
            z-index: 10000; min-width: 120px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.6);
        `;
        document.body.appendChild(contextMenu);
    }
    node._contextMenu = contextMenu;

    // Close context menu on click anywhere
    document.addEventListener("click", () => {
        if (node._contextMenu) node._contextMenu.style.display = "none";
    }, { once: false });

    // Restore scroll position and resize (only grow, never shrink)
    requestAnimationFrame(() => {
        scrollWrap.scrollLeft = state.scrollLeft;
        const newSize = node.computeSize();
        const curSize = node.size;
        if (curSize) {
            newSize[0] = Math.max(newSize[0], curSize[0]);
            newSize[1] = Math.max(newSize[1], curSize[1]);
        }
        node.setSize?.(newSize);
        app.graph?.setDirtyCanvas(true, true);
    });
}

// ────────────────────────────────────────────────────────────────────────
// Word block creation + drag logic
// ────────────────────────────────────────────────────────────────────────

function createWordBlock(
    wordData, minTime, pps, trackHeight, handleWidth,
    color, node, allBlocks, timings, fps, state
) {
    const left = (wordData.start - minTime) * pps;
    const width = Math.max((wordData.end - wordData.start) * pps, 12);

    const isSelected = state.selectedWordIndex === wordData._index;
    const isHidden = wordData.override_hidden || false;

    const block = document.createElement("div");
    block.style.cssText = `
        position: absolute;
        left: ${left}px;
        top: 4px;
        width: ${width}px;
        height: ${trackHeight - 8}px;
        background: ${isHidden ? "#333" : color};
        border-radius: 3px;
        cursor: grab;
        display: flex;
        align-items: center;
        justify-content: center;
        overflow: hidden;
        user-select: none;
        box-sizing: border-box;
        transition: box-shadow 0.1s;
        opacity: ${isHidden ? "0.4" : "1"};
        ${isSelected ? "outline: 2px solid #fff;" : ""}
    `;

    // Word text
    const text = document.createElement("span");
    text.style.cssText = `
        color: #fff; font-size: 10px; font-weight: 600;
        pointer-events: none; white-space: nowrap;
        text-overflow: ellipsis; overflow: hidden;
        padding: 0 ${handleWidth + 2}px 0 4px;
    `;
    const displayText = wordData.override_text || wordData.word;
    text.textContent = displayText;
    block.appendChild(text);

    // Override indicator dots
    const hasOverrides = wordData.override_color || wordData.override_highlight ||
        wordData.override_scale || wordData.override_font_size ||
        wordData.override_stroke_width || wordData.override_stroke_color ||
        wordData.override_text || wordData.override_hidden;
    if (hasOverrides) {
        const dot = document.createElement("div");
        dot.style.cssText = `
            position: absolute; top: 2px; right: ${handleWidth + 2}px;
            width: 5px; height: 5px; border-radius: 50%;
            background: #ffcc00;
        `;
        block.appendChild(dot);
    }

    // Right resize handle
    const handle = document.createElement("div");
    handle.style.cssText = `
        position: absolute; right: 0; top: 0;
        width: ${handleWidth}px; height: 100%;
        cursor: ew-resize;
        background: rgba(255,255,255,0.15);
        border-radius: 0 3px 3px 0;
    `;
    block.appendChild(handle);

    // Hover tooltip
    block.addEventListener("mouseenter", (e) => {
        const tip = node._tooltip;
        if (tip) {
            tip.textContent = `"${displayText}" ${wordData.start.toFixed(3)}s \u2192 ${wordData.end.toFixed(3)}s`;
            tip.style.display = "block";
        }
        if (!isSelected) block.style.boxShadow = "0 0 0 2px rgba(255,255,255,0.4)";
    });
    block.addEventListener("mousemove", (e) => {
        const tip = node._tooltip;
        if (tip && tip.style.display !== "none") {
            tip.style.left = e.clientX + 12 + "px";
            tip.style.top = e.clientY - 28 + "px";
        }
    });
    block.addEventListener("mouseleave", () => {
        if (node._tooltip) node._tooltip.style.display = "none";
        if (!isSelected) block.style.boxShadow = "none";
    });

    // ── Right-click: Track context menu ──────────────────────────────
    block.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        e.stopPropagation();

        const menu = node._contextMenu;
        if (!menu) return;

        menu.innerHTML = "";

        const menuTitle = document.createElement("div");
        menuTitle.style.cssText = "padding: 4px 12px; color: #888; font-size: 9px; border-bottom: 1px solid #444;";
        menuTitle.textContent = `"${wordData.word}" \u2014 Move to:`;
        menu.appendChild(menuTitle);

        for (let t = 0; t < state.trackCount; t++) {
            const currentTrack = wordData.track || 0;
            const item = document.createElement("div");
            item.style.cssText = `
                padding: 4px 12px; cursor: pointer; font-size: 11px;
                color: ${t === currentTrack ? "#666" : "#ccc"};
                ${t === currentTrack ? "font-style: italic;" : ""}
            `;
            item.textContent = `Track ${t + 1}${t === currentTrack ? " (current)" : ""}`;
            if (t !== currentTrack) {
                item.addEventListener("mouseenter", () => { item.style.background = "#3a3a5e"; });
                item.addEventListener("mouseleave", () => { item.style.background = "none"; });
                item.addEventListener("click", (ev) => {
                    ev.stopPropagation();
                    wordData.track = t;
                    // Update in the timings array as well
                    timings[wordData._index].track = t;
                    menu.style.display = "none";
                    serializeEdits(node, allBlocks);
                    renderTimeline(node, timings, fps);
                });
            }
            menu.appendChild(item);
        }

        menu.style.display = "block";
        menu.style.left = e.clientX + "px";
        menu.style.top = e.clientY + "px";
    });

    // ── Click: Select word ───────────────────────────────────────────
    let wasDragged = false;
    let mouseDownPos = null;

    // ── Drag: move entire block ─────────────────────────────────────
    block.addEventListener("mousedown", (e) => {
        if (e.target === handle) return;
        if (e.button === 2) return; // right-click handled by contextmenu
        e.preventDefault();
        e.stopPropagation();

        wasDragged = false;
        mouseDownPos = { x: e.clientX, y: e.clientY };

        block.style.cursor = "grabbing";
        block.style.zIndex = "10";

        const startX = e.clientX;
        const origLeft = parseFloat(block.style.left);
        const duration = wordData.end - wordData.start;

        function onMove(ev) {
            const dx = ev.clientX - startX;
            if (Math.abs(dx) > 3) wasDragged = true;

            const newLeft = Math.max(0, origLeft + dx);
            block.style.left = newLeft + "px";

            wordData.start = newLeft / pps + minTime;
            wordData.end = wordData.start + duration;

            const tip = node._tooltip;
            if (tip && tip.style.display !== "none") {
                const dt = wordData.override_text || wordData.word;
                tip.textContent = `"${dt}" ${wordData.start.toFixed(3)}s \u2192 ${wordData.end.toFixed(3)}s`;
                tip.style.left = ev.clientX + 12 + "px";
                tip.style.top = ev.clientY - 28 + "px";
            }
        }
        function onUp(ev) {
            document.removeEventListener("mousemove", onMove);
            document.removeEventListener("mouseup", onUp);
            block.style.cursor = "grab";
            block.style.zIndex = "";

            if (!wasDragged) {
                // It was a click, not a drag — select the word
                state.selectedWordIndex = wordData._index;
                renderTimeline(node, timings, fps);
            } else {
                serializeEdits(node, allBlocks);
            }
        }
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
    });

    // ── Drag: resize right edge ─────────────────────────────────────
    handle.addEventListener("mousedown", (e) => {
        e.preventDefault();
        e.stopPropagation();

        const startX = e.clientX;
        const origWidth = parseFloat(block.style.width);

        function onMove(ev) {
            const dx = ev.clientX - startX;
            const newWidth = Math.max(12, origWidth + dx);
            block.style.width = newWidth + "px";

            wordData.end = wordData.start + newWidth / pps;

            const tip = node._tooltip;
            if (tip && tip.style.display !== "none") {
                const dt = wordData.override_text || wordData.word;
                tip.textContent = `"${dt}" ${wordData.start.toFixed(3)}s \u2192 ${wordData.end.toFixed(3)}s`;
                tip.style.left = ev.clientX + 12 + "px";
                tip.style.top = ev.clientY - 28 + "px";
            }
        }
        function onUp() {
            document.removeEventListener("mousemove", onMove);
            document.removeEventListener("mouseup", onUp);
            serializeEdits(node, allBlocks);
        }
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
    });

    return block;
}

// ────────────────────────────────────────────────────────────────────────
// Property Panel
// ────────────────────────────────────────────────────────────────────────

function showPropertyPanel(node, wordData, wordIndex, timings, fps, allBlocks) {
    const panel = node._propertyPanel;
    if (!panel) return;

    panel.style.display = "block";
    panel.innerHTML = "";

    // Title
    const title = document.createElement("div");
    title.style.cssText = "font-size: 11px; font-weight: 600; color: #ddd; margin-bottom: 6px;";
    title.textContent = `Word: "${wordData.word}" (${wordData.start.toFixed(3)}s \u2013 ${wordData.end.toFixed(3)}s)`;
    panel.appendChild(title);

    // Grid
    const grid = document.createElement("div");
    grid.style.cssText = `
        display: grid; grid-template-columns: auto 1fr;
        gap: 4px 8px; align-items: center;
        font-size: 10px;
    `;
    panel.appendChild(grid);

    // Helper to update word data and serialize
    function update(field, value) {
        wordData[field] = value;
        timings[wordIndex][field] = value;
        serializeEdits(node, allBlocks);
    }

    // Display Text
    addGridRow(grid, "Display Text",
        createInput("text", wordData.override_text || "", (val) => {
            update("override_text", val || undefined);
        })
    );

    // Fill Color
    addGridRow(grid, "Fill Color",
        createColorPicker(wordData.override_color || "#FFFFFF", (val) => {
            update("override_color", val === "#FFFFFF" || val === "#ffffff" ? undefined : val);
        })
    );

    // Highlight Color
    addGridRow(grid, "Highlight Color",
        createColorPicker(wordData.override_highlight || "#00FFFF", (val) => {
            update("override_highlight", val === "#00FFFF" || val === "#00ffff" ? undefined : val);
        })
    );

    // Scale
    addGridRow(grid, "Scale",
        createSlider(0.5, 3.0, 0.1, wordData.override_scale || 1.0, (val) => {
            update("override_scale", val === 1.0 ? undefined : val);
        })
    );

    // Font Size
    addGridRow(grid, "Font Size",
        createInput("number", wordData.override_font_size || "", (val) => {
            update("override_font_size", val ? parseInt(val) : undefined);
        })
    );

    // Stroke Width
    addGridRow(grid, "Stroke Width",
        createInput("number", wordData.override_stroke_width != null ? wordData.override_stroke_width : "", (val) => {
            update("override_stroke_width", val !== "" ? parseInt(val) : undefined);
        })
    );

    // Stroke Color
    addGridRow(grid, "Stroke Color",
        createColorPicker(wordData.override_stroke_color || "#000000", (val) => {
            update("override_stroke_color", val === "#000000" || val === "#000000" ? undefined : val);
        })
    );

    // Hidden
    addGridRow(grid, "Hidden",
        createCheckbox(wordData.override_hidden || false, (val) => {
            update("override_hidden", val || undefined);
        })
    );

    // Buttons row
    const btnRow = document.createElement("div");
    btnRow.style.cssText = "margin-top: 6px; display: flex; gap: 6px;";
    panel.appendChild(btnRow);

    const clearBtn = createButton("Clear Overrides", () => {
        const fields = [
            "override_text", "override_color", "override_highlight",
            "override_scale", "override_font_size", "override_stroke_width",
            "override_stroke_color", "override_hidden"
        ];
        fields.forEach(f => {
            delete wordData[f];
            delete timings[wordIndex][f];
        });
        serializeEdits(node, allBlocks);
        renderTimeline(node, timings, fps);
    });
    btnRow.appendChild(clearBtn);

    const deselectBtn = createButton("Deselect", () => {
        node._editorState.selectedWordIndex = null;
        renderTimeline(node, timings, fps);
    });
    btnRow.appendChild(deselectBtn);
}

function addGridRow(grid, label, inputEl) {
    const labelEl = document.createElement("span");
    labelEl.style.cssText = "color: #999; text-align: right; padding-right: 4px;";
    labelEl.textContent = label;
    grid.appendChild(labelEl);
    grid.appendChild(inputEl);
}

// ────────────────────────────────────────────────────────────────────────
// Serialize edits to hidden widget
// ────────────────────────────────────────────────────────────────────────

function serializeEdits(node, allBlocks) {
    if (!node._editedWidget) return;

    const edited = allBlocks.map((b) => {
        const d = b.data;
        const entry = {
            word: d.word,
            start: Math.round(d.start * 1000) / 1000,
            end: Math.round(d.end * 1000) / 1000,
            segment_idx: d.segment_idx ?? -1,
            track: d.track || 0,
        };

        // Only include non-default override values to keep JSON compact
        if (d.override_text) entry.override_text = d.override_text;
        if (d.override_color) entry.override_color = d.override_color;
        if (d.override_highlight) entry.override_highlight = d.override_highlight;
        if (d.override_scale && d.override_scale !== 1.0) entry.override_scale = d.override_scale;
        if (d.override_font_size) entry.override_font_size = d.override_font_size;
        if (d.override_stroke_width != null && d.override_stroke_width !== undefined) {
            entry.override_stroke_width = d.override_stroke_width;
        }
        if (d.override_stroke_color) entry.override_stroke_color = d.override_stroke_color;
        if (d.override_hidden) entry.override_hidden = d.override_hidden;

        return entry;
    });

    node._editedWidget.value = JSON.stringify(edited);
}

// ────────────────────────────────────────────────────────────────────────
// UI Helper Functions
// ────────────────────────────────────────────────────────────────────────

function createInput(type, value, onChange) {
    const input = document.createElement("input");
    input.type = type;
    input.value = value;
    input.style.cssText = `
        background: #2a2a3e; color: #ddd; border: 1px solid #555;
        border-radius: 3px; padding: 2px 6px; font-size: 10px;
        width: 100%; box-sizing: border-box; outline: none;
    `;
    if (type === "number") {
        input.style.width = "80px";
        input.min = "0";
    }
    input.addEventListener("change", () => onChange(input.value));
    return input;
}

function createColorPicker(value, onChange) {
    const wrapper = document.createElement("div");
    wrapper.style.cssText = "display: flex; align-items: center; gap: 4px;";

    const picker = document.createElement("input");
    picker.type = "color";
    picker.value = value;
    picker.style.cssText = `
        width: 28px; height: 20px; border: 1px solid #555;
        border-radius: 3px; cursor: pointer; padding: 0;
        background: none;
    `;
    picker.addEventListener("input", () => {
        hexInput.value = picker.value;
        onChange(picker.value);
    });
    wrapper.appendChild(picker);

    const hexInput = document.createElement("input");
    hexInput.type = "text";
    hexInput.value = value;
    hexInput.style.cssText = `
        background: #2a2a3e; color: #ddd; border: 1px solid #555;
        border-radius: 3px; padding: 2px 4px; font-size: 9px;
        width: 70px; font-family: monospace;
    `;
    hexInput.addEventListener("change", () => {
        const v = hexInput.value;
        if (/^#[0-9a-fA-F]{6}$/.test(v)) {
            picker.value = v;
            onChange(v);
        }
    });
    wrapper.appendChild(hexInput);

    return wrapper;
}

function createSlider(min, max, step, value, onChange) {
    const wrapper = document.createElement("div");
    wrapper.style.cssText = "display: flex; align-items: center; gap: 6px;";

    const slider = document.createElement("input");
    slider.type = "range";
    slider.min = min;
    slider.max = max;
    slider.step = step;
    slider.value = value;
    slider.style.cssText = "flex: 1; height: 14px; cursor: pointer;";

    const display = document.createElement("span");
    display.style.cssText = "color: #aaa; font-size: 10px; min-width: 32px; text-align: right;";
    display.textContent = parseFloat(value).toFixed(1);

    slider.addEventListener("input", () => {
        const v = parseFloat(slider.value);
        display.textContent = v.toFixed(1);
        onChange(v);
    });

    wrapper.appendChild(slider);
    wrapper.appendChild(display);
    return wrapper;
}

function createCheckbox(checked, onChange) {
    const wrapper = document.createElement("div");
    wrapper.style.cssText = "display: flex; align-items: center;";

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = checked;
    cb.style.cssText = "cursor: pointer; margin: 0;";
    cb.addEventListener("change", () => onChange(cb.checked));

    wrapper.appendChild(cb);
    return wrapper;
}

function createButton(label, onClick) {
    const btn = document.createElement("button");
    btn.textContent = label;
    btn.style.cssText = `
        background: #333; color: #ccc; border: 1px solid #555;
        border-radius: 4px; padding: 3px 10px; cursor: pointer;
        font-size: 10px; white-space: nowrap;
    `;
    btn.addEventListener("mouseenter", () => { btn.style.background = "#444"; });
    btn.addEventListener("mouseleave", () => { btn.style.background = "#333"; });
    btn.addEventListener("click", onClick);
    return btn;
}

// ────────────────────────────────────────────────────────────────────────
// Helpers
// ────────────────────────────────────────────────────────────────────────

function computeTickInterval(duration) {
    if (duration <= 2) return 0.1;
    if (duration <= 5) return 0.25;
    if (duration <= 15) return 0.5;
    if (duration <= 60) return 1;
    if (duration <= 300) return 5;
    return 10;
}

function formatTime(seconds) {
    if (seconds < 60) return seconds.toFixed(1) + "s";
    const m = Math.floor(seconds / 60);
    const s = (seconds % 60).toFixed(1);
    return `${m}:${s.padStart(4, "0")}`;
}
