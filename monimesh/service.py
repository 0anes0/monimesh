"""systemd --user integration.

* Each virtual display runs in a transient unit `monimesh-vd-<NAME>.service`,
  so streaming keeps going after the window is closed.
* The optional background service `monimesh.service` runs `monimesh daemon`
  (hotplug profiles + autostart of virtual displays).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

UNIT_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"
DAEMON_UNIT = "monimesh.service"
_PKG_PARENT = str(Path(__file__).resolve().parent.parent)


def _command(*args: str) -> list[str]:
    return [sys.executable, "-m", "monimesh", *args]


def _env_args() -> list[str]:
    env = ["--setenv=PYTHONPATH=" + _PKG_PARENT]
    for key in ("WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE", "XDG_RUNTIME_DIR"):
        if os.environ.get(key):
            env.append(f"--setenv={key}={os.environ[key]}")
    return env


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=10)


def vd_unit(name: str) -> str:
    return f"monimesh-vd-{name}.service"


def is_active(unit: str) -> bool:
    return _systemctl("is-active", "--quiet", unit).returncode == 0


def start_virtual(name: str) -> tuple[bool, str]:
    unit = vd_unit(name)
    if is_active(unit):
        return True, ""
    _systemctl("reset-failed", unit)
    res = subprocess.run(
        ["systemd-run", "--user", f"--unit={unit}", "--collect",
         f"--description=Monimesh virtual display {name}", *_env_args(),
         *_command("virtual-run", name)],
        capture_output=True, text=True, timeout=10,
    )
    return res.returncode == 0, res.stderr.strip()


def stop_virtual(name: str) -> None:
    _systemctl("stop", vd_unit(name))


def restart_virtual(name: str) -> tuple[bool, str]:
    stop_virtual(name)
    return start_virtual(name)


def virtual_running(name: str) -> bool:
    return is_active(vd_unit(name))


# --- background daemon ----------------------------------------------------------

def daemon_installed() -> bool:
    return (UNIT_DIR / DAEMON_UNIT).exists()


def daemon_enabled() -> bool:
    return _systemctl("is-enabled", "--quiet", DAEMON_UNIT).returncode == 0


def install_daemon() -> None:
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    cmd = " ".join(_command("daemon"))
    (UNIT_DIR / DAEMON_UNIT).write_text(
        "[Unit]\n"
        "Description=Monimesh display daemon (hotplug profiles, virtual displays)\n"
        "PartOf=graphical-session.target\n"
        "After=graphical-session.target\n\n"
        "[Service]\n"
        f"Environment=PYTHONPATH={_PKG_PARENT}\n"
        f"ExecStart={cmd}\n"
        "Restart=on-failure\n"
        "RestartSec=3\n\n"
        "[Install]\n"
        "WantedBy=graphical-session.target\n"
    )
    _systemctl("daemon-reload")


def set_daemon(enabled: bool) -> tuple[bool, str]:
    if enabled:
        install_daemon()
        res = _systemctl("enable", "--now", DAEMON_UNIT)
    else:
        res = _systemctl("disable", "--now", DAEMON_UNIT)
    return res.returncode == 0, res.stderr.strip()
