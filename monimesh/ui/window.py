"""Main window: arrangement canvas, per-display settings, profiles."""

from __future__ import annotations

import socket
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .. import __version__, android, hyprland, profiles, service, virtual  # noqa: E402
from ..config import Config, VirtualDisplay  # noqa: E402
from ..geometry import TRANSFORMS, Mode, fmt_scale, logical_size, nearest_valid_scale, quality_presets, valid_scales  # noqa: E402
from ..hyprland import Output  # noqa: E402
from .canvas import Canvas  # noqa: E402

POLL_SECONDS = 2.0
STATE_TEXT = {
    "waiting": "Waiting for the device — plug it in with USB debugging enabled",
    "unauthorized": "Allow USB debugging on the device",
    "streaming": "Streaming",
    "error": "Streaming failed",
}


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="Monimesh", default_width=1100, default_height=680)
        self.cfg = Config.load()
        self.dirty = False
        self.devices: list[android.Device] = []
        self.device_names: dict[str, str] = {}   # serial -> friendly name
        self.vd_state: dict[str, dict] = {}
        self._present_names: set[str] = set()
        self._updating = False          # suppress widget callbacks while syncing
        self._status_widgets = None     # (row, button) of the visible virtual display
        self._pos_rows = None           # (x_row, y_row) of the visible display

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        if not hyprland.available():
            page = Adw.StatusPage(
                icon_name="video-display-symbolic",
                title="Hyprland not detected",
                description="Monimesh currently supports Hyprland. Support for other "
                            "compositors is planned.",
            )
            view = Adw.ToolbarView(content=page)
            view.add_top_bar(Adw.HeaderBar())
            self.toasts.set_child(view)
            return

        self._build()
        self._install_actions()
        self.reload()
        self._poll_stop = threading.Event()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        self.connect("close-request", self._on_close)

    # ======================================================================
    # Layout
    # ======================================================================
    def _build(self) -> None:
        header = Adw.HeaderBar()

        self.profiles_btn = Gtk.MenuButton(label="Profiles", tooltip_text="Saved layouts")
        self.profiles_btn.set_create_popup_func(self._fill_profiles_popover)
        self.profiles_btn.set_popover(Gtk.Popover())
        header.pack_start(self.profiles_btn)

        add_btn = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add a virtual display (Android device over USB)")
        add_btn.connect("clicked", lambda *_: self._add_virtual_dialog())
        header.pack_start(add_btn)

        menu = Gio.Menu()
        menu.append("Save Layout to Hyprland Config", "win.persist")
        menu.append("Preferences", "win.preferences")
        menu.append("About Monimesh", "win.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True))

        self.apply_btn = Gtk.Button(label="Apply", sensitive=False)
        self.apply_btn.add_css_class("suggested-action")
        self.apply_btn.connect("clicked", lambda *_: self._apply())
        header.pack_end(self.apply_btn)

        reset = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Discard changes and reload from the compositor")
        reset.connect("clicked", lambda *_: self.reload())
        header.pack_end(reset)

        self.banner = Adw.Banner(
            title="Changes are lost when Hyprland reloads its config",
            button_label="Save to Config",
        )
        self.banner.connect("button-clicked", lambda *_: self._persist())

        self.canvas = Canvas()
        self.canvas.connect("selected", lambda *_: self._build_panel())
        self.canvas.connect("moved", self._on_canvas_moved)

        hint = Gtk.Label(
            label="Drag displays to arrange them — edges snap together. Dashed outlines are virtual displays.",
            wrap=True, margin_bottom=10, margin_start=12, margin_end=12,
        )
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self.canvas)
        content.append(hint)

        self.split = Adw.OverlaySplitView(
            content=content, sidebar_position=Gtk.PackType.END,
            min_sidebar_width=340, max_sidebar_width=420, sidebar_width_fraction=0.36,
        )

        view = Adw.ToolbarView(content=self.split)
        view.add_top_bar(header)
        view.add_top_bar(self.banner)
        self.toasts.set_child(view)

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 720sp"))
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)

    def _install_actions(self) -> None:
        for name, cb in (
            ("persist", lambda *_: self._persist()),
            ("preferences", lambda *_: self._preferences()),
            ("about", lambda *_: self._about()),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)

    def toast(self, text: str, button: str | None = None, callback=None, timeout: int = 4) -> None:
        t = Adw.Toast(title=GLib.markup_escape_text(text), timeout=timeout)
        if button and callback:
            t.set_button_label(button)
            t.connect("button-clicked", lambda *_: callback())
        self.toasts.add_toast(t)

    # ======================================================================
    # State
    # ======================================================================
    def reload(self) -> None:
        self.cfg = Config.load()
        try:
            outs = hyprland.outputs()
        except hyprland.HyprlandError as e:
            self.toast(f"Cannot read outputs: {e}")
            outs = []
        present = {o.name for o in outs}
        self._present_names = present
        for o in outs:
            vd = self.cfg.virtual_displays.get(o.name)
            o.virtual = vd is not None
            if vd:
                o.description = vd.device_label or "Virtual display"
        for name, vd in self.cfg.virtual_displays.items():
            if name not in present:
                outs.append(self._vd_output(vd, present=False))
        selected = self.canvas.selected
        self.canvas.set_outputs(outs)
        if selected and self.canvas.get(selected):
            self.canvas.selected = selected
        self._set_dirty(False)
        self.banner.set_revealed(not hyprland.is_included())
        self._build_panel()

    @staticmethod
    def _vd_output(vd: VirtualDisplay, present: bool) -> Output:
        mode = Mode.parse(vd.mode)
        return Output(
            name=vd.name, description=vd.device_label or "Virtual display", mode=mode,
            x=vd.position[0], y=vd.position[1], scale=vd.scale, transform=vd.transform,
            modes=[mode], virtual=True, present=present,
        )

    def _set_dirty(self, dirty: bool = True) -> None:
        self.dirty = dirty
        self.apply_btn.set_sensitive(dirty)
        self.canvas.queue_draw()

    def _changed(self, rebuild: bool = False) -> None:
        self._set_dirty(True)
        if rebuild:
            # not from inside the signal of a row that is about to be destroyed
            GLib.idle_add(lambda: (self._build_panel(), False)[1])
        else:
            self._sync_position_rows()

    def _on_canvas_moved(self, _canvas, _name) -> None:
        self._changed(rebuild=bool(self.canvas.problems()) or self._has_warning_row)

    def _sync_position_rows(self) -> None:
        o = self.canvas.get(self.canvas.selected)
        if not o or not self._pos_rows:
            return
        self._updating = True
        self._pos_rows[0].set_value(o.x)
        self._pos_rows[1].set_value(o.y)
        self._updating = False

    # ======================================================================
    # Applying
    # ======================================================================
    def _save_virtual_geometry(self) -> None:
        for o in self.canvas.outputs:
            vd = self.cfg.virtual_displays.get(o.name)
            if vd:
                vd.mode, vd.scale, vd.transform = str(o.mode), o.scale, o.transform
                vd.position = [o.x, o.y]
        self.cfg.save()

    def _apply(self) -> None:
        problems = self.canvas.problems()
        if any(p.startswith("Overlaps") for p in problems.values()):
            self.toast("Displays overlap — move them apart first")
            return
        if not any(o.enabled for o in self.canvas.outputs if o.present):
            self.toast("At least one display must stay enabled")
            return
        try:
            before = hyprland.outputs()
        except hyprland.HyprlandError as e:
            self.toast(str(e))
            return
        before_by_name = {o.name: o.to_dict() for o in before}
        targets = [o for o in self.canvas.outputs if o.present]
        physical_changed = any(
            before_by_name.get(o.name) != o.to_dict() for o in targets if not o.virtual
        )
        try:
            hyprland.apply(targets)
        except hyprland.HyprlandError as e:
            self.toast(f"Hyprland refused the change: {e}")
            return
        self._save_virtual_geometry()

        def keep():
            self._set_dirty(False)
            if hyprland.is_included():
                hyprland.persist(self.canvas.outputs)
            self.toast("Display settings applied")
            GLib.timeout_add(400, self._reload_once)

        def revert():
            try:
                hyprland.apply(before)
            except hyprland.HyprlandError as e:
                self.toast(f"Revert failed: {e}")
            self.toast("Previous settings restored")
            GLib.timeout_add(400, self._reload_once)

        if physical_changed:
            self._confirm_dialog(keep, revert)
        else:
            keep()

    def _reload_once(self) -> bool:
        self.reload()
        return False

    def _confirm_dialog(self, keep, revert) -> None:
        seconds = [self.cfg.confirm_seconds]
        dialog = Adw.AlertDialog(
            heading="Keep these display settings?",
            body=f"Reverting in {seconds[0]} seconds…",
        )
        dialog.add_response("revert", "Revert")
        dialog.add_response("keep", "Keep Changes")
        dialog.set_response_appearance("keep", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("keep")
        dialog.set_close_response("revert")
        done = [False]

        def finish(response):
            if done[0]:
                return
            done[0] = True
            (keep if response == "keep" else revert)()

        def tick():
            if done[0]:
                return False
            seconds[0] -= 1
            if seconds[0] <= 0:
                finish("revert")
                dialog.force_close()
                return False
            dialog.set_body(f"Reverting in {seconds[0]} seconds…")
            return True

        dialog.connect("response", lambda _d, r: finish(r))
        GLib.timeout_add_seconds(1, tick)
        dialog.present(self)

    def _persist(self) -> None:
        path = hyprland.persist(self.canvas.outputs)
        if hyprland.is_included():
            self.banner.set_revealed(False)
            self.toast(f"Saved to {path.name}")
            return
        dialog = Adw.AlertDialog(
            heading="Load the layout at startup?",
            body=f"The layout was written to {path}. Hyprland loads it only if "
                 f"{hyprland.main_config().name} contains:\n\n{hyprland.include_line()}\n\n"
                 "Monimesh can add this line at the end of the file for you.",
        )
        dialog.add_response("cancel", "Not Now")
        dialog.add_response("add", "Add Line")
        dialog.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_d, response):
            if response != "add":
                return
            try:
                hyprland.add_include()
            except OSError as e:
                self.toast(f"Could not edit config: {e}")
                return
            self.banner.set_revealed(False)
            self.toast("Layout is now loaded at startup")

        dialog.connect("response", on_response)
        dialog.present(self)

    # ======================================================================
    # Side panel
    # ======================================================================
    def _build_panel(self) -> None:
        o = self.canvas.get(self.canvas.selected)
        page = Adw.PreferencesPage()
        self._status_widgets = None
        self._pos_rows = None
        self._has_warning_row = False
        if o is None:
            page.add(Adw.PreferencesGroup(title="No display selected"))
            self.split.set_sidebar(page)
            return

        vd = self.cfg.virtual_displays.get(o.name)
        if vd:
            page.add(self._stream_group(o, vd))
        page.add(self._display_group(o, vd))
        if vd:
            page.add(self._device_group(vd))
            remove = Adw.PreferencesGroup()
            btn = Adw.ButtonRow(title="Remove Virtual Display")
            btn.add_css_class("destructive-action")
            btn.connect("activated", lambda *_: self._remove_virtual(vd.name))
            remove.add(btn)
            page.add(remove)
        self.split.set_sidebar(page)

    def _display_group(self, o: Output, vd: VirtualDisplay | None) -> Adw.PreferencesGroup:
        g = Adw.PreferencesGroup(
            title="Display" if vd else GLib.markup_escape_text(o.name),
            description=None if vd or not o.description else GLib.markup_escape_text(o.description),
        )
        problem = self.canvas.problems().get(o.name)
        if problem:
            self._has_warning_row = True
            warn = Adw.ActionRow(title=problem)
            warn.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
            warn.add_css_class("error")
            g.add(warn)

        if not vd:
            enabled = Adw.SwitchRow(title="Enabled", active=o.enabled)
            enabled.connect("notify::active", self._on_enabled, o)
            g.add(enabled)

        if vd:
            for p in self._presets(vd):
                row = Adw.ActionRow(title=p.title, subtitle=p.subtitle, activatable=True)
                check = Gtk.CheckButton(active=(str(p.mode) == str(o.mode) and abs(p.scale - o.scale) < 1e-3))
                check.set_can_target(False)
                row.add_prefix(check)
                row.connect("activated", self._on_preset, o, p)
                g.add(row)
            custom = Adw.EntryRow(title="Custom mode (WIDTHxHEIGHT@HZ)", text=str(o.mode), show_apply_button=True)
            custom.connect("apply", self._on_custom_mode, o)
            g.add(custom)
        else:
            modes = list(o.modes) or [o.mode]
            if o.mode not in modes:
                modes.insert(0, o.mode)
            mode_row = Adw.ComboRow(title="Resolution", model=Gtk.StringList.new([m.label for m in modes]))
            mode_row.set_selected(modes.index(o.mode))
            mode_row.connect("notify::selected", self._on_mode, o, modes)
            g.add(mode_row)

        scales = valid_scales(o.mode.width, o.mode.height)
        if not any(abs(s - o.scale) < 1e-3 for s in scales):
            scales = sorted(scales + [o.scale])
        labels = []
        for s in scales:
            lw, lh = logical_size(o.mode.width, o.mode.height, s)
            labels.append(f"{fmt_scale(s)}×  ({lw} × {lh})")
        scale_row = Adw.ComboRow(title="Scale", model=Gtk.StringList.new(labels))
        scale_row.set_selected(min(range(len(scales)), key=lambda i: abs(scales[i] - o.scale)))
        scale_row.connect("notify::selected", self._on_scale, o, scales)
        g.add(scale_row)

        rot = Adw.ComboRow(title="Rotation", model=Gtk.StringList.new(TRANSFORMS))
        rot.set_selected(o.transform)
        rot.connect("notify::selected", self._on_transform, o)
        g.add(rot)

        x_row = Adw.SpinRow.new_with_range(-32768, 32768, 1)
        x_row.set_title("Position X")
        x_row.set_value(o.x)
        y_row = Adw.SpinRow.new_with_range(-32768, 32768, 1)
        y_row.set_title("Position Y")
        y_row.set_value(o.y)
        x_row.connect("notify::value", self._on_position, o, "x")
        y_row.connect("notify::value", self._on_position, o, "y")
        g.add(x_row)
        g.add(y_row)
        self._pos_rows = (x_row, y_row)
        return g

    def _presets(self, vd: VirtualDisplay):
        try:
            w, h = (int(v) for v in vd.native.split("x"))
        except ValueError:
            w, h = 1920, 1200
        unique, seen = [], set()
        for p in quality_presets(w, h):
            if (str(p.mode), p.scale) not in seen:   # small panels give duplicates
                seen.add((str(p.mode), p.scale))
                unique.append(p)
        return unique

    def _stream_group(self, o: Output, vd: VirtualDisplay) -> Adw.PreferencesGroup:
        g = Adw.PreferencesGroup(
            title=GLib.markup_escape_text(vd.name),
            description=GLib.markup_escape_text(vd.device_label) if vd.device_label else None,
        )
        row = Adw.ActionRow(title="Stopped", use_markup=False)
        row.add_prefix(Gtk.Image(icon_name="video-display-symbolic"))
        btn = Gtk.Button(valign=Gtk.Align.CENTER)
        btn.connect("clicked", self._on_toggle_stream, vd.name)
        row.add_suffix(btn)
        g.add(row)
        self._status_widgets = (row, btn, vd.name)
        self._update_status_widgets()
        return g

    def _device_group(self, vd: VirtualDisplay) -> Adw.PreferencesGroup:
        g = Adw.PreferencesGroup(title="Device &amp; Streaming")

        choices = [("", "First USB device found")]
        for d in self.devices:
            choices.append((d.serial, f"{self._device_label(d)} ({d.serial})"))
        if vd.serial and vd.serial not in {c[0] for c in choices}:
            choices.append((vd.serial, f"{vd.serial} (not connected)"))
        dev_row = Adw.ComboRow(title="Device", model=Gtk.StringList.new([c[1] for c in choices]))
        dev_row.set_selected(next((i for i, c in enumerate(choices) if c[0] == vd.serial), 0))
        dev_row.connect("notify::selected", self._on_device, vd, choices)
        g.add(dev_row)

        detect = Adw.ActionRow(
            use_markup=False, title="Panel", subtitle=f"{vd.native or 'unknown'} — quality presets are based on it",
        )
        detect_btn = Gtk.Button(label="Detect", valign=Gtk.Align.CENTER)
        detect_btn.connect("clicked", self._on_detect, vd)
        detect.add_suffix(detect_btn)
        g.add(detect)

        viewer = Adw.SwitchRow(
            title="Open viewer on the device",
            subtitle="Launches the VNC app (e.g. AVNC) automatically when plugged in",
            active=vd.open_viewer,
        )
        viewer.connect("notify::active", lambda r, _p: self._set_vd(vd, "open_viewer", r.get_active()))
        g.add(viewer)

        auto = Adw.SwitchRow(
            title="Start automatically",
            subtitle="Started by the background service at login (see Preferences)",
            active=vd.autostart,
        )
        auto.connect("notify::active", lambda r, _p: self._set_vd(vd, "autostart", r.get_active(), restart=False))
        g.add(auto)

        fps = Adw.SpinRow.new_with_range(10, 144, 5)
        fps.set_title("Frame rate limit")
        fps.set_value(vd.max_fps)
        fps.connect("notify::value", lambda r, _p: self._set_vd(vd, "max_fps", int(r.get_value())))
        g.add(fps)

        port = Adw.SpinRow.new_with_range(1024, 65535, 1)
        port.set_title("Port")
        port.set_value(vd.port)
        port.connect("notify::value", lambda r, _p: self._set_vd(vd, "port", int(r.get_value())))
        g.add(port)

        ws = Adw.EntryRow(title="Workspaces to move here (e.g. 6-10)", text=vd.workspaces, show_apply_button=True)
        ws.connect("apply", lambda r: self._set_vd(vd, "workspaces", r.get_text().strip()))
        g.add(ws)
        return g

    # -- panel callbacks ------------------------------------------------------
    def _on_enabled(self, row, _p, o: Output) -> None:
        if self._updating:
            return
        if not row.get_active() and sum(1 for x in self.canvas.outputs if x.enabled and x.present) <= 1:
            self._updating = True
            row.set_active(True)
            self._updating = False
            self.toast("At least one display must stay enabled")
            return
        o.enabled = row.get_active()
        self._changed(rebuild=True)

    def _on_mode(self, row, _p, o: Output, modes: list[Mode]) -> None:
        if self._updating:
            return
        o.mode = modes[row.get_selected()]
        o.scale = nearest_valid_scale(o.mode.width, o.mode.height, o.scale)
        self._changed(rebuild=True)

    def _on_custom_mode(self, row, o: Output) -> None:
        try:
            mode = Mode.parse(row.get_text())
        except ValueError:
            self.toast("Use the format 1920x1200@60")
            return
        if not (320 <= mode.width <= 8192 and 240 <= mode.height <= 8192):
            self.toast("Mode out of range")
            return
        o.mode = mode
        o.modes = [mode]
        o.scale = nearest_valid_scale(mode.width, mode.height, o.scale)
        self._changed(rebuild=True)

    def _on_preset(self, _row, o: Output, preset) -> None:
        o.mode, o.modes, o.scale = preset.mode, [preset.mode], preset.scale
        self._changed(rebuild=True)

    def _on_scale(self, row, _p, o: Output, scales: list[float]) -> None:
        if self._updating:
            return
        o.scale = scales[row.get_selected()]
        self._changed(rebuild=True)

    def _on_transform(self, row, _p, o: Output) -> None:
        if self._updating:
            return
        o.transform = row.get_selected()
        self._changed(rebuild=True)

    def _on_position(self, row, _p, o: Output, axis: str) -> None:
        if self._updating:
            return
        setattr(o, axis, int(row.get_value()))
        self._set_dirty(True)

    def _set_vd(self, vd: VirtualDisplay, key: str, value, restart: bool = True) -> None:
        if getattr(vd, key) == value:
            return
        setattr(vd, key, value)
        self.cfg.save()
        if restart and service.virtual_running(vd.name):
            self.toast(f"Restart {vd.name} to use the new setting", "Restart",
                       lambda: self._restart_virtual(vd.name), timeout=8)

    def _on_device(self, row, _p, vd: VirtualDisplay, choices) -> None:
        serial = choices[row.get_selected()][0]
        dev = next((d for d in self.devices if d.serial == serial), None)
        if dev:
            vd.device_label = self._device_label(dev)
        self._set_vd(vd, "serial", serial)

    def _on_detect(self, btn, vd: VirtualDisplay) -> None:
        btn.set_sensitive(False)

        def work():
            dev = android.find(vd.serial)
            info = None
            if dev and dev.state == "device":
                info = (android.panel_size(dev.serial), android.marketing_name(dev.serial))
            GLib.idle_add(done, info)

        def done(info):
            btn.set_sensitive(True)
            if not info or not info[0]:
                self.toast("No authorized device found")
                return False
            (w, h), label = info
            vd.native, vd.device_label = f"{w}x{h}", label
            self.cfg.save()
            o = self.canvas.get(vd.name)
            if o:
                o.description = label
            self._build_panel()
            return False

        threading.Thread(target=work, daemon=True).start()

    # ======================================================================
    # Virtual displays
    # ======================================================================
    def _on_toggle_stream(self, _btn, name: str) -> None:
        if service.virtual_running(name):
            service.stop_virtual(name)
            self.toast(f"{name} stopped")
        else:
            if self.dirty:
                self._save_virtual_geometry()
            ok, err = service.start_virtual(name)
            self.toast(f"{name} started" if ok else f"Could not start {name}: {err}")
        self.vd_state.setdefault(name, {})["running"] = service.virtual_running(name)
        self._update_status_widgets()

    def _restart_virtual(self, name: str) -> None:
        ok, err = service.restart_virtual(name)
        if not ok:
            self.toast(f"Could not restart {name}: {err}")

    def _remove_virtual(self, name: str) -> None:
        dialog = Adw.AlertDialog(heading=f"Remove {name}?", body="The virtual display and its settings are deleted. Profiles keep their copy.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_d, r):
            if r != "remove":
                return
            service.stop_virtual(name)
            self.cfg.virtual_displays.pop(name, None)
            self.cfg.save()
            GLib.timeout_add(500, self._reload_once)

        dialog.connect("response", on_response)
        dialog.present(self)

    def _update_status_widgets(self) -> None:
        if not self._status_widgets:
            return
        row, btn, name = self._status_widgets
        st = self.vd_state.get(name, {})
        running = st.get("running", False)
        status = st.get("status", {})
        if not running:
            row.set_title("Stopped")
            row.set_subtitle("Press Start, then plug in the device")
        else:
            state = status.get("state", "waiting")
            row.set_title(STATE_TEXT.get(state, state).split(" — ")[0])
            if state == "streaming":
                clients = st.get("clients")
                viewers = "" if clients is None else f" · {clients} viewer{'s' if clients != 1 else ''}"
                port = self.cfg.virtual_displays[name].port if name in self.cfg.virtual_displays else ""
                row.set_subtitle(f"{status.get('device', '')} · port {port}{viewers}")
            elif state == "error":
                row.set_subtitle(status.get("error", ""))
            else:
                row.set_subtitle(STATE_TEXT.get(state, "").partition(" — ")[2] or STATE_TEXT.get(state, ""))
        btn.set_label("Stop" if running else "Start")
        for cls in ("suggested-action", "destructive-action"):
            btn.remove_css_class(cls)
        btn.add_css_class("destructive-action" if running else "suggested-action")

    def _add_virtual_dialog(self) -> None:
        dialog = Adw.Dialog(title="Add Virtual Display", content_width=440)
        devices = [d for d in self.devices if d.usb]
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(
            description="Turns an Android tablet or phone into a wired display. The device needs "
                        "USB debugging enabled and a VNC viewer such as AVNC.",
        )
        choices = [(d.serial, f"{self._device_label(d)} ({d.serial})" + ("" if d.state == "device" else " — not authorized"))
                   for d in devices]
        choices.append(("", "First USB device found"))
        dev_row = Adw.ComboRow(title="Device", model=Gtk.StringList.new([c[1] for c in choices]))
        g.add(dev_row)

        taken = set(self.cfg.virtual_displays) | {o.name for o in self.canvas.outputs}
        name = "TABLET"
        n = 2
        while name in taken:
            name, n = f"TABLET-{n}", n + 1
        name_row = Adw.EntryRow(title="Output name", text=name)
        g.add(name_row)
        start_row = Adw.SwitchRow(title="Start streaming now", active=bool(devices))
        g.add(start_row)
        page.add(g)

        add = Gtk.Button(label="Add", halign=Gtk.Align.CENTER, margin_bottom=18)
        add.add_css_class("suggested-action")
        add.add_css_class("pill")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(page)
        box.append(add)
        view = Adw.ToolbarView(content=box)
        view.add_top_bar(Adw.HeaderBar())
        dialog.set_child(view)

        def on_add(_b):
            new_name = name_row.get_text().strip().replace(" ", "-")
            if not new_name or new_name in taken:
                self.toast("Choose a unique output name")
                return
            serial = choices[dev_row.get_selected()][0]
            add.set_sensitive(False)
            threading.Thread(target=create, args=(new_name, serial, start_row.get_active()), daemon=True).start()

        def create(new_name, serial, start):
            dev = android.find(serial)
            native, label = None, ""
            if dev and dev.state == "device":
                native = android.panel_size(dev.serial)
                label = android.marketing_name(dev.serial)
            GLib.idle_add(finish, new_name, serial, native, label, start)

        def finish(new_name, serial, native, label, start):
            w, h = native or (1920, 1200)
            preset = next(p for p in quality_presets(w, h) if p.key == "balanced")
            port = self.cfg.next_port()
            while not _port_free(port):
                port += 1
            vd = VirtualDisplay(
                name=new_name, serial=serial, device_label=label,
                native=f"{w}x{h}" if native else "", mode=str(preset.mode), scale=preset.scale,
                position=self._free_spot(preset.mode, preset.scale), port=port,
            )
            self.cfg.virtual_displays[new_name] = vd
            self.cfg.save()
            dialog.close()
            if start:
                ok, err = service.start_virtual(new_name)
                if not ok:
                    self.toast(f"Could not start {new_name}: {err}")
            self.canvas.selected = new_name
            GLib.timeout_add(800 if start else 10, self._reload_once)
            return False

        add.connect("clicked", on_add)
        dialog.present(self)

    def _free_spot(self, mode: Mode, scale: float) -> list[int]:
        """Below the existing layout, horizontally centred."""
        active = [o for o in self.canvas.outputs if o.enabled]
        if not active:
            return [0, 0]
        x0 = min(o.x for o in active)
        x1 = max(o.x + o.logical[0] for o in active)
        y1 = max(o.y + o.logical[1] for o in active)
        lw, _ = logical_size(mode.width, mode.height, scale)
        return [x0 + max(0, (x1 - x0 - lw) // 2), y1]

    # ======================================================================
    # Profiles
    # ======================================================================
    def _fill_profiles_popover(self, button: Gtk.MenuButton) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6,
                      margin_bottom=6, margin_start=6, margin_end=6, width_request=260)
        if self.cfg.profiles:
            lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            lb.add_css_class("boxed-list")
            for p in self.cfg.profiles.values():
                row = Adw.ActionRow(title=GLib.markup_escape_text(p.name), activatable=True,
                                    subtitle="Switches automatically" if p.auto else "")
                row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
                row.connect("activated", self._on_profile_activated, p.name, button)
                lb.append(row)
            box.append(lb)
        else:
            empty = Gtk.Label(label="No saved profiles yet", margin_top=6, margin_bottom=6)
            empty.add_css_class("dim-label")
            box.append(empty)
        save = Gtk.Button(label="Save Current Layout…")
        save.connect("clicked", lambda *_: (button.popdown(), self._save_profile_dialog()))
        box.append(save)
        if self.cfg.profiles:
            manage = Gtk.Button(label="Manage Profiles…")
            manage.add_css_class("flat")
            manage.connect("clicked", lambda *_: (button.popdown(), self._manage_profiles()))
            box.append(manage)
        button.get_popover().set_child(box)

    def _on_profile_activated(self, _row, name: str, button: Gtk.MenuButton) -> None:
        button.popdown()
        self._apply_profile(name)

    def _apply_profile(self, name: str) -> None:
        p = self.cfg.profiles.get(name)
        if not p:
            return
        try:
            warnings = profiles.apply(p, self.cfg)
        except hyprland.HyprlandError as e:
            self.toast(f"Could not apply {name}: {e}")
            return
        if hyprland.is_included():
            GLib.timeout_add(600, lambda: (hyprland.persist(hyprland.outputs()), False)[1])
        self.toast(f"Profile “{name}” applied" + (f" — {'; '.join(warnings)}" if warnings else ""))
        GLib.timeout_add(800, self._reload_once)

    def _save_profile_dialog(self) -> None:
        dialog = Adw.AlertDialog(heading="Save Profile", body="Saves the arrangement shown on the canvas, "
                                 "including which virtual displays are streaming.")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        entry = Gtk.Entry(placeholder_text="Profile name, e.g. Desk", activates_default=True)
        auto = Gtk.CheckButton(label="Switch to it automatically when these displays are plugged in")
        box.append(entry)
        box.append(auto)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_response_enabled("save", False)
        entry.connect("changed", lambda e: dialog.set_response_enabled("save", bool(e.get_text().strip())))

        def on_response(_d, r):
            name = entry.get_text().strip()
            if r != "save" or not name:
                return
            existed = name in self.cfg.profiles
            self.cfg.profiles[name] = profiles.capture(name, self.canvas.outputs, self.cfg, auto.get_active())
            self.cfg.save()
            self.toast(f"Profile “{name}” {'updated' if existed else 'saved'}")

        dialog.connect("response", on_response)
        dialog.present(self)

    def _manage_profiles(self) -> None:
        dialog = Adw.Dialog(title="Profiles", content_width=480, content_height=460)
        page = Adw.PreferencesPage()
        view = Adw.ToolbarView(content=page)
        view.add_top_bar(Adw.HeaderBar())
        dialog.set_child(view)

        def rebuild():
            for child in list(page_groups):
                page.remove(child)
            page_groups.clear()
            if not self.cfg.profiles:
                g = Adw.PreferencesGroup(title="No profiles")
                page.add(g)
                page_groups.append(g)
                return
            for p in list(self.cfg.profiles.values()):
                g = Adw.PreferencesGroup(title=GLib.markup_escape_text(p.name),
                                         description=GLib.markup_escape_text(", ".join(p.outputs)))
                auto = Adw.SwitchRow(title="Switch automatically", active=p.auto,
                                     subtitle="When exactly these physical displays are connected")
                auto.connect("notify::active", on_auto, p.name)
                g.add(auto)
                actions = Adw.ActionRow(title="Actions")
                for icon, tip, cb in (
                    ("media-playback-start-symbolic", "Apply", on_apply),
                    ("document-save-symbolic", "Overwrite with the current layout", on_update),
                    ("user-trash-symbolic", "Delete", on_delete),
                ):
                    b = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER)
                    b.add_css_class("flat")
                    b.connect("clicked", cb, p.name)
                    actions.add_suffix(b)
                g.add(actions)
                page.add(g)
                page_groups.append(g)

        def on_auto(row, _p, name):
            self.cfg.profiles[name].auto = row.get_active()
            self.cfg.save()

        def on_apply(_b, name):
            dialog.close()
            self._apply_profile(name)

        def on_update(_b, name):
            old = self.cfg.profiles[name]
            self.cfg.profiles[name] = profiles.capture(name, self.canvas.outputs, self.cfg, old.auto)
            self.cfg.save()
            self.toast(f"Profile “{name}” updated")
            rebuild()

        def on_delete(_b, name):
            self.cfg.profiles.pop(name, None)
            self.cfg.save()
            rebuild()

        page_groups: list = []
        rebuild()
        dialog.present(self)

    # ======================================================================
    # Preferences / about
    # ======================================================================
    def _preferences(self) -> None:
        dialog = Adw.PreferencesDialog(title="Preferences")
        page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
        g = Adw.PreferencesGroup(title="Background Service")
        bg = Adw.SwitchRow(
            title="Run in the background",
            subtitle="Starts virtual displays marked “Start automatically” at login and "
                     "switches profiles when displays are plugged in",
            active=service.daemon_enabled(),
        )

        def on_bg(row, _p):
            ok, err = service.set_daemon(row.get_active())
            if not ok:
                self.toast(f"systemctl failed: {err}")

        bg.connect("notify::active", on_bg)
        g.add(bg)
        page.add(g)

        g2 = Adw.PreferencesGroup(title="Safety")
        secs = Adw.SpinRow.new_with_range(5, 120, 5)
        secs.set_title("Confirmation timeout")
        secs.set_subtitle("Seconds before unconfirmed changes are reverted")
        secs.set_value(self.cfg.confirm_seconds)

        def on_secs(row, _p):
            self.cfg.confirm_seconds = int(row.get_value())
            self.cfg.save()

        secs.connect("notify::value", on_secs)
        g2.add(secs)
        page.add(g2)
        dialog.add(page)
        dialog.present(self)

    def _about(self) -> None:
        Adw.AboutDialog(
            application_name="Monimesh",
            application_icon="video-display",
            version=__version__,
            developer_name="Ahmet Enes",
            website="https://github.com/0anes0/monimesh",
            issue_url="https://github.com/0anes0/monimesh/issues",
            license_type=Gtk.License.MIT_X11,
            comments="Arrange displays on Wayland and turn Android devices into wired displays.",
        ).present(self)

    # ======================================================================
    # Background polling
    # ======================================================================
    def _device_label(self, d: android.Device) -> str:
        return self.device_names.get(d.serial) or d.label

    def _poll_loop(self) -> None:
        names: dict[str, str] = {}
        while not self._poll_stop.is_set():
            data = {"devices": android.devices() if android.available() else [], "vd": {}}
            for d in data["devices"]:
                if d.state == "device" and d.serial not in names:
                    names[d.serial] = android.marketing_name(d.serial)
            data["names"] = dict(names)
            try:
                data["present"] = {o.name for o in hyprland.outputs()}
            except hyprland.HyprlandError:
                data["present"] = None
            for name in list(self.cfg.virtual_displays):
                running = service.virtual_running(name)
                status = virtual.read_status(name) if running else {}
                clients = virtual.client_count(name) if status.get("state") == "streaming" else None
                data["vd"][name] = {"running": running, "status": status, "clients": clients}
            GLib.idle_add(self._on_poll, data)
            self._poll_stop.wait(POLL_SECONDS)

    def _on_poll(self, data: dict) -> bool:
        old_serials = {(d.serial, d.state) for d in self.devices}
        self.devices = data["devices"]
        self.device_names = data["names"]
        self.vd_state = data["vd"]
        self._update_status_widgets()
        present = data.get("present")
        if present is not None and present != self._present_names and not self.dirty:
            self.reload()
        elif {(d.serial, d.state) for d in self.devices} != old_serials and self._status_widgets:
            self._build_panel()
        return False

    def _on_close(self, *_):
        self._poll_stop.set()
        return False
