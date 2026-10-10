"""Render a timelapse frame: thermal sky image + colour bar + conditions panel.

The palette and display-range logic mirror heatmapColor() / getDisplayRange() /
fillPaletteIndices() in SkyConditions_90640/src/sensors/sky_sensor.cpp so the
frames match the sensor's own live view.
"""

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HOT_INDEX = 255
HOT_MARGIN_C = 20.0
WARM_START_C = 0.0

CDS_STOPS = [
    (0, 62, 126), (19, 83, 147), (38, 102, 166), (78, 142, 206), (98, 162, 226),
    (118, 182, 246), (153, 217, 217), (173, 237, 237), (193, 193, 193),
    (233, 233, 233), (250, 250, 250),
]
WARM_POS = [0.0, 0.2, 0.5, 0.8, 1.0]
WARM_STOPS = [(250, 250, 250), (255, 230, 90), (255, 150, 40), (220, 40, 30), (150, 0, 40)]

# Theme – same palette as the sensor's web UI (html_templates.h).
BG = (26, 26, 46)          # #1a1a2e
CARD = (22, 33, 62)        # #16213e
BOX = (15, 52, 96)         # #0f3460
BORDER = (45, 53, 97)      # #2d3561
TEXT = (224, 224, 224)     # #e0e0e0
HEADING = (160, 196, 255)  # #a0c4ff
LABEL = (116, 185, 255)    # #74b9ff
MUTED = (178, 190, 195)    # #b2bec3

FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")


def _font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(str(FONT_DIR / name), size)
    except OSError:
        return ImageFont.load_default()


def _lerp(a, b, f):
    return tuple(int(a[i] + f * (b[i] - a[i])) for i in range(3))


def _cds(t):
    if t <= 0.0:
        return CDS_STOPS[0]
    if t >= 1.0:
        return CDS_STOPS[10]
    lo = int(t * 10.0)
    return _lerp(CDS_STOPS[lo], CDS_STOPS[lo + 1], t * 10.0 - lo)


def palette(scale):
    """256-entry RGB lookup table, identical to heatmapColor()."""
    lut = np.zeros((256, 3), dtype=np.uint8)
    lo, hi = scale["fixed_min"], scale["fixed_max"]
    for idx in range(256):
        if idx == HOT_INDEX:
            lut[idx] = (255, 0, 255)
            continue
        t = idx / (HOT_INDEX - 1)
        if scale["mode"] != "fixed":
            lut[idx] = _cds(t)
            continue
        T = lo + t * (hi - lo)
        split = min(max(WARM_START_C, lo), hi)
        if T <= split:
            lut[idx] = _cds((T - lo) / (split - lo) if split > lo else 1.0)
            continue
        w = (T - split) / (hi - split)
        k = 1
        while k < 4 and w > WARM_POS[k]:
            k += 1
        f = (w - WARM_POS[k - 1]) / (WARM_POS[k] - WARM_POS[k - 1])
        lut[idx] = _lerp(WARM_STOPS[k - 1], WARM_STOPS[k], f)
    return lut


def display_range(scale, ambient):
    """(cold_end, span, hot_above) – mirrors SkyConditions::getDisplayRange()."""
    if scale["mode"] == "fixed":
        cold = scale["fixed_min"]
        span = max(1.0, scale["fixed_max"] - scale["fixed_min"])
        return cold, span, cold + span
    cold = ambient - scale["clear_delta"]
    span = max(1.0, scale["clear_delta"] - scale["overcast_delta"])
    return cold, span, ambient + HOT_MARGIN_C


def palette_indices(frame, scale, ambient):
    """32×24 °C array -> palette indices, with the sensor's 3×3 display blur."""
    cold, span, hot_above = display_range(scale, ambient)
    padded = np.pad(frame, 1, mode="constant", constant_values=np.nan)
    stack = np.stack([padded[1 + dr:1 + dr + frame.shape[0], 1 + dc:1 + dc + frame.shape[1]]
                      for dr in (-1, 0, 1) for dc in (-1, 0, 1)])
    blurred = np.nanmean(stack, axis=0)
    hot = frame > hot_above
    T = np.where(hot, frame, blurred)
    t = np.clip((T - cold) / span, 0.0, 1.0)
    idx = np.floor(t * (HOT_INDEX - 1) + 0.5).astype(np.uint8)
    if scale["hot_marker"]:
        idx[hot] = HOT_INDEX
    return idx


def thermal_image(matrix, scale, size=(640, 480)):
    frame = np.array(matrix["pixels"], dtype=np.float32)
    idx = palette_indices(frame, scale, matrix["ambient"])
    rgb = palette(scale)[idx]
    # Bicubic upscale, as the sensor's live view does in the browser.
    return Image.fromarray(rgb, "RGB").resize(size, Image.BICUBIC)


# ---------------------------------------------------------------------------
# Composite frame
# ---------------------------------------------------------------------------

def _fmt(v, fmt, unit=""):
    return "--" if v is None else (fmt.format(v) + unit)


def _brightness(v):
    if v is None:
        return "--"
    return f"{v:.0f} lx" if v >= 100 else f"{v:.3g} lx"


def stat_items(r):
    raining = r.get("raining")
    rain_txt, rain_col = (("--", MUTED) if raining is None else
                          (("Raining", (255, 118, 117)) if raining else ("Dry", (85, 239, 196))))
    return [
        ("Cloud Cover", _fmt(r.get("cloud_cover"), "{:.0f}", " %"), (223, 230, 233)),
        ("Ambient Temp", _fmt(r.get("ambient_temp"), "{:.1f}", " °C"), (255, 234, 167)),
        ("Sky Temp", _fmt(r.get("sky_temp"), "{:.1f}", " °C"), (162, 155, 254)),
        ("Brightness", _brightness(r.get("brightness")), (253, 203, 110)),
        ("SQM", _fmt(r.get("sqm"), "{:.2f}", ""), HEADING),
        ("Pressure", _fmt(r.get("pressure"), "{:.1f}", " hPa"), (129, 236, 236)),
        ("Humidity", _fmt(r.get("humidity"), "{:.1f}", " %"), LABEL),
        ("Rain", rain_txt, rain_col),
    ]


def _color_bar(draw, x, y, h, scale, ambient, lut):
    cold, span, _ = display_range(scale, ambient)
    f_small = _font(13)
    bar_w = 16
    top = y
    if scale["hot_marker"]:
        draw.rounded_rectangle([x, y, x + bar_w, y + 14], 3, fill=(255, 0, 255))
        hot_lbl = (cold + span) if scale["mode"] == "fixed" else (ambient + HOT_MARGIN_C)
        draw.text((x + bar_w + 6, y + 7), f">{hot_lbl:.1f}°C", font=f_small, fill=MUTED, anchor="lm")
        top = y + 20
    bot = y + h
    for py in range(top, bot):
        t = 1.0 - (py - top) / max(1, bot - top - 1)
        draw.line([x, py, x + bar_w, py], fill=tuple(int(c) for c in lut[round(t * 254)]))
    draw.rectangle([x, top, x + bar_w, bot], outline=BOX)
    n = 5
    for i in range(n):
        t = 1 - i / (n - 1)
        pre = "≥" if (i == 0 and not (scale["hot_marker"] and scale["mode"] == "fixed")) else (
            "≤" if i == n - 1 else "")
        py = top + i * (bot - top) / (n - 1)
        anchor = "lt" if i == 0 else ("lb" if i == n - 1 else "lm")
        draw.text((x + bar_w + 6, py), f"{pre}{cold + t * span:.1f}°C", font=f_small,
                  fill=MUTED, anchor=anchor)


def compose_frame(matrix, readings, scale, when, location_name, title="CloudRoll"):
    """Build the full timelapse frame (800×740 RGB)."""
    W, H = 800, 740
    pad = 16
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Header
    d.text((pad, 22), title, font=_font(22, True), fill=HEADING, anchor="lm")
    d.text((W - pad, 22), f"{location_name}  ·  {when.strftime('%Y-%m-%d %H:%M %Z')}",
           font=_font(15), fill=TEXT, anchor="rm")

    # Thermal card
    card_y = 44
    d.rounded_rectangle([pad - 8, card_y, W - pad + 8, card_y + 480 + 16], 8, fill=CARD)
    timg = thermal_image(matrix, scale)
    img.paste(timg, (pad, card_y + 8))
    lut = palette(scale)
    _color_bar(d, pad + 640 + 14, card_y + 8, 480, scale, matrix["ambient"], lut)

    # Conditions grid: 4 × 2 stat boxes
    grid_y = card_y + 480 + 16 + 12
    d.rounded_rectangle([pad - 8, grid_y, W - pad + 8, H - 8], 8, fill=CARD)
    cols, gap = 4, 10
    bw = (W - 2 * pad - (cols - 1) * gap) / cols
    bh = 74
    f_lbl, f_val = _font(12), _font(22, True)
    for i, (label, value, col) in enumerate(stat_items(readings)):
        r, c = divmod(i, cols)
        bx = pad + c * (bw + gap)
        by = grid_y + 10 + r * (bh + gap)
        d.rounded_rectangle([bx, by, bx + bw, by + bh], 6, fill=BOX)
        d.text((bx + bw / 2, by + 20), label.upper(), font=f_lbl, fill=LABEL, anchor="mm")
        d.text((bx + bw / 2, by + 50), value, font=f_val, fill=col, anchor="mm")
    return img


def thermal_with_scale(matrix, scale):
    """Thermal image with its colour bar (no conditions panel) – 780×496."""
    img = Image.new("RGB", (780, 496), CARD)
    img.paste(thermal_image(matrix, scale), (8, 8))
    _color_bar(ImageDraw.Draw(img), 8 + 640 + 14, 8, 480, scale, matrix["ambient"],
               palette(scale))
    return img
