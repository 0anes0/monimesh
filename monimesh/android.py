"""adb helpers for USB-connected Android devices."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Device:
    serial: str
    state: str          # "device", "unauthorized", "offline", ...
    model: str = ""
    usb: bool = True

    @property
    def label(self) -> str:
        return self.model.replace("_", " ") or self.serial


def available() -> bool:
    return shutil.which("adb") is not None


def adb(*args: str, serial: str = "", timeout: float = 10) -> subprocess.CompletedProcess:
    cmd = ["adb"] + (["-s", serial] if serial else []) + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def devices() -> list[Device]:
    try:
        out = adb("devices", "-l", timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    result = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        info = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        result.append(Device(parts[0], parts[1], info.get("model", ""), "usb" in info))
    return result


def find(serial: str = "") -> Device | None:
    """The requested device, or the first USB one when serial is empty."""
    for d in devices():
        if d.usb and (d.serial == serial or not serial):
            return d
    return None


def panel_size(serial: str) -> tuple[int, int] | None:
    """Physical panel size in landscape orientation."""
    try:
        out = adb("shell", "wm", "size", serial=serial).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in reversed(out.splitlines()):
        if "x" in line and ":" in line:
            try:
                w, h = (int(v) for v in line.split(":")[1].strip().split("x"))
            except ValueError:
                continue
            return (max(w, h), min(w, h))
    return None


def marketing_name(serial: str) -> str:
    """Human-friendly name: the device name set in Android settings, else the model."""
    queries = [("settings", "get", "global", "device_name")] + [
        ("getprop", p) for p in ("ro.product.marketname", "ro.config.marketing_name", "ro.product.model")
    ]
    for q in queries:
        try:
            v = adb("shell", *q, serial=serial, timeout=5).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            continue
        if v and v != "null":
            return v
    return serial


def reverse(serial: str, port: int) -> bool:
    return adb("reverse", f"tcp:{port}", f"tcp:{port}", serial=serial).returncode == 0


def reverse_remove(serial: str, port: int) -> None:
    try:
        adb("reverse", "--remove", f"tcp:{port}", serial=serial, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def open_viewer(serial: str, port: int) -> bool:
    """Open vnc://127.0.0.1:port on the device (AVNC and others handle it)."""
    try:
        res = adb(
            "shell", "am", "start", "-a", "android.intent.action.VIEW",
            "-d", f"vnc://127.0.0.1:{port}", serial=serial,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return res.returncode == 0 and "Error" not in res.stdout + res.stderr
