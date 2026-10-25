"""Capturing and applying named profiles (shared by the GUI, CLI and daemon)."""

from __future__ import annotations

from . import hyprland, service
from .config import Config, Profile


def capture(name: str, outs: list[hyprland.Output], cfg: Config, auto: bool = False) -> Profile:
    """Profile from the given layout (physical outputs + virtual displays)."""
    active = [n for n in cfg.virtual_displays if service.virtual_running(n)]
    return Profile(
        name=name,
        outputs={o.name: o.to_dict() for o in outs},
        virtual_active=active,
        auto=auto,
    )


def apply(profile: Profile, cfg: Config) -> list[str]:
    """Apply a profile. Returns warnings (e.g. outputs that are not connected)."""
    warnings = []
    present = {o.name: o for o in hyprland.outputs()}

    # Virtual displays: store their geometry, then start/stop as recorded.
    for name, vd in cfg.virtual_displays.items():
        if name in profile.outputs:
            d = profile.outputs[name]
            vd.mode = d.get("mode", vd.mode)
            vd.scale = d.get("scale", vd.scale)
            vd.transform = d.get("transform", vd.transform)
            vd.position = list(d.get("position", vd.position))
    cfg.save()

    to_apply = []
    for name, d in profile.outputs.items():
        if name in present:
            o = present[name]
            o.update_from(d)
            to_apply.append(o)
        elif name not in cfg.virtual_displays:
            warnings.append(f"{name} is not connected")
    if to_apply:
        hyprland.apply(to_apply)

    for name in cfg.virtual_displays:
        should_run = name in profile.virtual_active
        if should_run and not service.virtual_running(name):
            ok, err = service.start_virtual(name)
            if not ok:
                warnings.append(f"{name}: {err}")
        elif not should_run and service.virtual_running(name):
            service.stop_virtual(name)
    return warnings


def match(cfg: Config, physical: set[str]) -> Profile | None:
    """Auto profile whose physical outputs equal the connected ones."""
    virtual = set(cfg.virtual_displays)
    for p in cfg.profiles.values():
        if p.auto and p.physical_set(virtual) == frozenset(physical):
            return p
    return None
