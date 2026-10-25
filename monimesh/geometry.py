"""Pure helpers for modes, scales and quality presets (no I/O)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction

# Hyprland only accepts scales where width/scale and height/scale are integers.
# Candidates are k/120 in [0.5, 3], kept "nice" (small denominator) for the UI.
_SCALE_CANDIDATES = sorted(
    {Fraction(k, 120) for k in range(60, 361) if Fraction(k, 120).denominator <= 8}
)

TRANSFORMS = [
    "Normal",
    "90°",
    "180°",
    "270°",
    "Flipped",
    "Flipped 90°",
    "Flipped 180°",
    "Flipped 270°",
]


@dataclass(frozen=True)
class Mode:
    width: int
    height: int
    refresh: float = 60.0

    @classmethod
    def parse(cls, text: str) -> "Mode":
        """Parse '1920x1080@60', '1920x1080@144.00Hz' or '1920x1080'."""
        text = text.strip().removesuffix("Hz")
        res, _, rate = text.partition("@")
        w, _, h = res.partition("x")
        return cls(int(w), int(h), float(rate) if rate else 60.0)

    def __str__(self) -> str:
        rate = f"{self.refresh:.2f}".rstrip("0").rstrip(".")
        return f"{self.width}x{self.height}@{rate}"

    @property
    def label(self) -> str:
        return f"{self.width} × {self.height} @ {round(self.refresh)} Hz"


def fmt_scale(scale: float) -> str:
    return f"{scale:.4f}".rstrip("0").rstrip(".")


def valid_scales(width: int, height: int) -> list[float]:
    """Scales that give an integer logical size for this mode."""
    out = []
    for s in _SCALE_CANDIDATES:
        if (width / s).denominator == 1 and (height / s).denominator == 1:
            out.append(float(s))
    return out or [1.0]


def nearest_valid_scale(width: int, height: int, scale: float) -> float:
    return min(valid_scales(width, height), key=lambda s: abs(s - scale))


def logical_size(width: int, height: int, scale: float, transform: int = 0) -> tuple[int, int]:
    w, h = round(width / scale), round(height / scale)
    return (h, w) if transform % 2 else (w, h)


@dataclass(frozen=True)
class Preset:
    key: str
    title: str
    mode: Mode
    scale: float
    est_fps: int

    @property
    def subtitle(self) -> str:
        lw, lh = logical_size(self.mode.width, self.mode.height, self.scale)
        return f"{self.mode.width}×{self.mode.height}, scale {fmt_scale(self.scale)} → {lw}×{lh} workspace, ~{self.est_fps} fps"


# wayvnc encodes on the CPU; measured on a 2800x1752 Galaxy Tab S10+ with a
# full-screen 60 fps animation. Pixel count is what matters.
_FPS_SAMPLES = [(1.92e6, 59), (2.30e6, 50), (3.14e6, 30), (4.90e6, 28)]


def estimate_fps(pixels: float) -> int:
    pts = _FPS_SAMPLES
    if pixels <= pts[0][0]:
        return 60
    for (p0, f0), (p1, f1) in zip(pts, pts[1:]):
        if pixels <= p1:
            return round(f0 + (f1 - f0) * (pixels - p0) / (p1 - p0))
    return max(10, round(pts[-1][1] * pts[-1][0] / pixels))


def _fit(logical_w: int, logical_h: int, scale: Fraction) -> Mode:
    # physical = logical * scale must be integral -> logical multiple of denominator
    den = scale.denominator
    lw = max(den, round(logical_w / den) * den)
    lh = max(den, round(logical_h / den) * den)
    return Mode(int(lw * scale), int(lh * scale), 60.0)


def quality_presets(native_w: int, native_h: int, target_logical_h: int = 1000) -> list[Preset]:
    """Sharp / Balanced / Smooth presets for a device panel, all with ~the same workspace size."""
    if native_h > native_w:
        native_w, native_h = native_h, native_w
    nice = [s for s in _SCALE_CANDIDATES if s >= 1]

    def snap(value: float) -> Fraction:
        return min(nice, key=lambda s: abs(float(s) - value))

    sharp_scale = snap(native_h / target_logical_h)
    lw, lh = round(native_w / sharp_scale), round(native_h / sharp_scale)

    presets = []
    for key, title, pixels in (
        ("sharp", "Sharp", native_w * native_h),
        ("balanced", "Balanced", 2.3e6),
        ("smooth", "Smooth", 1.9e6),
    ):
        scale = snap(math.sqrt(pixels / (lw * lh)))
        if scale > sharp_scale:
            scale = sharp_scale
        mode = _fit(lw, lh, scale)
        if mode.width > native_w or mode.height > native_h:
            mode = _fit(lw - scale.denominator, lh - scale.denominator, scale)
        presets.append(Preset(key, title, mode, float(scale), estimate_fps(mode.width * mode.height)))
    return presets
