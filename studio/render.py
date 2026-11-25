"""Clip composition, preserving the legacy vertical recipe unless visual settings are supplied."""
import copy
import hashlib
import io
import json
import math
import re
import shutil
import tempfile
from functools import lru_cache
from pathlib import Path

from . import config, roman, voice
from .media import ProcessFailed, probe, report_progress, run

W, H = config.OUT_W, config.OUT_H

# ASS colours are &HBBGGRR. `size` is the ASS font size (line height); _Font turns it into real pixels.
STYLES = {
    "bounce": {"label": "Bounce", "font": "Poppins ExtraBold", "size": 132, "base": "FFFFFF", "active": "00F0FF",
               "strong": "FFFFFF", "outline": 7, "pop": 112, "upper": True, "per": 3},
    "hype": {"label": "Hype", "font": "Anton", "size": 152, "base": "FFFFFF", "active": "3C3CFF",
             "strong": "00D7FF", "outline": 7, "pop": 114, "upper": True, "per": 2},
    "clean": {"label": "Clean", "font": "Poppins ExtraBold", "size": 100, "base": "FFFFFF", "active": "6BD7FF",
              "strong": "FFFFFF", "outline": 5, "pop": 106, "upper": False, "per": 4},
}
HOOK = {"font": "Poppins ExtraBold", "size": 96, "ink": "14100E", "box": "FFFFFF", "pad_x": 36, "pad_y": 22, "radius": 26,
        "max_w": 900}
FONT_FILES = {"Poppins ExtraBold": "Poppins-ExtraBold.ttf", "Anton": "Anton-Regular.ttf", "Archivo Black": "ArchivoBlack-Regular.ttf"}
CAP_MAX_W = 960   # captions never run wider than this, whatever is said
FONT_IDS = {"poppins": "Poppins ExtraBold", "anton": "Anton", "archivo": "Archivo Black"}
HEADLINE_DEFAULTS = {"font": "poppins", "size": 96, "color": "#0E1014",
                     "background": {"color": "#FFFFFF", "opacity": 1}, "width": 5 / 6, "position": None}
FRAMING_DEFAULTS = {"mode": "legacy", "ratio": "9:16", "roi": {"x": 0, "y": 0, "width": 1, "height": 1}, "fit": "crop"}


def _number(value, field, low, high, *, positive=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or value < low or value > high or (positive and value <= 0)):
        raise ValueError(f"{field} must be a finite number {'above' if positive else 'from'} {low} to {high}.")
    return value


def _object(value, field, keys):
    if not isinstance(value, dict) or set(value) - set(keys):
        raise ValueError(f"{field} must be an object with supported fields only.")
    return value


def _color(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
        raise ValueError(f"{field} must be an RGB color such as #FFFFFF.")
    return value.upper()


def validate_visual(value, base=None):
    """Merge an optional visual patch without opting untouched legacy clips into new behavior."""
    merged = validate_visual(base) if base is not None else None
    if value is None:
        return merged
    _object(value, "visual", ("schema_version", "headline", "framing"))
    version = value.get("schema_version", 1)
    if isinstance(version, bool) or version != 1:
        raise ValueError("visual.schema_version must be 1.")
    merged = merged or {"schema_version": 1}
    if "headline" in value:
        patch = _object(value["headline"], "headline", HEADLINE_DEFAULTS)
        headline = copy.deepcopy(merged.get("headline", HEADLINE_DEFAULTS))
        old_background, old_position = headline["background"], headline["position"]
        headline.update(copy.deepcopy(patch))
        if not isinstance(headline["font"], str) or headline["font"] not in FONT_IDS:
            raise ValueError("headline.font must be poppins, anton, or archivo.")
        _number(headline["size"], "headline.size", 0, 1080, positive=True)
        _number(headline["width"], "headline.width", 0, 1, positive=True)
        headline["color"] = _color(headline["color"], "headline.color")
        if headline["background"] is not None:
            background = _object(headline["background"], "headline.background", ("color", "opacity"))
            background = {**(old_background or HEADLINE_DEFAULTS["background"]), **background}
            background["color"] = _color(background["color"], "headline.background.color")
            _number(background["opacity"], "headline.background.opacity", 0, 1)
            headline["background"] = background
        if headline["position"] is not None:
            position = _object(headline["position"], "headline.position", ("x", "y"))
            position = {**(old_position or {}), **position}
            if set(position) != {"x", "y"}:
                raise ValueError("headline.position needs both x and y.")
            for key in ("x", "y"):
                _number(position[key], f"headline.position.{key}", 0, 1)
            headline["position"] = position
        merged["headline"] = headline
    if "framing" in value:
        patch = _object(value["framing"], "framing", FRAMING_DEFAULTS)
        framing = copy.deepcopy(merged.get("framing", FRAMING_DEFAULTS))
        old_roi = framing["roi"]
        framing.update(copy.deepcopy(patch))
        if framing["mode"] not in ("legacy", "manual"):
            raise ValueError("framing.mode must be legacy or manual.")
        if framing["ratio"] not in ("9:16", "1:1", "16:9", "source"):
            raise ValueError("framing.ratio isn't supported.")
        if framing["fit"] not in ("crop", "contain"):
            raise ValueError("framing.fit must be crop or contain.")
        roi = {**old_roi, **_object(framing["roi"], "framing.roi", ("x", "y", "width", "height"))}
        for key in ("x", "y", "width", "height"):
            _number(roi[key], f"framing.roi.{key}", 0, 1, positive=key in ("width", "height"))
        if roi["x"] + roi["width"] > 1 + 1e-9 or roi["y"] + roi["height"] > 1 + 1e-9:
            raise ValueError("framing.roi must stay within the source.")
        framing["roi"] = roi
        if framing["mode"] == "legacy" and (framing["ratio"] != "9:16" or framing["fit"] != "crop"
                                               or roi != FRAMING_DEFAULTS["roi"]):
            raise ValueError("Custom ratio, ROI, or fit requires framing.mode='manual'.")
        merged["framing"] = framing
    return merged


def visual_catalog():
    rgb = lambda bgr: "#" + bgr[4:6] + bgr[2:4] + bgr[0:2]  # noqa: E731
    return {"schema_version": 1, "fonts": [{"id": key, "label": name} for key, name in FONT_IDS.items()],
            "defaults": {"headline": copy.deepcopy(HEADLINE_DEFAULTS), "framing": copy.deepcopy(FRAMING_DEFAULTS)},
            "styles": {key: {**{field: st[field] for field in ("label", "font", "size")},
                             **{field: rgb(st[field]) for field in ("base", "active", "strong")}}
                       for key, st in STYLES.items()}}


class _Font:
    """Measures text exactly as the renderer will draw it, so every word can be placed by hand: the highlighted
    word can grow without pushing its neighbours, and the heading box fits its text."""
    _cache: dict = {}

    def __init__(self, name):
        import struct
        path = Path(__file__).resolve().parent / "fonts" / FONT_FILES.get(name, FONT_FILES["Poppins ExtraBold"])
        d = path.read_bytes()
        tables = {}
        for i in range(struct.unpack(">H", d[4:6])[0]):
            tag, _, off, _ = struct.unpack(">4sIII", d[12 + 16 * i: 28 + 16 * i])
            tables[tag] = off
        self.upm = struct.unpack(">H", d[tables[b"head"] + 18: tables[b"head"] + 20])[0]
        o = tables[b"OS/2"]
        self.asc, self.desc = struct.unpack(">HH", d[o + 74: o + 78])      # what the renderer sizes the font by
        self.cap = struct.unpack(">h", d[o + 88: o + 90])[0] or int(self.upm * 0.7)
        self.path = str(path)
        self._pil = {}

    @classmethod
    def get(cls, name):
        if name not in cls._cache:
            cls._cache[name] = cls(name)
        return cls._cache[name]

    def em(self, size):
        return size * self.upm / (self.asc + self.desc)

    def width(self, text, size):
        """Pixels wide at this ASS size."""
        try:
            from PIL import ImageFont
            if "f" not in self._pil:
                self._pil["f"] = ImageFont.truetype(self.path, 200)
            return self._pil["f"].getlength(text) * self.em(size) / 200
        except Exception:  # noqa: BLE001 - no measuring library: a fair estimate keeps clips rendering
            return len(text) * self.em(size) * 0.62

    def cap_shift(self, size):
        """How far below the anchor the middle of a capital letter sits when text is centred on a point."""
        return ((self.asc - self.desc) / 2 - self.cap / 2) * size / (self.asc + self.desc)


CAPTION_LEAD = 0.0  # measured: word stamps are within 0.15 s of the voice, so no shift is needed
def install_fonts():
    """Copy the bundled caption fonts next to the data, so captions look the same on every machine and server."""
    src = Path(__file__).resolve().parent / "fonts"
    for f in src.glob("*.ttf"):
        if not (config.FONTS_DIR / f.name).exists():
            shutil.copyfile(f, config.FONTS_DIR / f.name)


LAYOUTS = {"blur": "Full frame + blurred fill", "zoom": "Zoomed in + blurred fill", "crop": "Fill the screen (centre crop)"}


def _ts(t):
    t = max(0.0, t)
    h, m = int(t // 3600), int(t % 3600 // 60)
    return f"{h}:{m:02d}:{t % 60:05.2f}"


def _clean(text):
    return re.sub(r"[\x00-\x1f\x7f]+", " ", re.sub(r"[{}\\]", "", str(text))).strip()


def _key(word):
    return re.sub(r"[^a-z0-9']", "", word.lower())


def _groups(words, per):
    out, cur = [], []
    for w in words:
        if cur and w.get("caption_group") is not None and cur[-1].get("caption_group") is not None:
            if w["caption_group"] != cur[-1]["caption_group"]:
                out.append(cur)
                cur = []
            cur.append(w)
            continue
        if cur and (len(cur) >= per or sum(len(x["w"]) for x in cur) + len(w["w"]) > 16
                    or w["s"] - cur[-1]["e"] > 0.45 or cur[-1]["w"][-1:] in ".?!"):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def geometry(src_w, src_h, layout):
    """Where the video sits in the 1080x1920 frame, and where hook and captions go."""
    if src_h >= src_w and layout != "crop":
        layout = "crop"  # already vertical or square: nothing to gain from a blurred fill
    if layout == "blur":
        fg_h = min(H, int(round(W * src_h / src_w / 2)) * 2)
    elif layout == "zoom":
        fg_h = 1000
    else:
        fg_h = H
    top = (H - fg_h) // 2
    if layout == "crop":
        return layout, fg_h, 300, 1380
    return layout, fg_h, max(230, top - 190), min(1640, top + fg_h + 180)


def _resolve_canvas(clip, source_meta, visual, legacy_size=None):
    sw, sh = source_meta.get("width"), source_meta.get("height")
    _number(sw, "source.width", 1, 16384)
    _number(sh, "source.height", 1, 16384)
    sw, sh = int(round(sw)), int(round(sh))
    rotation = source_meta.get("rotation", 0)
    source = {"width": sw, "height": sh, "coded_width": source_meta.get("coded_width", sh if rotation in (90, 270) else sw),
              "coded_height": source_meta.get("coded_height", sw if rotation in (90, 270) else sh),
              "sar": source_meta.get("sar", "1:1"), "rotation": rotation}
    framing = (visual or {}).get("framing", FRAMING_DEFAULTS)
    mode, ratio, fit = framing["mode"], framing["ratio"], framing["fit"]
    warnings = []
    if mode == "legacy":
        # The old probe ignored SAR. Preserve that recipe even though the new probe reports display dimensions.
        lw, lh = legacy_size if legacy_size is not None else (source["coded_width"], source["coded_height"])
        if legacy_size is None and rotation in (90, 270):
            lw, lh = lh, lw
        _number(lw, "legacy source width", 1, 16384)
        _number(lh, "legacy source height", 1, 16384)
        layout, fg_h, hook_y, cap_y = geometry(lw, lh, clip.get("layout", "blur"))
        if fg_h < 2:
            raise ValueError("The legacy foreground is too small; choose crop or manual framing.")
        width, height = W, H
        foreground = {"x": 0, "y": (H - fg_h) // 2, "width": W, "height": fg_h}
        target = W / fg_h
        rw, rh = (lh * target, lh) if lw / lh > target else (lw, lw / target)
        crop = {"x": (lw - rw) / 2 * sw / lw, "y": (lh - rh) / 2 * sh / lh,
                "width": rw * sw / lw, "height": rh * sh / lh}
    else:
        sample_w, sample_h = source["coded_width"], source["coded_height"]
        _number(sample_w, "source.coded_width", 1, 16384)
        _number(sample_h, "source.coded_height", 1, 16384)
        if rotation in (90, 270):
            sample_w, sample_h = sample_h, sample_w
        detail_limit = min(1, sample_w / sw, sample_h / sh)
        if detail_limit < 1:
            warnings.append("sar_normalized")
        roi = framing["roi"]
        left = 2 * math.ceil(roi["x"] * sw / 2 - 1e-9)
        top = 2 * math.ceil(roi["y"] * sh / 2 - 1e-9)
        right = 2 * math.floor((roi["x"] + roi["width"]) * sw / 2 + 1e-9)
        bottom = 2 * math.floor((roi["y"] + roi["height"]) * sh / 2 + 1e-9)
        rw, rh = right - left, bottom - top
        if min(rw, rh) < 2:
            raise ValueError("The source ROI is too small to encode; choose more source detail.")
        if ratio == "source":
            cap_scale = min(1920 / max(sw, sh), 1080 / min(sw, sh))
            cap_w, cap_h = 2 * int(sw * cap_scale / 2), 2 * int(sh * cap_scale / 2)
            if fit == "crop":
                target = sw / sh
                cw, ch = (rh * target, rh) if rw / rh > target else (rw, rw / target)
                cw, ch = 2 * int(cw / 2 + 1e-9), 2 * int(ch / 2 + 1e-9)
                left += 2 * int((rw - cw) / 4)
                top += 2 * int((rh - ch) / 4)
                rw, rh = cw, ch
                if min(rw, rh) < 2:
                    raise ValueError("The source ROI is too small for the source ratio.")
                scale = min(1, cap_w / rw, cap_h / rh, detail_limit)
                width, height = 2 * int(rw * scale / 2 + 1e-9), 2 * int(rh * scale / 2 + 1e-9)
            else:
                scale = min(1, max(rw / cap_w, rh / cap_h) * detail_limit)
                width, height = 2 * int(cap_w * scale / 2 + 1e-9), 2 * int(cap_h * scale / 2 + 1e-9)
        else:
            a, b = (int(v) for v in ratio.split(":"))
            unit_w, unit_h = a * 2, b * 2
            cap_w, cap_h = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080)}[ratio]
            if fit == "crop":
                multiples = int(min(rw / unit_w, rh / unit_h))
                if multiples < 1:
                    raise ValueError("The source ROI is too small for this ratio; choose more source detail.")
                cw, ch = unit_w * multiples, unit_h * multiples
                left += 2 * int((rw - cw) / 4)
                top += 2 * int((rh - ch) / 4)
                rw, rh = cw, ch
                multiples = min(multiples, cap_w // unit_w, cap_h // unit_h,
                                int(rw * detail_limit / unit_w), int(rh * detail_limit / unit_h))
            else:
                scale = min(1, max(rw / cap_w, rh / cap_h) * detail_limit)
                multiples = int(min(cap_w * scale / unit_w, cap_h * scale / unit_h))
            if multiples < 1:
                raise ValueError("The source ROI is too small for this ratio; choose more source detail.")
            width, height = multiples * unit_w, multiples * unit_h
        if min(width, height) < 2:
            raise ValueError("The source ROI has too little coded detail to encode.")
        crop = {"x": left, "y": top, "width": rw, "height": rh}
        scale = min(width / rw, height / rh, detail_limit)
        fw, fh = ((width, height) if fit == "crop" else
                  (max(2, 2 * int(rw * scale / 2 + 1e-9)), max(2, 2 * int(rh * scale / 2 + 1e-9))))
        foreground = {"x": (width - fw) // 2, "y": (height - fh) // 2, "width": fw, "height": fh}
        layout, hook_y, cap_y = "manual", height * 300 / 1920, height * 0.82
        if width < cap_w or height < cap_h:
            warnings.append("reduced_resolution")
    return {"schema_version": 1, "mode": mode, "ratio": ratio, "fit": fit, "width": width, "height": height,
            "source": source, "effective_roi": {"x": crop["x"] / sw, "y": crop["y"] / sh,
                                                "width": crop["width"] / sw, "height": crop["height"] / sh},
            "crop_pixels": crop, "foreground": foreground, "layout": layout,
            "hook_y": hook_y, "caption_y": cap_y, "headline": None, "warnings": warnings}


@lru_cache(maxsize=64)
def _headline_bitmap(text, style_json, width, height):
    from PIL import Image, ImageDraw, ImageFont

    style = json.loads(style_json)
    f = _Font.get(FONT_IDS[style["font"]])
    scale = min(width, height) / 1080
    size = style["size"] * scale
    pixels = max(1, int(round(f.em(size))))
    pad_x, pad_y = min(HOOK["pad_x"] * scale, width / 8), min(HOOK["pad_y"] * scale, height / 8)
    available = max(1, min(width * style["width"], width - 2 * pad_x))
    font = ImageFont.truetype(f.path, pixels)
    words, warnings = text.split(), []
    lines = [" ".join(words)] if words else []
    if lines and font.getlength(lines[0]) > available and len(words) > 1:
        cut = min(range(1, len(words)), key=lambda i: max(font.getlength(" ".join(words[:i])), font.getlength(" ".join(words[i:]))))
        lines = [" ".join(words[:cut]), " ".join(words[cut:])]
    widest = max((font.getlength(line) for line in lines), default=0)
    line_h = pixels * 1.16
    original_pixels = pixels
    while widest > available or line_h * len(lines) > max(1, height - 2 * pad_y):
        if pixels == 1:
            raise ValueError("The headline is too large to fit this canvas.")
        if not warnings:
            warnings.append("headline_shrunk")
        shrink = min(available / max(1, widest), max(1, height - 2 * pad_y) / max(1, line_h * len(lines)))
        pixels = max(1, min(pixels - 1, int(pixels * shrink)))
        font = ImageFont.truetype(f.path, pixels)
        widest = max((font.getlength(line) for line in lines), default=0)
        line_h = pixels * 1.16
    size *= pixels / original_pixels
    if widest + 2 * pad_x > width + 1e-6 or line_h * len(lines) + 2 * pad_y > height + 1e-6:
        raise ValueError("The headline is too large to fit this canvas.")
    bw, bh = (max(1, math.ceil(widest + 2 * pad_x)), max(1, math.ceil(line_h * len(lines) + 2 * pad_y))) if lines else (1, 1)
    image = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    rgb = lambda color: tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))  # noqa: E731
    background = style["background"]
    if lines and background is not None:
        draw.rounded_rectangle((0, 0, bw - 1, bh - 1), radius=min(HOOK["radius"] * scale, bh / 2),
                               fill=(*rgb(background["color"]), round(background["opacity"] * 255)))
    for i, line in enumerate(lines):
        left, top, right, bottom = draw.textbbox((0, 0), line, font=font)
        cy = pad_y + line_h * (i + 0.5)
        draw.text(((bw - (right - left)) / 2 - left, cy - (bottom - top) / 2 - top),
                  line, font=font, fill=(*rgb(style["color"]), 255))
    data = io.BytesIO()
    image.save(data, format="PNG")
    return data.getvalue(), {"bitmap_width": bw, "bitmap_height": bh, "canvas_width": width, "canvas_height": height,
                             "font": style["font"], "size": round(size / scale, 4), "font_pixels": pixels,
                             "color": style["color"], "background": background, "lines": lines,
                             "padding": {"x": pad_x, "y": pad_y}, "fade_ms": 160, "warnings": warnings}


def _headline(text, visual, resolved):
    style = (visual or {}).get("headline", HEADLINE_DEFAULTS)
    bitmap_style = {key: val for key, val in style.items() if key != "position"}
    data, cached = _headline_bitmap(_clean(text), json.dumps(bitmap_style, sort_keys=True), resolved["width"], resolved["height"])
    meta = copy.deepcopy(cached)
    meta["warnings"] = list(dict.fromkeys([*resolved["warnings"], *meta["warnings"]]))
    width, height, bw, bh = meta["canvas_width"], meta["canvas_height"], meta["bitmap_width"], meta["bitmap_height"]
    position = style["position"]
    cx, cy = (width * position["x"], height * position["y"]) if position is not None else (width / 2, resolved["hook_y"])
    x, y = max(0, min(width - bw, round(cx - bw / 2))), max(0, min(height - bh, round(cy - bh / 2)))
    meta.update(x=x, y=y, center={"x": (x + bw / 2) / width, "y": (y + bh / 2) / height})
    if x != round(cx - bw / 2) or y != round(cy - bh / 2):
        meta["warnings"].append("position_clamped")
    if y + bh > resolved["caption_y"] - min(width, height) * 0.1:
        meta["warnings"].append("caption_overlap")
    return data, meta


def _resolve_visual(clip, source_meta, visual, legacy_size=None):
    resolved = _resolve_canvas(clip, source_meta, visual, legacy_size)
    if visual is not None and ("headline" in visual or resolved["mode"] == "manual") and _clean(clip.get("hook", "")):
        _, resolved["headline"] = _headline(clip["hook"], visual, resolved)
        resolved["warnings"] = list(dict.fromkeys([*resolved["warnings"], *resolved["headline"]["warnings"]]))
    return resolved


def resolve_visual(clip, source_meta):
    """Serializable display-oriented canvas, effective crop, and shared headline geometry."""
    return _resolve_visual(clip, source_meta, validate_visual(clip.get("visual")))


def headline_preview(text, visual, source_meta, layout="blur"):
    visual = validate_visual(visual)
    resolved = _resolve_canvas({"layout": layout}, source_meta, visual)
    return _headline(text, visual, resolved)


def _hook_events(hook, dur, y, *, width=W, height=H):
    """The heading: dark text in a white rounded box that fits it, on one line or two balanced ones."""
    settings = HOOK
    if (width, height) != (W, H):
        scale = min(width, height) / 1080
        settings = {**HOOK, **{key: HOOK[key] * scale for key in ("size", "pad_x", "pad_y", "radius")},
                    "max_w": width * HOOK["max_w"] / W}
    f, size = _Font.get(settings["font"]), settings["size"]
    words = hook.split()
    if not words:
        return []
    lines = [" ".join(words)]
    if f.width(lines[0], size) > settings["max_w"] and len(words) > 1:      # two lines, split where they come out most even
        cut = min(range(1, len(words)), key=lambda i: max(f.width(" ".join(words[:i]), size), f.width(" ".join(words[i:]), size)))
        lines = [" ".join(words[:cut]), " ".join(words[cut:])]
    widest = max(f.width(l, size) for l in lines)
    if widest > settings["max_w"]:
        size = max(40 if (width, height) == (W, H) else 1, int(size * settings["max_w"] / widest))
        widest = max(f.width(l, size) for l in lines)
    line_h = f.em(size) * 1.16
    bw, bh = widest + 2 * settings["pad_x"], line_h * len(lines) + 2 * settings["pad_y"]
    x0, y0, r = (width - bw) / 2, y - bh / 2, min(settings["radius"], bh / 2)
    x1, y1 = x0 + bw, y0 + bh
    n = lambda v: f"{v:.0f}"                                                         # noqa: E731
    box = (f"m {n(x0 + r)} {n(y0)} l {n(x1 - r)} {n(y0)} b {n(x1)} {n(y0)} {n(x1)} {n(y0)} {n(x1)} {n(y0 + r)} "
           f"l {n(x1)} {n(y1 - r)} b {n(x1)} {n(y1)} {n(x1)} {n(y1)} {n(x1 - r)} {n(y1)} "
           f"l {n(x0 + r)} {n(y1)} b {n(x0)} {n(y1)} {n(x0)} {n(y1)} {n(x0)} {n(y1 - r)} "
           f"l {n(x0)} {n(y0 + r)} b {n(x0)} {n(y0)} {n(x0)} {n(y0)} {n(x0 + r)} {n(y0)}")
    span = f"{_ts(0)},{_ts(dur)}"
    ev = [f"Dialogue: 3,{span},Hook,,0,0,0,,{{\\an7\\pos(0,0)\\fad(160,0)\\c&H{HOOK['box']}&\\p1}}{box}{{\\p0}}"]
    for i, line in enumerate(lines):
        cy = y - line_h * (len(lines) - 1) / 2 + i * line_h - f.cap_shift(size)
        ev.append(f"Dialogue: 4,{span},Hook,,0,0,0,,{{\\an5\\pos({width // 2},{cy:.0f})\\fad(160,0)\\fs{size}}}{line}")
    return ev


def build_ass(words, start, end, hook, style_name, highlight, hook_y, cap_y, *, width=W, height=H, omit_hook=False) -> str:
    st = STYLES.get(style_name, STYLES["bounce"])
    legacy = (width, height) == (W, H)
    scale = 1 if legacy else min(width, height) / 1080
    if not legacy:
        st = {**st, "size": max(1, round(st["size"] * scale)), "outline": round(st["outline"] * scale, 3)}
    max_w = CAP_MAX_W if legacy else width * CAP_MAX_W / W
    words = [dict(w, s=w["s"] - CAPTION_LEAD, e=w["e"] - CAPTION_LEAD) for w in words]
    strong = {_key(h) for h in highlight if _key(h)}
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{st['font']},{st['size']},&H00{st['base']},&H00{st['base']},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,{st['outline']},0,5,0,0,0,1
Style: Hook,{HOOK['font']},{HOOK['size']},&H00{HOOK['ink']},&H00{HOOK['ink']},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dur = end - start
    ev = [] if omit_hook else _hook_events(_clean(hook), dur, hook_y, width=width, height=height)
    f = _Font.get(st["font"])
    inside = [dict(w, w=_clean(w["w"])) for w in words if w["s"] >= start - 0.05 and w["s"] < end]
    inside = sorted((w for w in inside if w["w"]), key=lambda w: w["s"])
    for w in inside:
        if w.get("caption_font") and w["caption_font"] != st["font"]:
            w.pop("caption_group", None)
    for i, w in enumerate(inside[:-1]):
        w["e"] = max(w["s"], min(w["e"], inside[i + 1]["s"]))
    groups = _groups(inside, st["per"])
    for gi, g in enumerate(groups):
        g_start = max(0.0, g[0]["s"] - start)
        tail = 0 if g[-1].get("timing") == "ass-recovered" else 0.25
        g_end = min(g[-1]["e"] + tail, groups[gi + 1][0]["s"] if gi + 1 < len(groups) else end) - start
        if g_end - g_start < 0.02:
            continue
        texts = []
        for x in g:
            t = re.sub(r"[.,;:]+$", "", x["w"])
            texts.append(t.upper() if st["upper"] else t)
        if not all(roman.drawable(t) for t in texts):
            # A script the caption font cannot draw (Japanese, Thai, Chinese...): its words cannot be measured, so
            # they are not placed one by one. The line is drawn whole and centred, in a font the system has for it.
            line = " ".join(texts)
            fs = max(44 if legacy else max(1, round(44 * scale)), min(int(st["size"] * 0.7), int(1500 * scale / max(1, len(line)) * 1.6)))
            margin = 60 if legacy else max(1, round(60 * scale))
            ev.append(f"Dialogue: 1,{_ts(g_start)},{_ts(g_end)},Cap,,{margin},{margin},0,,{{\\an5\\pos({width // 2},{cap_y:.0f})\\q0\\fs{fs}}}{line}")
            continue
        size = st["size"]
        gap = f.width("n n", size) - 2 * f.width("n", size) + (2 * st["outline"] + 6 * scale) * size / st["size"]   # a space, plus room for both outlines
        total = sum(f.width(t, size) for t in texts) + gap * (len(texts) - 1)
        if total > max_w:                                  # a long phrase: shrink it to fit, never run off the frame
            size = max(36 if legacy else 1, int(size * max_w / total))
            gap = f.width("n n", size) - 2 * f.width("n", size) + (2 * st["outline"] + 6 * scale) * size / st["size"]   # a space, plus room for both outlines
            total = sum(f.width(t, size) for t in texts) + gap * (len(texts) - 1)
        cy = cap_y - f.cap_shift(size)
        x = (width - total) / 2
        p = st["pop"]
        for i, (w, t) in enumerate(zip(g, texts)):
            wd = f.width(t, size)
            p = max(104, min(st["pop"], 100 + int(2200 / max(wd, 1))))   # long words grow less, so they never touch their neighbours
            pos = f"\\an5\\pos({x + wd / 2:.0f},{cy:.0f})\\fs{size}"
            x += wd + gap
            a = max(g_start, w["s"] - start)
            b = (g[i + 1]["s"] - start) if i + 1 < len(g) else g_end
            base = st["strong"] if _key(w["w"]) in strong else st["base"]
            if a - g_start >= 0.02:                                                   # waiting to be said
                ev.append(f"Dialogue: 1,{_ts(g_start)},{_ts(a)},Cap,,0,0,0,,{{{pos}\\c&H{base}&}}{t}")
            if b - a >= 0.02:                                                         # being said: colour and a quick pop, in front
                ev.append(f"Dialogue: 2,{_ts(a)},{_ts(b)},Cap,,0,0,0,,{{{pos}\\c&H{st['active']}&\\fscx94\\fscy94"
                          f"\\t(0,90,\\fscx{p}\\fscy{p})\\t(90,200,\\fscx{p - 5}\\fscy{p - 5})}}{t}")
            if g_end - b >= 0.02:                                                     # said
                ev.append(f"Dialogue: 1,{_ts(b)},{_ts(g_end)},Cap,,0,0,0,,{{{pos}\\c&H{base}&\\fscx{p - 5}\\fscy{p - 5}"
                          f"\\t(0,90,\\fscx100\\fscy100)}}{t}")
    return head + "\n".join(ev) + "\n"


def _filter(layout, fg_h):
    if layout == "crop":
        return f"[0:v]fps={config.OUT_FPS},scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[v0];"
    return (f"[0:v]fps={config.OUT_FPS},split=2[a][b];"
            f"[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=6:2,"
            f"scale={W}:{H},eq=brightness=-0.14:saturation=1.15[bg];"
            f"[b]scale={W}:{fg_h}:force_original_aspect_ratio=increase,crop={W}:{fg_h}[fg];"
            f"[bg][fg]overlay=0:(H-h)/2,setsar=1[v0];")


def _visual_filter(resolved):
    """Normalize display pixels once, apply the reported ROI once, then scale without a hidden crop."""
    source, crop, foreground = resolved["source"], resolved["crop_pixels"], resolved["foreground"]
    width, height = resolved["width"], resolved["height"]
    graph = (f"[0:v]fps={config.OUT_FPS},scale={source['width']}:{source['height']},setsar=1,"
             f"crop={crop['width']}:{crop['height']}:{crop['x']}:{crop['y']}")
    if resolved["fit"] == "crop" or (foreground["width"], foreground["height"]) == (width, height):
        return graph + f",scale={width}:{height},setsar=1[v0];"
    bg_w, bg_h = max(2, 2 * int(width / 8)), max(2, 2 * int(height / 8))
    radius = min(6, max(1, min(bg_w, bg_h) // 4))
    return (graph + ",split=2[a][b];"
            f"[a]scale={bg_w}:{bg_h},boxblur={radius}:2,scale={width}:{height},"
            f"eq=brightness=-0.14:saturation=1.15[bg];"
            f"[b]scale={foreground['width']}:{foreground['height']},setsar=1[fg];"
            f"[bg][fg]overlay={foreground['x']}:{foreground['y']},setsar=1[v0];")


def voice_fingerprint(vo_cfg):
    settings = {"engine": "edge-tts", "script": voice._clean_script(vo_cfg.get("script") or ""),
                "voice": voice.voice_info(vo_cfg.get("voice", voice.DEFAULT_VOICE))["id"],
                "rate": vo_cfg.get("rate", "+0%")}
    return hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _saved_voice_words(asset, audio_path, script=None, cancel=None):
    words = asset.get("words")
    if words:
        if not isinstance(words, list) or any(not isinstance(w, dict) or not isinstance(w.get("w"), str)
                                             or not w["w"].strip() for w in words):
            raise RuntimeError("Saved narration word timings are invalid; recovery is required.")
        try:
            for w in words:
                if cancel:
                    cancel()
                _number(w.get("s"), "narration word start", 0, 1e10)
                _number(w.get("e"), "narration word end", w["s"], 1e10)
        except ValueError:
            raise RuntimeError("Saved narration word timings are invalid; recovery is required.") from None
        return copy.deepcopy(words)
    ass_path = asset.get("ass")
    if ass_path is None:
        stem = audio_path.stem[:-3] if audio_path.stem.endswith("_vo") else audio_path.stem
        ass_path = audio_path.with_name(stem + ".ass")
    try:
        ass = Path(ass_path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        raise RuntimeError("Could not recover saved narration word timings. Restore its ASS/timings or explicitly regenerate narration.") from None

    def seconds(ts):
        h, m, s = ts.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    events = []
    caption_font = None
    for line in ass.splitlines():
        if cancel:
            cancel()
        if line.startswith("Style: Cap,"):
            caption_font = line.split(",", 2)[1]
        fields = line.split(",", 9)
        if len(fields) != 10 or fields[0].strip() not in ("Dialogue: 1", "Dialogue: 2") or fields[3] != "Cap":
            continue
        text = re.sub(r"\{[^}]*\}", "", fields[9]).strip()
        position = re.search(r"\\pos\(([^)]*)\)", fields[9])
        try:
            start, end = seconds(fields[1]), seconds(fields[2])
        except ValueError:
            continue
        if position and text and len(text.split()) == 1 and math.isfinite(start) and math.isfinite(end) and 0 <= start < end:
            events.append((fields[0].strip(), start, end, text, position[1], "\\fscx" in fields[9]))
    bases = [event for event in events if event[0] == "Dialogue: 1"]
    linked, group_ids, recovered = set(), {}, []
    for layer, start, end, text, position, _ in events:
        if cancel:
            cancel()
        if layer != "Dialogue: 2":
            continue
        first, last = start, end
        for i, (_, a, b, word, pos, said) in enumerate(bases):
            if word != text or pos != position:
                continue
            if not said and abs(b - start) < 1e-6:
                first = min(first, a)
                linked.add(i)
            if said and abs(a - end) < 1e-6:
                last = max(last, b)
                linked.add(i)
        group = group_ids.setdefault((first, last), len(group_ids))
        # ASS times already contain the caption lead and hold. Undo the lead, but do not add the hold twice.
        recovered.append({"w": text, "s": start + CAPTION_LEAD, "e": end + CAPTION_LEAD,
                          "timing": "ass-recovered", "caption_group": group, "caption_font": caption_font})
    recovered.sort(key=lambda w: w["s"])
    expected = _key(_clean(voice._clean_script(script)).upper()).replace("'", "") if script is not None else None
    actual = _key("".join(w["w"] for w in recovered).upper()).replace("'", "")
    if (not recovered or caption_font not in FONT_FILES or len(linked) != len(bases)
            or (expected is not None and actual != expected)):
        raise RuntimeError("Could not recover complete saved narration word timings from ASS. Explicit narration regeneration is required.")
    return recovered


_qsv_ok = None
RENDER_SECONDS = 600      # one clip's video encode; far beyond normal, so a frozen encoder is stopped, not waited on


def render_clip(src: Path, out_dir: Path, name: str, clip: dict, words: list, src_w: int, src_h: int, jid: str = None,
                *, source_meta=None, on_progress=None, cancel=None, voice_asset=None) -> dict:
    """Writes <name>.mp4, <name>.jpg and <name>.ass into out_dir. Returns file names."""
    if cancel:
        cancel()
    report_progress(on_progress, "prepare")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", name):
        raise ValueError("Render name must be a short artifact identifier.")
    start, end = clip["start"], clip["end"]
    _number(start, "clip start", 0, 1e10)
    _number(end, "clip end", 0, 1e10)
    if end <= start:
        raise ValueError("Clip end must be after its start.")
    visual = validate_visual(clip.get("visual"))
    meta = dict(source_meta) if source_meta is not None else probe(src, jid=jid, cancel=cancel)
    resolved = _resolve_visual(clip, meta, visual, legacy_size=(src_w, src_h) if source_meta is None else None)
    layout, fg_h, hook_y, cap_y = resolved["layout"], resolved["foreground"]["height"], resolved["hook_y"], resolved["caption_y"]
    customized = visual is not None and ("headline" in visual or resolved["mode"] == "manual")
    headline_png = _headline(clip.get("hook", ""), visual, resolved)[0] if resolved["headline"] else None
    if cancel:
        cancel()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if any((out_dir / f"{name}{suffix}").exists() for suffix in (".mp4", ".jpg", ".ass", "_vo.mp3")):
        raise RuntimeError("Render artifacts for this revision already exist. Use a fresh revision name.")

    # The clip is made in a short work folder and moved into place afterwards. The caption step of ffmpeg cannot
    # open files whose full path is longer than 260 characters on Windows, which a data folder deep inside other
    # folders easily reaches. Inside the work folder every name is short and relative (a drive colon would also
    # break the filter syntax).
    work = Path(tempfile.mkdtemp(prefix="pk"))
    try:
        vo_cfg = clip.get("voiceover") or {}
        vo_file = None
        vo_metadata = None
        vo_mode = vo_cfg.get("mode", "mute") if vo_cfg.get("mode") in ("mute", "duck") else "mute"
        caption_words, caption_start, caption_end = words, start, end
        if vo_cfg.get("enabled") and (vo_cfg.get("script") or "").strip():
            fingerprint = voice_fingerprint(vo_cfg)
            vo_file = work / "v.mp3"
            reuse = voice_asset is not None and voice_asset.get("fingerprint") in (None, "", fingerprint)
            report_progress(on_progress, "voice-reuse" if reuse else "voice")
            if cancel:
                cancel()
            if reuse:
                saved = Path(voice_asset.get("path") or "")
                if not saved.is_file():
                    raise RuntimeError("Saved narration audio is missing. Restore it or explicitly regenerate narration.")
                caption_words = _saved_voice_words(voice_asset, saved, script=vo_cfg["script"], cancel=cancel)
                if cancel:
                    cancel()
                shutil.copyfile(saved, vo_file)
            else:
                caption_words = voice.generate(vo_cfg["script"], vo_cfg.get("voice", "en-US-AndrewNeural"),
                                               vo_file, rate=vo_cfg.get("rate", "+0%"))
            if cancel:
                cancel()
            if not vo_file.is_file() or not vo_file.stat().st_size:
                raise RuntimeError("Narration generation did not produce usable audio.")
            caption_start, caption_end = 0.0, end - start
            vo_metadata = {"file": f"{name}_vo.mp3", "words": copy.deepcopy(caption_words), "fingerprint": fingerprint}
        report_progress(on_progress, "captions")
        if cancel:
            cancel()
        ass = build_ass(caption_words, caption_start, caption_end, clip.get("hook", ""), clip.get("style", "bounce"),
                        clip.get("highlight_words", []), hook_y, cap_y, width=resolved["width"],
                        height=resolved["height"], omit_hook=customized)
        result = _render_in(work, src, out_dir, name, start, end, layout, fg_h, ass, vo_file=vo_file, vo_mode=vo_mode,
                            jid=jid, resolved=resolved, headline_png=headline_png, source_audio=meta.get("audio", True),
                            on_progress=on_progress, cancel=cancel)
        result["geometry"] = resolved
        if resolved["mode"] == "manual":
            result["layout"] = clip.get("layout", "blur")
        if vo_metadata is not None:
            result["voice_asset"] = vo_metadata
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _render_in(work: Path, src: Path, out_dir: Path, name: str, start, end, layout, fg_h, ass: str,
               vo_file: Path = None, vo_mode: str = "mute", jid: str = None, *, resolved=None, headline_png=None,
               source_audio=True, on_progress=None, cancel=None) -> dict:
    global _qsv_ok
    dur = end - start
    if cancel:
        cancel()
    (work / "c.ass").write_text(ass, encoding="utf-8")
    (work / "f").mkdir()
    for f in config.FONTS_DIR.glob("*.ttf"):
        if cancel:
            cancel()
        shutil.copyfile(f, work / "f" / f.name)
    fc = _visual_filter(resolved) if resolved and resolved["mode"] == "manual" else _filter(layout, fg_h)
    fc += "[v0]ass=c.ass:fontsdir=f" + ("[caps]" if headline_png else "[v]")
    inputs = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", str(Path(src).resolve())]
    maps = ["-map", "[v]"]
    if vo_file and vo_file.is_file():
        inputs += ["-i", vo_file.name]
        if vo_mode == "duck" and source_audio:
            fc += f";[0:a]volume=0.18[bg];[1:a]apad[vo];[vo][bg]amix=inputs=2:duration=first:dropout_transition=2,atrim=0:{dur:.2f},loudnorm=I=-14:TP=-1.5:LRA=11[a]"
        else:
            fc += f";[1:a]apad,atrim=0:{dur:.2f},loudnorm=I=-14:TP=-1.5:LRA=11[a]"
        maps += ["-map", "[a]"]
    else:
        maps += ["-map", "0:a:0?", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
    if headline_png:
        (work / "h.png").write_bytes(headline_png)
        index = 2 if vo_file else 1
        inputs += ["-loop", "1", "-framerate", str(config.OUT_FPS), "-i", "h.png"]
        headline = resolved["headline"]
        fc += (f";[{index}:v]format=rgba,fade=t=in:st=0:d=0.16:alpha=1[hook];"
               f"[caps][hook]overlay={headline['x']}:{headline['y']}:shortest=1[v]")
    base = inputs + ["-filter_complex", fc] + maps + ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", "-pix_fmt"]
    encoders = []
    if _qsv_ok is not False:
        encoders.append(("qsv", ["nv12", "-c:v", "h264_qsv", "-global_quality", "23", "-preset", "fast"]))
    encoders.append(("x264", ["yuv420p", "-c:v", "libx264", "-crf", "21", "-preset", "veryfast"]))
    err = None
    for attempt, (label, enc) in enumerate(encoders, 1):
        if cancel:
            cancel()
        try:
            run(base + enc + ["c.mp4"], cwd=work, timeout=RENDER_SECONDS, jid=jid, cancel=cancel,
                on_progress=on_progress, duration=dur, phase="encode", phase_attempt=attempt)
            if label == "qsv":
                _qsv_ok = True
            break
        except ProcessFailed as error:
            err = str(error)[-1200:]
            if cancel:
                cancel()
            if label == "qsv":
                _qsv_ok = False
    else:
        raise RuntimeError(f"Render failed: {err}")
    cover_t = min(1.2, (end - start) / 2)
    report_progress(on_progress, "cover")
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{cover_t:.2f}", "-i", "c.mp4", "-frames:v", "1",
         "-q:v", "3", "c.jpg"], cwd=work, jid=jid, cancel=cancel, timeout=60)
    artifacts = [(work / f"c.{ext}", out_dir / f"{name}.{ext}") for ext in ("mp4", "jpg", "ass")]
    if vo_file:
        artifacts.append((vo_file, out_dir / f"{name}_vo.mp3"))
    if any(not path.is_file() or not path.stat().st_size for path, _ in artifacts):
        raise RuntimeError("Render finished without all required artifacts.")
    report_progress(on_progress, "publication")
    if any(destination.exists() for _, destination in artifacts):
        raise RuntimeError("Render artifacts for this revision already exist. Use a fresh revision name.")
    moved = []
    try:
        for path, destination in artifacts:
            if cancel:
                cancel()
            if destination.exists():
                raise RuntimeError("Render artifacts for this revision already exist. Use a fresh revision name.")
            shutil.move(str(path), str(destination))
            moved.append(destination)
        if cancel:
            cancel()
    except BaseException:
        for destination in moved:
            destination.unlink(missing_ok=True)
        raise
    return {"file": f"{name}.mp4", "cover": f"{name}.jpg", "layout": layout}
