"""Source framing: map ANY 16:9 source onto the approved template through NORMALIZED ratios (config/ranking_style.json).

The style file stores the template in reference pixels (source 1280x720, keep the top 630 rows). Here those become ratios, so a
1080p/1440p/4K source gets the identical crop and placement as 720p. An aspect ratio that cannot be mapped safely is never cropped
blindly: the render falls back to an uncropped fit and the output is sent to needs-review.
"""
from . import style

ASPECT_TOLERANCE = 0.01          # relative: 16:9 +-1% (1280x720, 1920x1080, 2560x1440, 3840x2160, 1366x768 ...)
MIN_WIDTH = 320


def reference() -> tuple[int, int]:
    s = style.load()["source"]
    return s["width"], s["height"]


def crop_ratio() -> float:
    """Fraction of the source height kept (drops the burned-in caption strip): 630/720 = 0.875 in the approved template."""
    return style.load()["framing"]["keep_height"] / reference()[1]


def analyze(width: int, height: int) -> dict:
    rw, rh = reference()
    if not width or not height or width < MIN_WIDTH:
        return {"ok": False, "width": width, "height": height, "reason": f"source size {width}x{height} is unusable"}
    aspect, ref_aspect = width / height, rw / rh
    ok = abs(aspect - ref_aspect) / ref_aspect <= ASPECT_TOLERANCE
    return {"ok": ok, "width": width, "height": height, "aspect": round(aspect, 4), "scale": round(width / rw, 4), "crop_ratio": crop_ratio(),
            "reason": "" if ok else f"unusual aspect ratio {width}x{height} ({aspect:.3f}:1, template is 16:9); cannot be cropped safely"}


def crop_filter(fr: dict) -> str:
    """ffmpeg crop (even height) by ratio for a mappable source; no crop at all otherwise."""
    if not fr["ok"]:
        return "null"
    return f"crop=iw:2*trunc(ih*{crop_ratio():.6f}/2):0:0"
