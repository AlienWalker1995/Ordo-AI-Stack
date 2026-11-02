/**
 * Ordo Caption Studio — ComfyUI extension loader.
 *
 * Hand-written JS (not part of Vite build). Auto-loaded by ComfyUI.
 * Lazy-loads the React bundle only when a Caption Studio node is created.
 */

const { app } = window.comfyAPI.app;

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

// Lazy-loaded React module reference
let _reactModule = null;
let _reactLoadPromise = null;

// Compute asset URLs relative to this extension file
const _baseUrl = import.meta.url.substring(0, import.meta.url.lastIndexOf("/"));
const _bundleUrl = _baseUrl + "/assets/caption-studio-app.js";
const _cssUrl = _baseUrl + "/assets/caption-studio-app.css";

function getReactModule() {
    if (_reactModule) return Promise.resolve(_reactModule);
    if (_reactLoadPromise) return _reactLoadPromise;

    console.log("[Ordo] Caption Studio extension URL:", import.meta.url);
    console.log("[Ordo] Loading React bundle from:", _bundleUrl);

    _reactLoadPromise = import(_bundleUrl)
        .then((mod) => {
            _reactModule = mod;
            console.log("[Ordo] Caption Studio React bundle loaded OK");

            // Inject CSS if not already present
            if (!document.getElementById("jcs-styles")) {
                const link = document.createElement("link");
                link.id = "jcs-styles";
                link.rel = "stylesheet";
                link.href = _cssUrl;
                document.head.appendChild(link);
            }

            return mod;
        })
        .catch((err) => {
            console.error("[Ordo] Failed to load Caption Studio React bundle:", err);
            console.error("[Ordo] Tried URL:", _bundleUrl);
            _reactLoadPromise = null;
            throw err;
        });

    return _reactLoadPromise;
}

app.registerExtension({
    name: "Ordo.CaptionStudio",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData?.name !== "OrdoCaptionStudio") return;

        chainCallback(nodeType.prototype, "onNodeCreated", function () {
            // Hide the edited_timings and style_config widgets
            const editedWidget = this.widgets.find(
                (w) => w.name === "edited_timings"
            );
            if (editedWidget) {
                editedWidget.hidden = true;
                editedWidget.computeSize = () => [0, -4];
                editedWidget.type = "converted-widget";
            }

            const styleWidget = this.widgets.find(
                (w) => w.name === "style_config"
            );
            if (styleWidget) {
                styleWidget.hidden = true;
                styleWidget.computeSize = () => [0, -4];
                styleWidget.type = "converted-widget";
            }

            // Create container div for React app
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
                Queue the workflow to populate Caption Studio.</div>`;

            // Store references on the node
            this._studioContainer = container;
            this._editedWidget = editedWidget;
            this._styleWidget = styleWidget;
            this._studioMounted = false;

            // Add as DOM widget
            this.addDOMWidget(
                "caption_studio",
                "CaptionStudioWidget",
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

                getReactModule()
                    .then((mod) => {
                        // Set widget references for syncing back to Python
                        mod.setWidgetRefs(
                            self._editedWidget,
                            self._styleWidget
                        );

                        // Mount React app on first execution
                        if (!self._studioMounted) {
                            mod.mountCaptionStudio(
                                self._studioContainer,
                                self.id.toString()
                            );
                            self._studioMounted = true;
                        }

                        // Send Python output to React
                        mod.receiveFromPython(output);

                        // Resize node to fit content
                        requestAnimationFrame(() => {
                            const newSize = self.computeSize();
                            const curSize = self.size;
                            if (curSize) {
                                newSize[0] = Math.max(newSize[0], curSize[0], 500);
                                newSize[1] = Math.max(newSize[1], curSize[1]);
                            }
                            self.setSize?.(newSize);
                            app.graph?.setDirtyCanvas(true, true);
                        });
                    })
                    .catch((err) => {
                        console.error(
                            "[Ordo] Caption Studio: failed to load React app",
                            err
                        );
                        self._studioContainer.innerHTML = `
                            <div style="padding:12px;color:#f66;text-align:center;">
                                Failed to load Caption Studio UI. Check console for errors.
                            </div>`;
                    });
            });
        });
    },
});
