"""Hyprland backend: query outputs, apply monitor rules, headless outputs, events.

Hyprland 0.56+ can be configured in Lua (hyprland.lua); older/classic setups
use hyprland.conf. Both are supported: runtime rules go through
`hyprctl eval` (Lua) or `hyprctl keyword monitor` (classic).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .geometry import Mode, fmt_scale, logical_size

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "hypr"


class HyprlandError(RuntimeError):
    pass


@dataclass
class Output:
    name: str
    description: str = ""
    mode: Mode = field(default_factory=lambda: Mode(1920, 1080, 60))
    x: int = 0
    y: int = 0
    scale: float = 1.0
    transform: int = 0
    enabled: bool = True
    modes: list[Mode] = field(default_factory=list)
    virtual: bool = False      # a Monimesh virtual display (headless output)
    present: bool = True       # currently exists in the compositor

    @property
    def logical(self) -> tuple[int, int]:
        return logical_size(self.mode.width, self.mode.height, self.scale, self.transform)

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "mode": str(self.mode),
            "position": [self.x, self.y],
            "scale": self.scale,
            "transform": self.transform,
        }

    def update_from(self, d: dict) -> None:
        self.enabled = d.get("enabled", self.enabled)
        if "mode" in d:
            self.mode = Mode.parse(d["mode"])
        if "position" in d:
            self.x, self.y = d["position"]
        self.scale = d.get("scale", self.scale)
        self.transform = d.get("transform", self.transform)


def available() -> bool:
    return bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")) and shutil.which("hyprctl") is not None


def uses_lua() -> bool:
    return (CONFIG_DIR / "hyprland.lua").exists()


def hyprctl(*args: str, check: bool = True) -> str:
    try:
        res = subprocess.run(["hyprctl", *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HyprlandError(str(e)) from e
    out = (res.stdout + res.stderr).strip()
    if check and (res.returncode != 0 or out.lower().startswith(("error", "err:"))):
        raise HyprlandError(out or f"hyprctl {' '.join(args)} failed")
    return out


def outputs() -> list[Output]:
    data = json.loads(hyprctl("monitors", "all", "-j"))
    result = []
    for m in data:
        modes = []
        for s in m.get("availableModes", []):
            try:
                mode = Mode.parse(s)
            except ValueError:
                continue
            if mode not in modes:
                modes.append(mode)
        result.append(
            Output(
                name=m["name"],
                description=m.get("description", "") or "",
                mode=Mode(m["width"], m["height"], round(m["refreshRate"], 2)),
                x=m["x"],
                y=m["y"],
                scale=m["scale"],
                transform=m.get("transform", 0),
                enabled=not m.get("disabled", False),
                modes=modes,
            )
        )
    return result


# --- monitor rules -----------------------------------------------------------

def _lua_rule(o: Output) -> str:
    if not o.enabled:
        return f'hl.monitor({{ output = "{o.name}", disabled = true }})'
    return (
        f'hl.monitor({{ output = "{o.name}", mode = "{o.mode}", '
        f'position = "{o.x}x{o.y}", scale = {fmt_scale(o.scale)}, transform = {o.transform} }})'
    )


def _classic_rule(o: Output) -> str:
    if not o.enabled:
        return f"{o.name},disable"
    return f"{o.name},{o.mode},{o.x}x{o.y},{fmt_scale(o.scale)},transform,{o.transform}"


def apply(outs: list[Output]) -> None:
    """Apply monitor rules at runtime (lost on `hyprctl reload` unless persisted)."""
    # Enable/position everything first and disable last so we never end up
    # with zero active outputs in between.
    ordered = sorted(outs, key=lambda o: not o.enabled)
    if not ordered:
        return
    if uses_lua():
        hyprctl("eval", "\n".join(_lua_rule(o) for o in ordered))
    else:
        hyprctl("--batch", " ; ".join(f"keyword monitor {_classic_rule(o)}" for o in ordered))


def persist_path() -> Path:
    return CONFIG_DIR / ("monimesh.lua" if uses_lua() else "monimesh.conf")


def include_line() -> str:
    return 'require("monimesh")' if uses_lua() else "source = ~/.config/hypr/monimesh.conf"


def main_config() -> Path:
    return CONFIG_DIR / ("hyprland.lua" if uses_lua() else "hyprland.conf")


def is_included() -> bool:
    try:
        return include_line() in main_config().read_text()
    except OSError:
        return False


def persist(outs: list[Output]) -> Path:
    """Write the layout to a file the main Hyprland config includes."""
    path = persist_path()
    header = "Generated by Monimesh - changes here are overwritten from the app."
    if uses_lua():
        lines = [f"-- {header}"] + [_lua_rule(o) for o in outs]
    else:
        lines = [f"# {header}"] + [f"monitor = {_classic_rule(o)}" for o in outs]
    path.write_text("\n".join(lines) + "\n")
    return path


def add_include() -> None:
    cfg = main_config()
    text = cfg.read_text() if cfg.exists() else ""
    if include_line() not in text:
        sep = "" if text.endswith("\n") or not text else "\n"
        comment = "-- Monimesh display layout" if uses_lua() else "# Monimesh display layout"
        cfg.write_text(f"{text}{sep}\n{comment}\n{include_line()}\n")


# --- headless outputs & workspaces --------------------------------------------

def create_headless(name: str) -> None:
    hyprctl("output", "create", "headless", name)


def remove_output(name: str) -> None:
    hyprctl("output", "remove", name, check=False)


def workspace_ids() -> list[int]:
    return [w["id"] for w in json.loads(hyprctl("workspaces", "-j"))]


def move_workspace(ws: int, monitor: str) -> None:
    if uses_lua():
        hyprctl("dispatch", f'hl.dsp.workspace.move({{ workspace = "{ws}", monitor = "{monitor}" }})', check=False)
    else:
        hyprctl("dispatch", "moveworkspacetomonitor", str(ws), monitor, check=False)


def focus_monitor(name: str) -> None:
    if uses_lua():
        hyprctl("dispatch", f'hl.dsp.focus({{ monitor = "{name}" }})', check=False)
    else:
        hyprctl("dispatch", "focusmonitor", name, check=False)


def focused_monitor() -> str | None:
    for m in json.loads(hyprctl("monitors", "-j")):
        if m.get("focused"):
            return m["name"]
    return None


# --- events -------------------------------------------------------------------

def event_socket() -> socket.socket:
    sig = os.environ["HYPRLAND_INSTANCE_SIGNATURE"]
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    path = Path(runtime) / "hypr" / sig / ".socket2.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(str(path))
    return s


def iter_events(sock: socket.socket):
    buf = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            return
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            name, _, data = line.decode(errors="replace").partition(">>")
            yield name, data
