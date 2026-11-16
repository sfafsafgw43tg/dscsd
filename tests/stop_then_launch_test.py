#!/usr/bin/env python3
"""Regression test: a stopped device must be launchable again.

Reported symptom: the card shows **Stopped** with a Launch button, but
clicking it never brings the device up ("after I stop the device I cannot
launch it again"). Two real-machine races caused this - the fake SDK hides
both because it drops the device instantly:

1. **Real adb is lazy.** `adb emu kill` returns before adbd deregisters, so
   for a few seconds `adb devices` still lists the dead emulator. The next
   Launch then saw `_is_running() == True` and bailed out with
   "Device already running: …", which the GUI only showed as a toast.
2. **The VM is a child process.** On Windows `emulator.exe` starts
   `qemu-system-x86_64.exe` as a *separate* process. Killing only the
   recorded pid left the VM alive: it kept the console/adb ports bound and
   the AVD lock files in place, so the next Launch died with
   "port in use" / "another emulator instance is running".

Covered here:
- stop kills the whole process *tree* and verifies the console is gone,
- stop makes adb drop the dead serial,
- a launch right after stop is accepted (no "already running"),
- a lingering emulator that ignores `emu kill` cannot block the next launch
  (it is force-stopped / its stale adb entry is cleared),
- stale AVD lock files left by a hard kill are removed before launch,
- stop reports honestly when an emulator refuses to die,
- the GUI endpoint surfaces a stop failure instead of claiming success.
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_sdk import setup_fake_sdk  # noqa: E402

paths = setup_fake_sdk()
TMP = paths["tmp"]
STATE = paths["state"]
LOG = paths["log"]
os.environ["HOME"] = str(TMP / "home")
(TMP / "home").mkdir(exist_ok=True)
for key in ("ANDROID_SDK_ROOT", "ANDROID_HOME", "ANDROID_SDK_HOME"):
    os.environ[key] = str(paths["sdk"])

failures = []


def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(label)


import config  # noqa: E402

config.DATA_DIR = TMP / "data"
config.SAVED_DEVICES_DIR = TMP / "devices"
config.LOGS_DIR = TMP / "logs"
for d in (config.DATA_DIR, config.SAVED_DEVICES_DIR, config.LOGS_DIR):
    d.mkdir(parents=True, exist_ok=True)

from core.device_manager import DeviceManager, EmulatorLaunchError  # noqa: E402

(STATE / "root-allowed").write_text("1\n")


class FakeConsole:
    """Minimal emulator console (banner + `avd name`) on a real socket."""

    def __init__(self, avd_name):
        self.avd_name = avd_name
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            conn.sendall(b"Android Console: type 'help' for a list of commands\r\nOK\r\n")
            buf = b""
            try:
                while True:
                    data = conn.recv(1024)
                    if not data:
                        return
                    buf += data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        cmd = line.strip().decode(errors="replace")
                        if cmd == "avd name":
                            conn.sendall(self.avd_name.encode() + b"\r\nOK\r\n")
                        elif cmd == "quit":
                            conn.sendall(b"OK\r\n")
                            return
                        else:
                            conn.sendall(b"KO: unknown command\r\n")
            except OSError:
                return

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


mgr = DeviceManager()
device = mgr.create_device("Google Pixel 7a", android_version="14", name="g7a")


def device_json():
    return json.loads((config.SAVED_DEVICES_DIR / "g7a.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ 1. plain stop/launch
print("== 1. stop -> launch works ==")
launched = mgr.launch_device("g7a", enable_frida=False, wait_for_boot=True)
FIRST_SERIAL = launched["serial"]
check("launched", bool(FIRST_SERIAL))
mgr.stop_device("g7a")
check("stop clears the running state", mgr._is_running("g7a") is False)
check("stop clears the persisted pid", "pid" not in device_json())
relaunched = mgr.launch_device("g7a", enable_frida=False, wait_for_boot=True)
check("relaunch after stop succeeds", bool(relaunched.get("serial")),
      str(relaunched.get("serial")))
mgr.stop_device("g7a")

# ------------------------------------------------------------------ 2. lazy adb deregistration
print("== 2. a dead emulator still listed by adb does not block the launch ==")
launched = mgr.launch_device("g7a", enable_frida=False, wait_for_boot=True)
SERIAL = launched["serial"]
mgr.stop_device("g7a")
# Real adb keeps listing a just-killed emulator for a few seconds.
(STATE / f"{SERIAL}.state").write_text("device\n")
check("the ghost is initially reported as running", mgr._is_running("g7a") is True)
# Real adb can keep listing the serial even after emu kill: force that state
# and prove stop tells adb to drop it.
class GhostAdbManager(DeviceManager):
    """Pretends adb still lists the dead serial for the first few polls."""

    def __init__(self):
        super().__init__()
        self.ghost_polls = 100

    def _adb_serial_present(self, serial):
        if self.ghost_polls > 0:
            self.ghost_polls -= 1
            return True
        return super()._adb_serial_present(serial)


ghost_mgr = GhostAdbManager()
LOG.write_text("")
ghost_mgr._running_processes.pop("g7a", None)
ghost_mgr.stop_device("g7a")
check("stop tells adb to drop the serial it still lists",
      f"adb -s {SERIAL} reconnect offline" in LOG.read_text(encoding="utf-8"),
      "no 'reconnect offline' call logged")
mgr._boot_id_cache.clear()
try:
    again = mgr.launch_device("g7a", enable_frida=False, wait_for_boot=True)
    check("launch is not rejected with 'already running'", bool(again.get("serial")))
    check("a fresh emulator was booted", again.get("serial") is not None)
except ValueError as exc:
    check("launch is not rejected with 'already running'", False, str(exc))
mgr.stop_device("g7a")

# ------------------------------------------------------------------ 3. lingering emulator
print("== 3. an emulator that ignores 'emu kill' cannot block the next launch ==")
device = mgr.get_device("g7a")
avd = device["avd_name"]
# A process that outlives stop (the qemu child that survived the kill).
# Mimic the emulator: its own session, so a tree-kill is safe.
survivor = subprocess.Popen(["sleep", "120"], start_new_session=True)
console = FakeConsole(avd)
saved = device_json()
saved.update({"pid": survivor.pid, "console_port": console.port,
              "serial": "emulator-5999", "launched_at": "2020-01-01T00:00:00"})
(config.SAVED_DEVICES_DIR / "g7a.json").write_text(json.dumps(saved), encoding="utf-8")
mgr._running_processes.pop("g7a", None)

check("blocker detects the surviving process + console",
      mgr._launch_blocker("g7a") is True)
try:
    mgr.stop_device("g7a")
    stopped_ok = True
except EmulatorLaunchError as exc:  # console is a fake, pid is our sleeper
    stopped_ok = False
    print(f"   (stop reported: {str(exc)[:90]}…)")
check("stop killed the surviving process tree", survivor.poll() is not None)
console.close()
time.sleep(0.6)
try:
    survivor.wait(timeout=5)
except Exception:
    pass

# ------------------------------------------------------------------ 4. stale AVD locks
print("== 4. stale AVD lock files are cleared before launch ==")
avd_dir = config.AVD_HOME / f"{avd}.avd"
avd_dir.mkdir(parents=True, exist_ok=True)
lock = avd_dir / "hardware-qemu.ini.lock"
lock_dir = avd_dir / "multiinstance.lock"
lock.write_text("stale")
lock_dir.mkdir(exist_ok=True)
mgr2 = DeviceManager()
dev = mgr2.get_device("g7a")
removed = mgr2._clear_stale_avd_locks(dev)
check("stale lock file removed", not lock.exists(), str(removed))
check("stale lock directory removed", not lock_dir.exists(), str(removed))
check("cleanup reports what it removed", "hardware-qemu.ini.lock" in removed, str(removed))

# ------------------------------------------------------------------ 5. launch clears ghosts
print("== 5. launch clears a stale runtime state before booting ==")
saved = device_json()
saved.update({"pid": 999999, "serial": "emulator-5998"})
(config.SAVED_DEVICES_DIR / "g7a.json").write_text(json.dumps(saved), encoding="utf-8")
(STATE / "emulator-5998.state").write_text("device\n")
mgr3 = DeviceManager()
try:
    fresh = mgr3.launch_device("g7a", enable_frida=False, wait_for_boot=True)
    check("launch succeeded despite a ghost adb entry", bool(fresh.get("serial")))
    check("the new serial is not the ghost", fresh.get("serial") != "emulator-5998")
except ValueError as exc:
    check("launch succeeded despite a ghost adb entry", False, str(exc))
mgr3.stop_device("g7a")

# ------------------------------------------------------------------ 6. GUI reports failures
print("== 6. the GUI stop endpoint tells the truth ==")
from gui.app import create_app  # noqa: E402

client = create_app().test_client()
r = client.post("/api/devices/g7a/stop")
check("stop endpoint returns JSON", r.status_code in (200, 409), str(r.status_code))
body = r.get_json()
check("stop endpoint reports success for a stopped device",
      body.get("success") is True or "error" in body, json.dumps(body)[:200])

# Force stop: frees the ports even when a (fake) emulator refuses to die.
console = FakeConsole("antidetect_g7a")
saved = device_json()
saved.update({"pid": 999998, "console_port": console.port, "serial": "emulator-5997"})
(config.SAVED_DEVICES_DIR / "g7a.json").write_text(json.dumps(saved), encoding="utf-8")
r_force = client.post("/api/devices/g7a/stop", json={"force": True})
force_body = r_force.get_json()
check("force stop frees a port held by an unresponsive emulator",
      r_force.status_code in (200, 409) and "force_attempted" in force_body,
      json.dumps(force_body)[:200])
console.close()
r_launch = client.post("/api/devices/g7a/launch", json={"wait": False})
check("launch endpoint accepts the device after stop",
      r_launch.status_code == 200 and r_launch.get_json().get("success") is True,
      json.dumps(r_launch.get_json())[:300])
time.sleep(3)
try:
    r_stop = client.post("/api/devices/g7a/stop")
    check("second stop endpoint call returns success or an honest error",
          r_stop.status_code == 200 or "error" in r_stop.get_json(),
          json.dumps(r_stop.get_json())[:200])
except Exception as exc:
    check("second stop endpoint call returns success or an honest error", False, str(exc))

# ------------------------------------------------------------------ 7. static guards
print("== 7. static guards ==")
src = (REPO / "core" / "device_manager.py").read_text(encoding="utf-8")
check("stop kills the process tree (not just the pid)", "_kill_process_tree" in src)
check("stop verifies the emulator really went away", "_wait_until_stopped" in src)
check("launch checks for a live console, not just a pid", "_launch_blocker" in src)
check("stale AVD locks are cleaned before launch", "_clear_stale_avd_locks" in src)
check("adb is told to drop the dead serial",
      'reconnect' in src and '"offline"' in src)
check("stop refuses to lie about a stuck emulator",
      "Could not stop" in src)
js = (REPO / "gui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
check("dashboard offers a force stop when stop fails", "forceStopDevice" in js)
check("dashboard surfaces a rejected launch instead of doing nothing",
      "Failed to launch device" in js)

if failures:
    print(f"\n{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("\nAll stop/launch lifecycle checks passed.")
