"""Runner for one virtual display: headless output + wayvnc + adb reverse.

Runs in the foreground (normally inside a systemd user unit started by
`service.start_virtual`). Waits for the USB device, creates the output when it
appears, tears everything down when it is unplugged, and repeats.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from . import android, hyprland
from .config import Config, VirtualDisplay
from .geometry import Mode

RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "monimesh"
POLL = 1.5


def status_path(name: str) -> Path:
    return RUNTIME_DIR / f"{name}.json"


def log_path(name: str) -> Path:
    return RUNTIME_DIR / f"{name}.log"


def ctl_socket(name: str) -> Path:
    return RUNTIME_DIR / f"wayvncctl-{name}"


def read_status(name: str) -> dict:
    try:
        return json.loads(status_path(name).read_text())
    except (OSError, ValueError):
        return {}


def client_count(name: str) -> int | None:
    try:
        res = subprocess.run(
            ["wayvncctl", "-S", str(ctl_socket(name)), "--json", "client-list"],
            capture_output=True, text=True, timeout=2,
        )
        return len(json.loads(res.stdout)) if res.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def notify(title: str, body: str = "") -> None:
    try:
        subprocess.Popen(
            ["notify-send", "-a", "Monimesh", "-i", "video-display", title, body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


class Runner:
    def __init__(self, vd: VirtualDisplay):
        self.vd = vd
        self.wayvnc: subprocess.Popen | None = None
        self.serial = ""
        self.stopping = False

    def set_status(self, state: str, **extra) -> None:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        data = {"state": state, "pid": os.getpid(), "since": time.time(), **extra}
        status_path(self.vd.name).write_text(json.dumps(data))

    # -- lifecycle -------------------------------------------------------------
    def connect(self, dev: android.Device) -> None:
        vd = self.vd
        self.serial = dev.serial
        hyprland.remove_output(vd.name)
        prev_focus = hyprland.focused_monitor()
        hyprland.create_headless(vd.name)
        out = hyprland.Output(
            name=vd.name, mode=Mode.parse(vd.mode), x=vd.position[0], y=vd.position[1],
            scale=vd.scale, transform=vd.transform,
        )
        hyprland.apply([out])

        wanted = vd.workspace_list()
        for ws in hyprland.workspace_ids():
            if ws in wanted:
                hyprland.move_workspace(ws, vd.name)
        if prev_focus:
            hyprland.focus_monitor(prev_focus)

        sock = ctl_socket(vd.name)
        sock.unlink(missing_ok=True)
        with open(log_path(vd.name), "w") as log:
            self.wayvnc = subprocess.Popen(
                ["wayvnc", f"--max-fps={vd.max_fps}", "-S", str(sock), "-o", vd.name,
                 "127.0.0.1", str(vd.port)],
                stdout=log, stderr=subprocess.STDOUT,
            )
        time.sleep(0.5)
        android.reverse(dev.serial, vd.port)
        if vd.open_viewer:
            android.open_viewer(dev.serial, vd.port)
        label = vd.device_label or dev.label
        self.set_status("streaming", serial=dev.serial, device=label)
        notify(f"{vd.name} connected", f"{label} · port {vd.port}")

    def teardown(self) -> str:
        err = ""
        if self.wayvnc:
            if self.wayvnc.poll() is None:
                self.wayvnc.terminate()
                try:
                    self.wayvnc.wait(3)
                except subprocess.TimeoutExpired:
                    self.wayvnc.kill()
            else:
                try:
                    lines = log_path(self.vd.name).read_text().strip().splitlines()
                except OSError:
                    lines = []
                err = lines[-1] if lines else ""
            self.wayvnc = None
        if self.serial:
            android.reverse_remove(self.serial, self.vd.port)
        hyprland.remove_output(self.vd.name)
        ctl_socket(self.vd.name).unlink(missing_ok=True)
        return err

    def run(self) -> int:
        signal.signal(signal.SIGTERM, lambda *_: self._stop())
        signal.signal(signal.SIGINT, lambda *_: self._stop())
        self.set_status("waiting")
        hyprland.remove_output(self.vd.name)
        try:
            while not self.stopping:
                dev = android.find(self.vd.serial)
                if not dev or dev.state != "device":
                    self.set_status("unauthorized" if dev else "waiting")
                    time.sleep(POLL)
                    continue

                self.connect(dev)
                while not self.stopping:
                    time.sleep(POLL)
                    if self.wayvnc and self.wayvnc.poll() is not None:
                        break
                    d = android.find(dev.serial)
                    if not d or d.state != "device":
                        break

                err = self.teardown()
                if self.stopping:
                    break
                if android.find(dev.serial):
                    # wayvnc died while the device is still plugged in
                    self.set_status("error", error=err or "wayvnc exited")
                    notify(f"{self.vd.name}: streaming failed", err or "wayvnc exited")
                    time.sleep(10)
                else:
                    notify(f"{self.vd.name} disconnected")
                    self.set_status("waiting")
        finally:
            self.teardown()
            status_path(self.vd.name).unlink(missing_ok=True)
        return 0

    def _stop(self) -> None:
        self.stopping = True


def run(name: str) -> int:
    vd = Config.load().virtual_displays.get(name)
    if not vd:
        print(f"monimesh: unknown virtual display {name!r}")
        return 1
    return Runner(vd).run()
