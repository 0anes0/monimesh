"""Command line entry point: `monimesh [command]` (no command opens the GUI)."""

from __future__ import annotations

import argparse
import sys

from . import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="monimesh", description="Display manager for Wayland.")
    parser.add_argument("--version", action="version", version=f"monimesh {__version__}")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("gui", help="open the window (default)")
    sub.add_parser("list", help="list outputs, virtual displays and profiles")
    p = sub.add_parser("apply", help="apply a saved profile")
    p.add_argument("profile")
    for name in ("start", "stop"):
        p = sub.add_parser(name, help=f"{name} a virtual display")
        p.add_argument("name")
    sub.add_parser("daemon", help="run the background daemon")
    p = sub.add_parser("virtual-run", help=argparse.SUPPRESS)
    p.add_argument("name")
    args = parser.parse_args(argv)

    if args.cmd in (None, "gui"):
        from .ui.app import run
        return run()

    from . import hyprland, profiles, service
    from .config import Config

    if args.cmd == "virtual-run":
        from .virtual import run
        return run(args.name)
    if args.cmd == "daemon":
        from .daemon import run
        return run()

    cfg = Config.load()
    if args.cmd == "list":
        print("Outputs:")
        for o in hyprland.outputs():
            state = "on " if o.enabled else "off"
            print(f"  [{state}] {o.name:10} {o.mode}  scale {o.scale:g}  at {o.x},{o.y}  {o.description}")
        print("Virtual displays:")
        for n, vd in cfg.virtual_displays.items():
            run_state = "running" if service.virtual_running(n) else "stopped"
            print(f"  {n:10} {vd.mode} scale {vd.scale:g}  port {vd.port}  {vd.device_label}  ({run_state})")
        print("Profiles:")
        for n, pr in cfg.profiles.items():
            print(f"  {n}{'  (auto)' if pr.auto else ''}: {', '.join(pr.outputs)}")
        return 0
    if args.cmd == "apply":
        pr = cfg.profiles.get(args.profile)
        if not pr:
            print(f"monimesh: no profile named {args.profile!r}", file=sys.stderr)
            return 1
        for w in profiles.apply(pr, cfg):
            print(f"monimesh: {w}", file=sys.stderr)
        return 0
    if args.cmd in ("start", "stop"):
        if args.name not in cfg.virtual_displays:
            print(f"monimesh: no virtual display named {args.name!r}", file=sys.stderr)
            return 1
        if args.cmd == "start":
            ok, err = service.start_virtual(args.name)
            if not ok:
                print(f"monimesh: {err}", file=sys.stderr)
                return 1
        else:
            service.stop_virtual(args.name)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
