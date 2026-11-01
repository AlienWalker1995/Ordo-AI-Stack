"""ComfyUI HTTP route that measures geometry drift between two keyframe stills.

The before/after time-lapse shorts (Hermes skill ai-timelapse-short) render a hero still and
derive the other keyframe from it with an image edit. If the edit moves the camera (a scale
change or a shift of the whole frame), the first/last-frame video model cannot morph one into
the other and cuts instead: the 2026-10-14 short jumped 2 s in because its "before" still was
3.5% shorter and 19 px lower than the "after". Hermes runs that pipeline in a container with no
numpy or OpenCV, so the measurement lives here, next to the renders.

Route (CPU only, never queues GPU work, so the GPU admission gate passes it straight through):
  POST /keyframe_qa/drift   body {"a": "<job>/after_00001_.png", "b": "<job>/before_00001_.png"}
                            (paths relative to ComfyUI's output directory)
  -> {"ok": true, "scale_x": 1.0002, "scale_y": 0.9998, "shift_x": 0.3, "shift_y": -0.1,
      "match": 0.67, "band": "bottom 35%"}

Method: the frame's bottom band (ground, curbs, walls: the invariants every concept keeps in
the foreground) is reduced to edge strength, so the lighting change between the two stills
does not dominate, and OpenCV's ECC fits the affine warp between them.
"""
import asyncio
import os

from aiohttp import web
from server import PromptServer

OUTPUT_DIR = "/root/ComfyUI/output"
BAND_TOP = 0.65  # the invariant foreground: the bottom 35% of the frame


def _edges(gray):
    import cv2
    import numpy as np
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1)
    return np.sqrt(cv2.GaussianBlur(gx * gx + gy * gy, (5, 5), 0))


def measure_drift(path_a: str, path_b: str) -> dict:
    import cv2
    import numpy as np
    a = cv2.imread(path_a, cv2.IMREAD_GRAYSCALE)
    b = cv2.imread(path_b, cv2.IMREAD_GRAYSCALE)
    if a is None or b is None:
        raise ValueError("could not read both images")
    if a.shape != b.shape:
        raise ValueError(f"keyframes differ in size: {a.shape[::-1]} vs {b.shape[::-1]}")
    top = int(a.shape[0] * BAND_TOP)
    edges_a = _edges(a.astype(np.float32) / 255.0)[top:]
    edges_b = _edges(b.astype(np.float32) / 255.0)[top:]
    warp = np.eye(2, 3, dtype=np.float32)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 500, 1e-6)
    match, warp = cv2.findTransformECC(edges_a, edges_b, warp, cv2.MOTION_AFFINE, criteria, None, 5)
    return {
        "scale_x": round(float(np.hypot(warp[0, 0], warp[1, 0])), 4),
        "scale_y": round(float(np.hypot(warp[0, 1], warp[1, 1])), 4),
        "shift_x": round(float(warp[0, 2]), 1),
        "shift_y": round(float(warp[1, 2]), 1),
        "match": round(float(match), 3),
        "band": f"bottom {round((1 - BAND_TOP) * 100)}%",
    }


# A correction larger than this is not drift but a different shot (or a failed fit), and is
# never applied.
REGISTER_MAX_SCALE_ERROR = 0.08
REGISTER_MAX_SHIFT_PX = 80.0


def _fit_full_frame(hero_gray, edit_gray):
    """ECC affine fit on the foreground band, re-expressed in full-frame coordinates (the band
    starts at row `top`, so the translation picks up the rows the band skipped)."""
    import cv2
    import numpy as np
    top = int(hero_gray.shape[0] * BAND_TOP)
    warp = np.eye(2, 3, dtype=np.float32)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 500, 1e-6)
    match, warp = cv2.findTransformECC(_edges(hero_gray)[top:], _edges(edit_gray)[top:], warp,
                                       cv2.MOTION_AFFINE, criteria, None, 5)
    full = warp.astype(np.float64)
    full[0, 2] = warp[0, 2] - warp[0, 1] * top
    full[1, 2] = warp[1, 2] + top - warp[1, 1] * top
    return full, float(match)


def _drift_size(drift: dict) -> tuple[float, float]:
    """(scale error as a fraction, shift in pixels) of a measure_drift result."""
    return (max(abs(drift["scale_x"] - 1), abs(drift["scale_y"] - 1)),
            (drift["shift_x"] ** 2 + drift["shift_y"] ** 2) ** 0.5)


def _drift_score(drift: dict) -> float:
    """One number for comparing drifts: the worse of the scale error in percent and the shift
    in units of 4 px (1.0 is the time-lapse pipeline's acceptance limit for each)."""
    scale_error, shift = _drift_size(drift)
    return max(scale_error * 100, shift / 4)


def register_keyframes(hero_path: str, edit_path: str, hero_out: str, edit_out: str) -> dict:
    """Warp the edited keyframe onto the hero's geometry, then crop both by the same centred
    window (the frame's aspect ratio) so the warp leaves no empty border, and scale both back
    to the original size. Writes the pair to hero_out/edit_out. The first/last-frame model then
    sees one camera, at the cost of a small zoom (reported)."""
    import cv2
    import numpy as np
    hero = cv2.imread(hero_path)
    edit = cv2.imread(edit_path)
    if hero is None or edit is None or hero.shape != edit.shape:
        raise ValueError("need two readable keyframes of the same size")
    before = measure_drift(hero_path, edit_path)
    scale_error, shift = _drift_size(before)
    if scale_error > REGISTER_MAX_SCALE_ERROR or shift > REGISTER_MAX_SHIFT_PX:
        return {"applied": False, "reason": "correction too large to be drift", "drift_before": before}
    h, w = hero.shape[:2]
    full, _ = _fit_full_frame(cv2.cvtColor(hero, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0,
                              cv2.cvtColor(edit, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0)
    inverse = cv2.INTER_LANCZOS4 + cv2.WARP_INVERSE_MAP
    aligned = cv2.warpAffine(edit, full, (w, h), flags=inverse)
    valid = cv2.warpAffine(np.full((h, w), 255, np.uint8), full, (w, h),
                           flags=cv2.INTER_NEAREST + cv2.WARP_INVERSE_MAP)
    margin_x = margin_y = 0
    for margin_x in range(w // 4):
        margin_y = round(margin_x * h / w)
        if valid[margin_y:h - margin_y, margin_x:w - margin_x].min() == 255:
            break
    else:
        return {"applied": False, "reason": "no border-free crop", "drift_before": before}

    def crop(img):
        return cv2.resize(img[margin_y:h - margin_y, margin_x:w - margin_x], (w, h),
                          interpolation=cv2.INTER_LANCZOS4)

    cv2.imwrite(hero_out, crop(hero))
    cv2.imwrite(edit_out, crop(aligned))
    after = measure_drift(hero_out, edit_out)
    if _drift_score(after) >= _drift_score(before):
        os.remove(hero_out)
        os.remove(edit_out)
        return {"applied": False, "reason": "registration did not reduce drift",
                "drift_before": before, "drift_after": after}
    return {"applied": True, "zoom": round(w / (w - 2 * margin_x), 4),
            "drift_before": before, "drift_after": after}


def _resolve(relative: str) -> str:
    path = os.path.realpath(os.path.join(OUTPUT_DIR, relative))
    if not path.startswith(OUTPUT_DIR + os.sep):
        raise ValueError(f"path outside the output directory: {relative}")
    return path


@PromptServer.instance.routes.post("/keyframe_qa/drift")
async def keyframe_drift(request):
    try:
        body = await request.json()
        a, b = _resolve(body["a"]), _resolve(body["b"])
    except (KeyError, ValueError, TypeError) as e:
        return web.json_response({"ok": False, "error": f"bad request: {e}"}, status=400)
    try:
        # OpenCV work runs on a worker thread so the server's event loop stays free.
        result = await asyncio.get_running_loop().run_in_executor(None, measure_drift, a, b)
    except Exception as e:  # noqa: BLE001 - every failure goes back to the caller as JSON
        return web.json_response({"ok": False, "error": f"{type(e).__name__}: {e}"}, status=422)
    return web.json_response({"ok": True, **result})


@PromptServer.instance.routes.post("/keyframe_qa/register")
async def keyframe_register(request):
    """body {"hero": "<job>/after_00001_.png", "edit": "<job>/before_00001_.png"}: writes
    <job>/registered_<hero name> and <job>/registered_<edit name> when it applies a correction."""
    try:
        body = await request.json()
        hero, edit = _resolve(body["hero"]), _resolve(body["edit"])
    except (KeyError, ValueError, TypeError) as e:
        return web.json_response({"ok": False, "error": f"bad request: {e}"}, status=400)
    hero_out = os.path.join(os.path.dirname(hero), "registered_" + os.path.basename(hero))
    edit_out = os.path.join(os.path.dirname(edit), "registered_" + os.path.basename(edit))
    try:
        result = await asyncio.get_running_loop().run_in_executor(
            None, register_keyframes, hero, edit, hero_out, edit_out)
    except Exception as e:  # noqa: BLE001
        return web.json_response({"ok": False, "error": f"{type(e).__name__}: {e}"}, status=422)
    if result["applied"]:
        result["hero"] = os.path.relpath(hero_out, OUTPUT_DIR)
        result["edit"] = os.path.relpath(edit_out, OUTPUT_DIR)
    return web.json_response({"ok": True, **result})


NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
