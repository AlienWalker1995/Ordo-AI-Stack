import { app } from "../../scripts/app.js";

const STYLE_PRESETS = {
    "Hormozi": {
        fill_color: "#FFFFFF",
        highlight_colors: ["#00FFFF", "#00FF00", "#FFFF00", "#FF4444"],
        stroke_color: "#000000", stroke_width: 8,
        shadow_color: "#000000", shadow_opacity: 0.6, shadow_blur: 6.0,
        font_size: 120, uppercase: true,
    },
    "MrBeast": {
        fill_color: "#FFFFFF",
        highlight_colors: ["#FFD700", "#FF4444", "#00FF00", "#FFFFFF"],
        stroke_color: "#000000", stroke_width: 12,
        shadow_color: "#000000", shadow_opacity: 0.6, shadow_blur: 8.0,
        font_size: 160, uppercase: true,
    },
    "Clean": {
        fill_color: "#FFFFFF",
        highlight_colors: ["#87CEEB", "#90EE90", "#FFFF99", "#FFB6C1"],
        stroke_color: "#000000", stroke_width: 0,
        shadow_color: "#000000", shadow_opacity: 0.5, shadow_blur: 12.0,
        font_size: 72, uppercase: false,
    },
    "Netflix": {
        fill_color: "#FFFFFF",
        highlight_colors: ["#FFFFFF", "#FFFFFF", "#FFFFFF", "#FFFFFF"],
        stroke_color: "#000000", stroke_width: 0,
        shadow_color: "#000000", shadow_opacity: 0.5, shadow_blur: 4.0,
        font_size: 64, uppercase: false,
    },
    "Neon": {
        fill_color: "#FFFFFF",
        highlight_colors: ["#FF00FF", "#00FFFF", "#39FF14", "#FF6600"],
        stroke_color: "#2D0040", stroke_width: 6,
        shadow_color: "#8800FF", shadow_opacity: 0.7, shadow_blur: 16.0,
        font_size: 120, uppercase: true,
    },
    "Pop Color": {
        fill_color: "#FFFFFF",
        highlight_colors: ["#FF69B4", "#4488FF", "#FFD700", "#00CC66"],
        stroke_color: "#000000", stroke_width: 10,
        shadow_color: "#000000", shadow_opacity: 0.6, shadow_blur: 6.0,
        font_size: 110, uppercase: true,
    },
    "Podcast": {
        fill_color: "#FFFFFF",
        highlight_colors: ["#6699CC", "#66AA66", "#CC8844", "#CC6644"],
        stroke_color: "#333333", stroke_width: 4,
        shadow_color: "#000000", shadow_opacity: 0.5, shadow_blur: 8.0,
        font_size: 80, uppercase: false,
    },
    "Luxury": {
        fill_color: "#FFFFFF",
        highlight_colors: ["#FFD700", "#FFFDD0", "#F5DEB3", "#FFFFFF"],
        stroke_color: "#1A1A1A", stroke_width: 2,
        shadow_color: "#000000", shadow_opacity: 0.5, shadow_blur: 10.0,
        font_size: 90, uppercase: true,
    },
};

function getWidgetValue(node, name) {
    const w = node.widgets?.find((w) => w.name === name);
    return w ? w.value : undefined;
}

function hexToRgba(hex, alpha) {
    const h = hex.replace("#", "");
    const r = parseInt(h.substring(0, 2), 16);
    const g = parseInt(h.substring(2, 4), 16);
    const b = parseInt(h.substring(4, 6), 16);
    return `rgba(${r},${g},${b},${alpha})`;
}

function buildTextShadow(strokeColor, strokeWidth, shadowColor, shadowOpacity, shadowBlur) {
    const parts = [];
    const n = strokeWidth;

    if (n > 0) {
        const offsets = [
            [-1, 0], [1, 0], [0, -1], [0, 1],
            [-1, -1], [1, -1], [-1, 1], [1, 1],
        ];
        for (const [dx, dy] of offsets) {
            parts.push(`${dx * n}px ${dy * n}px 0 ${strokeColor}`);
        }
    }

    if (shadowOpacity > 0) {
        const shadowRgba = hexToRgba(shadowColor, shadowOpacity);
        parts.push(`4px 4px ${shadowBlur}px ${shadowRgba}`);
    }

    return parts.join(", ") || "none";
}

function updatePreview(node, container) {
    const preset = getWidgetValue(node, "preset");

    let fillColor, highlightColor1, strokeColor, strokeWidth;
    let shadowColor, shadowOpacity, shadowBlur, fontSize, uppercase;

    if (preset && preset !== "Custom" && STYLE_PRESETS[preset]) {
        const p = STYLE_PRESETS[preset];
        fillColor = p.fill_color;
        highlightColor1 = p.highlight_colors[0];
        strokeColor = p.stroke_color;
        strokeWidth = p.stroke_width;
        shadowColor = p.shadow_color;
        shadowOpacity = p.shadow_opacity;
        shadowBlur = p.shadow_blur;
        fontSize = p.font_size;
        uppercase = p.uppercase;
    } else {
        fillColor = getWidgetValue(node, "fill_color") || "#FFFFFF";
        highlightColor1 = getWidgetValue(node, "highlight_color_1") || "#00FFFF";
        strokeColor = getWidgetValue(node, "stroke_color") || "#000000";
        strokeWidth = getWidgetValue(node, "stroke_width") ?? 8;
        shadowColor = getWidgetValue(node, "shadow_color") || "#000000";
        shadowOpacity = getWidgetValue(node, "shadow_opacity") ?? 0.6;
        shadowBlur = getWidgetValue(node, "shadow_blur") ?? 6.0;
        fontSize = getWidgetValue(node, "font_size") ?? 120;
        uppercase = getWidgetValue(node, "uppercase") ?? true;
    }

    const displaySize = Math.max(12, Math.round(fontSize / 3));
    const scaledStroke = Math.max(0, Math.round(strokeWidth / 3));
    const scaledBlur = Math.max(0, shadowBlur / 3);

    const textShadow = buildTextShadow(strokeColor, scaledStroke, shadowColor, shadowOpacity, scaledBlur);
    const transform = uppercase ? "uppercase" : "none";

    const helloText = uppercase ? "HELLO" : "Hello";
    const worldText = uppercase ? "WORLD" : "World";

    container.innerHTML = `
        <div style="
            background: #1a1a1a;
            border-radius: 6px;
            padding: 16px 12px;
            text-align: center;
            min-height: 40px;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 0.3em;
            overflow: hidden;
        ">
            <span style="
                font-family: Impact, 'Arial Black', sans-serif;
                font-size: ${displaySize}px;
                color: ${highlightColor1};
                text-shadow: ${textShadow};
                text-transform: ${transform};
                line-height: 1.2;
            ">${helloText}</span>
            <span style="
                font-family: Impact, 'Arial Black', sans-serif;
                font-size: ${displaySize}px;
                color: ${fillColor};
                text-shadow: ${textShadow};
                text-transform: ${transform};
                line-height: 1.2;
            ">${worldText}</span>
        </div>
    `;
}

app.registerExtension({
    name: "Ordo.CaptionStylePreview",

    async nodeCreated(node) {
        if (node.comfyClass !== "OrdoCaptionStyle") return;

        const container = document.createElement("div");
        container.style.width = "100%";

        const widget = node.addDOMWidget("style_preview", "custom", container, {
            serialize: false,
            hideOnZoom: false,
        });
        widget.computeSize = () => [0, 80];

        const doUpdate = () => updatePreview(node, container);

        // Hook into all existing widget callbacks
        for (const w of node.widgets || []) {
            if (w === widget) continue;
            const orig = w.callback;
            w.callback = function (...args) {
                if (orig) orig.apply(this, args);
                doUpdate();
            };
        }

        // Initial render
        requestAnimationFrame(doUpdate);
    },
});
