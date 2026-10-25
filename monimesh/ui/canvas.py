"""Arrangement canvas: drag displays around, with edge snapping and guides."""

from __future__ import annotations

import math

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, GObject, Gtk, Pango, PangoCairo  # noqa: E402

from ..geometry import fmt_scale  # noqa: E402
from ..hyprland import Output  # noqa: E402

SNAP_PX = 14          # snapping distance in screen pixels
PADDING = 40


def _rect(o: Output) -> tuple[int, int, int, int]:
    w, h = o.logical
    return o.x, o.y, w, h


def overlaps(a: Output, b: Output) -> bool:
    ax, ay, aw, ah = _rect(a)
    bx, by, bw, bh = _rect(b)
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def touches(a: Output, b: Output) -> bool:
    """True if the two rectangles share part of an edge (the cursor can cross)."""
    ax, ay, aw, ah = _rect(a)
    bx, by, bw, bh = _rect(b)
    vertical = (ax + aw == bx or bx + bw == ax) and min(ay + ah, by + bh) > max(ay, by)
    horizontal = (ay + ah == by or by + bh == ay) and min(ax + aw, bx + bw) > max(ax, bx)
    return vertical or horizontal


class Canvas(Gtk.DrawingArea):
    __gsignals__ = {
        "selected": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "moved": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self):
        super().__init__(hexpand=True, vexpand=True)
        self.outputs: list[Output] = []
        self.selected: str | None = None
        self._view: tuple[float, float, float] | None = None   # factor, ox, oy
        self._frozen_view = None
        self._drag: tuple[Output, int, int] | None = None
        self._guides: list[tuple[str, int]] = []

        self.set_draw_func(self._draw)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)
        Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self.queue_draw())
        Adw.StyleManager.get_default().connect("notify::accent-color", lambda *_: self.queue_draw())

    # -- public ------------------------------------------------------------------
    def set_outputs(self, outputs: list[Output]) -> None:
        self.outputs = outputs
        if self.selected not in {o.name for o in outputs}:
            self.selected = outputs[0].name if outputs else None
        self.queue_draw()

    def get(self, name: str | None) -> Output | None:
        return next((o for o in self.outputs if o.name == name), None)

    def select(self, name: str) -> None:
        self.selected = name
        self.queue_draw()
        self.emit("selected", name)

    def normalize(self) -> None:
        """Shift everything so the layout starts at (0, 0)."""
        active = [o for o in self.outputs if o.enabled]
        if not active:
            return
        mx = min(o.x for o in active)
        my = min(o.y for o in active)
        if mx or my:
            for o in self.outputs:
                o.x -= mx
                o.y -= my

    def problems(self) -> dict[str, str]:
        """name -> warning text for overlapping or unreachable displays."""
        active = [o for o in self.outputs if o.enabled]
        out = {}
        for o in active:
            others = [p for p in active if p is not o]
            if any(overlaps(o, p) for p in others):
                out[o.name] = "Overlaps another display"
            elif others and not any(touches(o, p) for p in others):
                out[o.name] = "Not touching any display: the cursor cannot reach it"
        return out

    # -- geometry ----------------------------------------------------------------
    def _compute_view(self, width: int, height: int) -> tuple[float, float, float]:
        if not self.outputs:
            return 1.0, 0.0, 0.0
        rects = [_rect(o) for o in self.outputs]
        x0 = min(r[0] for r in rects)
        y0 = min(r[1] for r in rects)
        x1 = max(r[0] + r[2] for r in rects)
        y1 = max(r[1] + r[3] for r in rects)
        bw, bh = max(1, x1 - x0), max(1, y1 - y0)
        # leave room around the layout so displays can be dragged outward
        factor = min((width - 2 * PADDING) / (bw * 1.35), (height - 2 * PADDING) / (bh * 1.35))
        factor = max(factor, 0.01)
        ox = (width - bw * factor) / 2 - x0 * factor
        oy = (height - bh * factor) / 2 - y0 * factor
        return factor, ox, oy

    def _to_screen(self, x: float, y: float) -> tuple[float, float]:
        f, ox, oy = self._view
        return ox + x * f, oy + y * f

    def _hit(self, sx: float, sy: float) -> Output | None:
        if not self._view:
            return None
        for o in reversed(self.outputs):
            x, y, w, h = _rect(o)
            x0, y0 = self._to_screen(x, y)
            f = self._view[0]
            if x0 <= sx <= x0 + w * f and y0 <= sy <= y0 + h * f:
                return o
        return None

    # -- dragging ----------------------------------------------------------------
    def _on_drag_begin(self, _g, sx, sy):
        o = self._hit(sx, sy)
        if not o:
            return
        self._frozen_view = self._view
        self._drag = (o, o.x, o.y)
        # draw the dragged display on top
        self.outputs.remove(o)
        self.outputs.append(o)
        if self.selected != o.name:
            self.select(o.name)

    def _on_drag_update(self, _g, dx, dy):
        if not self._drag:
            return
        o, x0, y0 = self._drag
        f = self._frozen_view[0]
        nx, ny = round(x0 + dx / f), round(y0 + dy / f)
        o.x, o.y, self._guides = self._snap(o, nx, ny, SNAP_PX / f)
        self.queue_draw()

    def _on_drag_end(self, _g, dx, dy):
        if not self._drag:
            return
        o, x0, y0 = self._drag
        self._drag = None
        self._guides = []
        self._frozen_view = None
        moved = (o.x, o.y) != (x0, y0)
        if moved:
            self.normalize()
        self.queue_draw()
        if moved:
            self.emit("moved", o.name)

    def _snap(self, o: Output, nx: int, ny: int, threshold: float):
        w, h = o.logical
        best_x = (threshold, nx, None)
        best_y = (threshold, ny, None)
        for p in self.outputs:
            if p is o or not p.enabled:
                continue
            px, py, pw, ph = _rect(p)
            for cand, guide in (
                (px + pw, px + pw), (px - w, px), (px, px), (px + pw - w, px + pw),
                (px + (pw - w) // 2, px + pw // 2),
            ):
                d = abs(nx - cand)
                if d < best_x[0]:
                    best_x = (d, cand, guide)
            for cand, guide in (
                (py + ph, py + ph), (py - h, py), (py, py), (py + ph - h, py + ph),
                (py + (ph - h) // 2, py + ph // 2),
            ):
                d = abs(ny - cand)
                if d < best_y[0]:
                    best_y = (d, cand, guide)
        guides = []
        if best_x[2] is not None:
            guides.append(("v", best_x[2]))
        if best_y[2] is not None:
            guides.append(("h", best_y[2]))
        return best_x[1], best_y[1], guides

    # -- drawing -----------------------------------------------------------------
    def _draw(self, _area, cr, width, height):
        sm = Adw.StyleManager.get_default()
        dark = sm.get_dark()
        accent = self._accent(sm)
        fg = (1, 1, 1) if dark else (0, 0, 0)

        self._view = self._frozen_view or self._compute_view(width, height)
        f = self._view[0]
        problems = self.problems()

        if self._guides:
            cr.set_source_rgba(accent.red, accent.green, accent.blue, 0.8)
            cr.set_line_width(1)
            cr.set_dash([4, 4])
            for kind, v in self._guides:
                if kind == "v":
                    x, _ = self._to_screen(v, 0)
                    cr.move_to(round(x) + 0.5, 0)
                    cr.line_to(round(x) + 0.5, height)
                else:
                    _, y = self._to_screen(0, v)
                    cr.move_to(0, round(y) + 0.5)
                    cr.line_to(width, round(y) + 0.5)
            cr.stroke()
            cr.set_dash([])

        for o in self.outputs:
            x, y, w, h = _rect(o)
            sx, sy = self._to_screen(x, y)
            sw, sh = w * f, h * f
            selected = o.name == self.selected
            alpha = 1.0 if (o.enabled and o.present) else 0.45

            self._rounded(cr, sx + 1, sy + 1, sw - 2, sh - 2, 10)
            if selected:
                cr.set_source_rgba(accent.red, accent.green, accent.blue, 0.28 * alpha)
            else:
                cr.set_source_rgba(*fg, (0.10 if dark else 0.06) * alpha)
            cr.fill_preserve()

            if o.name in problems:
                cr.set_source_rgba(0.88, 0.11, 0.14, 1)
            elif selected:
                cr.set_source_rgba(accent.red, accent.green, accent.blue, 1)
            else:
                cr.set_source_rgba(*fg, 0.35 * alpha)
            cr.set_line_width(2.5 if selected else 1.5)
            if o.virtual:
                cr.set_dash([7, 5])
            cr.stroke()
            cr.set_dash([])

            lw, lh = o.logical
            details = [o.description or ("Virtual display" if o.virtual else "")]
            if not o.present:
                details = ["Not connected"]
            elif not o.enabled:
                details = ["Disabled"]
            details.append(f"{lw} × {lh} · {fmt_scale(o.scale)}×")
            self._label(cr, o.name, details, sx, sy, sw, sh, fg, alpha)

    def _accent(self, sm):
        # Prefer the GTK theme's accent (custom themes) over libadwaita's setting.
        try:
            ok, color = self.get_style_context().lookup_color("accent_bg_color")
            if ok:
                return color
        except (AttributeError, TypeError):
            pass
        return sm.get_accent_color_rgba()

    @staticmethod
    def _rounded(cr, x, y, w, h, r):
        r = min(r, w / 2, h / 2)
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

    def _label(self, cr, title, lines, x, y, w, h, fg, alpha):
        layout = self.create_pango_layout("")   # widget font, not the cairo default
        layout.set_alignment(Pango.Alignment.CENTER)
        layout.set_width(int(max(10, w - 16) * Pango.SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        small = "\n".join(GLib.markup_escape_text(s) for s in lines if s)
        size = "large" if w > 160 else "medium"
        layout.set_markup(
            f'<span weight="bold" size="{size}">{GLib.markup_escape_text(title)}</span>\n'
            f'<span size="small">{small}</span>',
            -1,
        )
        _, ext = layout.get_pixel_extents()
        if ext.height > h - 8:
            layout.set_markup(f'<span weight="bold">{GLib.markup_escape_text(title)}</span>', -1)
            _, ext = layout.get_pixel_extents()
        cr.set_source_rgba(*fg, 0.9 * alpha)
        cr.move_to(x + 8, y + (h - ext.height) / 2)
        PangoCairo.show_layout(cr, layout)

