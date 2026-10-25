"""Persistent settings: virtual displays and named profiles (JSON)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "monimesh" / "config.json"


@dataclass
class VirtualDisplay:
    name: str                       # Wayland output name, e.g. "TABLET"
    serial: str = ""                # adb serial; "" = first USB device found
    device_label: str = ""          # e.g. "Galaxy Tab S10+"
    native: str = ""                # device panel, e.g. "2800x1752"
    mode: str = "1920x1200@60"
    scale: float = 1.2
    transform: int = 0
    position: list[int] = field(default_factory=lambda: [0, 0])
    port: int = 5900
    max_fps: int = 60
    open_viewer: bool = True        # launch the VNC viewer on the device
    workspaces: str = ""            # e.g. "6-10": moved here on connect
    autostart: bool = False         # started by the background service

    @classmethod
    def from_dict(cls, d: dict) -> "VirtualDisplay":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def workspace_list(self) -> list[int]:
        out: list[int] = []
        for part in self.workspaces.replace(" ", "").split(","):
            if not part:
                continue
            a, _, b = part.partition("-")
            try:
                out.extend(range(int(a), int(b or a) + 1))
            except ValueError:
                continue
        return out


@dataclass
class Profile:
    name: str
    # output name -> {"enabled", "mode", "position", "scale", "transform"}
    outputs: dict[str, dict] = field(default_factory=dict)
    # virtual displays that should be streaming when this profile is applied
    virtual_active: list[str] = field(default_factory=list)
    # apply automatically when exactly these physical outputs are connected
    auto: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def physical_set(self, virtual_names: set[str]) -> frozenset[str]:
        return frozenset(n for n in self.outputs if n not in virtual_names)


@dataclass
class Config:
    virtual_displays: dict[str, VirtualDisplay] = field(default_factory=dict)
    profiles: dict[str, Profile] = field(default_factory=dict)
    confirm_seconds: int = 15

    @classmethod
    def load(cls) -> "Config":
        try:
            raw = json.loads(CONFIG_PATH.read_text())
        except (OSError, ValueError):
            return cls()
        return cls(
            virtual_displays={
                k: VirtualDisplay.from_dict(v) for k, v in raw.get("virtual_displays", {}).items()
            },
            profiles={k: Profile.from_dict(v) for k, v in raw.get("profiles", {}).items()},
            confirm_seconds=raw.get("confirm_seconds", 15),
        )

    def save(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "virtual_displays": {k: asdict(v) for k, v in self.virtual_displays.items()},
            "profiles": {k: asdict(v) for k, v in self.profiles.items()},
            "confirm_seconds": self.confirm_seconds,
        }
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(CONFIG_PATH)

    def next_port(self) -> int:
        used = {v.port for v in self.virtual_displays.values()}
        port = 5900
        while port in used:
            port += 1
        return port
