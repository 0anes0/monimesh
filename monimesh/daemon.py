"""Background daemon: autostart virtual displays and switch profiles on hotplug."""

from __future__ import annotations

import time

from . import hyprland, profiles, service
from .config import Config

HOTPLUG_EVENTS = {"monitoradded", "monitoraddedv2", "monitorremoved", "monitorremovedv2"}
SETTLE = 1.0


def _physical(cfg: Config) -> set[str]:
    virtual = set(cfg.virtual_displays)
    return {o.name for o in hyprland.outputs() if o.name not in virtual and not o.name.startswith("HEADLESS")}


def _maybe_apply(last: frozenset[str] | None) -> frozenset[str]:
    cfg = Config.load()
    current = frozenset(_physical(cfg))
    if current != last:
        p = profiles.match(cfg, set(current))
        if p:
            print(f"monimesh: outputs {sorted(current)} -> profile {p.name!r}", flush=True)
            for w in profiles.apply(p, cfg):
                print(f"monimesh: {w}", flush=True)
    return current


def run() -> int:
    cfg = Config.load()
    for name, vd in cfg.virtual_displays.items():
        if vd.autostart:
            ok, err = service.start_virtual(name)
            print(f"monimesh: autostart {name}: {'ok' if ok else err}", flush=True)

    last = _maybe_apply(None)
    while True:
        try:
            sock = hyprland.event_socket()
        except OSError as e:
            print(f"monimesh: cannot reach Hyprland ({e}), retrying", flush=True)
            time.sleep(3)
            continue
        with sock:
            for name, _data in hyprland.iter_events(sock):
                if name in HOTPLUG_EVENTS:
                    time.sleep(SETTLE)  # let the output finish configuring
                    try:
                        last = _maybe_apply(last)
                    except hyprland.HyprlandError as e:
                        print(f"monimesh: {e}", flush=True)
        time.sleep(1)
