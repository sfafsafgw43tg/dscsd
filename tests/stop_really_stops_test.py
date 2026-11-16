#!/usr/bin/env python3
"""Regression test: clicking Stop must really stop the device.

Reported: "after clicking stop make the device stop properly". A stop used to
be declared successful as soon as the *recorded pid* disappeared, which left
real machines in these states while the card said **Stopped**:

1. the VM outlived `adb emu kill` (on Windows it is a separate qemu child),
   ignoring SIGTERM and holding the console/adb ports;
2. the pid was stale/recycled (or the app had restarted), so nothing tracked
   the emulator that was still holding the ports;
3. a helper child held the ports while its emulator parent kept running;
4. a *foreign* process owned the ports - that one must NOT be killed, and the
   stop must say so instead of pretending.

The port probe matters too: `connect()` fails against a process with a full
accept backlog, and `bind()` fails on a harmless TIME_WAIT entry. Either one
alone makes a running emulator look stopped (or a stopped one look running).

Run: ``python tests/stop_really_stops_test.py`` (exit 1 = fail).
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_sdk import setup_fake_sdk  # noqa: E402

paths = setup_fake_sdk()
TMP = paths["tmp"]
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
(paths["state"] / "root-allowed").write_text("1\n")

import gui.app as ga  # noqa: E402
from core.device_manager import DeviceManager  # noqa: E402

BASH = shutil.which("bash")
FAKE_EMU = TMP / "emulator"
if BASH:
    shutil.copy(BASH, FAKE_EMU)

HOLD_PY = TMP / "hold.py"
HOLD_PY.write_text(
    "import socket,time,sys\n"
    "s=socket.socket()\n"
    "s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
    "s.bind(('127.0.0.1',int(sys.argv[1])))\n"
    "s.listen(0)\n"
    "time.sleep(180)\n"
)

app = ga.create_app()
client = app.test_client()
mgr = DeviceManager()
started = []


def make_device(name):
    r = client.post("/api/devices", json={
        "model": "Samsung Galaxy S24", "android_version": "14", "name": name})
    assert r.get_json().get("success") is True, r.get_json()
    return name


def save_fields(name, **fields):
    path = config.SAVED_DEVICES_DIR / f"{name}.json"
    dev = json.loads(path.read_text(encoding="utf-8"))
    dev.update(fields)
    path.write_text(json.dumps(dev), encoding="utf-8")
    return dev


def spawn_stubborn(port=None, ignores_term=True):
    """An `emulator`-named process in its own session, optionally holding a port."""
    script = TMP / f"stubborn_{time.time_ns()}.sh"
    body = "#!/bin/bash\n"
    if ignores_term:
        body += "trap '' TERM\n"
    if port:
        body += f'"{sys.executable}" "{HOLD_PY}" {port} &\n'
    body += "sleep 300\n"
    script.write_text(body)
    proc = subprocess.Popen([str(FAKE_EMU), str(script)], start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    started.append(proc)
    return proc


def port_busy(port):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.4):
            return True
    except OSError:
        return False


def stop_and_check(name, label, expect_success=True):
    t0 = time.time()
    r = client.post(f"/api/devices/{name}/stop", json={"force": True})
    body = r.get_json()
    elapsed = time.time() - t0
    stopped = not client.get(f"/api/devices/{name}/status").get_json().get("running")
    err = bool(json.loads((config.SAVED_DEVICES_DIR / f"{name}.json")
                          .read_text(encoding="utf-8")).get("stop_error"))
    if expect_success:
        check(f"{label}: stop reports success", body.get("success") is True,
              json.dumps(body)[:300])
        check(f"{label}: device is not running afterwards", stopped)
        check(f"{label}: no stale stop error left on the card", not err)
    else:
        check(f"{label}: stop refuses instead of pretending", body.get("success") is False,
              json.dumps(body)[:300])
        check(f"{label}: the refusal is recorded for the card", err)
    check(f"{label}: stop is not slow ({elapsed:.1f}s)", elapsed < 25, f"{elapsed:.1f}s")
    return body


# ------------------------------------------------------------------ 1. port probing
print("== 1. port probing tells the truth ==")
listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", 0))
listener.listen(0)
free_port = listener.getsockname()[1]
check("a bound port is reported in use", mgr._port_in_use(free_port) is True)
# Saturate the backlog: connect() then fails while the port is definitely ours.
held = []
for _ in range(4):
    try:
        held.append(socket.create_connection(("127.0.0.1", free_port), timeout=0.5))
    except OSError:
        pass
check("a full accept backlog still counts as in use",
      mgr._port_in_use(free_port) is True)
for conn in held:
    conn.close()
listener.close()

# A port nothing listens on must be free. Probe it a few times: bind() alone
# mis-reports a harmless TIME_WAIT entry as "in use".
free2 = socket.socket()
free2.bind(("127.0.0.1", 0))
unused_port = free2.getsockname()[1]
free2.close()
check("a port nothing listens on is reported free",
      mgr._port_in_use(unused_port) is False)

# ------------------------------------------------------------------ 2. a stubborn emulator
print("== 2. a stuck emulator (ignores emu kill and SIGTERM) is really stopped ==")
make_device("stubborn")
proc = spawn_stubborn()
time.sleep(1.0)
save_fields("stubborn", pid=proc.pid, serial="emulator-5701",
            console_port=5701, adb_port=5702)
check("the stuck emulator counts as running",
      client.get("/api/devices/stubborn/status").get_json().get("running") is True)
stop_and_check("stubborn", "stubborn")
check("the stuck emulator process is gone", proc.poll() is not None)

# ------------------------------------------------------------------ 3. stale pid + port owner
print("== 3. an emulator we no longer track is found through its port ==")
make_device("untracked")
proc2 = spawn_stubborn(port=5711)
time.sleep(1.5)
save_fields("untracked", pid=999999, serial="emulator-5711",
            console_port=5711, adb_port=5712)
stop_and_check("untracked", "untracked")
check("the untracked emulator process is gone", proc2.poll() is not None)
check("its console port was freed", port_busy(5711) is False)

# ------------------------------------------------------------------ 4. helper child
print("== 4. a helper child holding the port takes the emulator down with it ==")
make_device("wrapped")
proc3 = spawn_stubborn(port=5721)
time.sleep(1.5)
save_fields("wrapped", pid=proc3.pid, serial="emulator-5721",
            console_port=5721, adb_port=5722)
stop_and_check("wrapped", "wrapped")
check("the emulator parent is gone, not just its helper", proc3.poll() is not None)

# ------------------------------------------------------------------ 5. a recycled pid is SAFE
print("== 5. a recycled pid is never killed ==")
victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                          start_new_session=True)
try:
    make_device("recycled")
    save_fields("recycled", pid=victim.pid, serial="emulator-5799",
                console_port=5799, adb_port=5800)
    check("a non-emulator pid is not treated as the device",
          mgr._pid_looks_like_emulator(victim.pid) is False)
    stop_and_check("recycled", "recycled")
    check("the unrelated process is still alive", victim.poll() is None)
finally:
    if victim.poll() is None:
        victim.kill()

# ------------------------------------------------------------------ 6. a foreign port holder
print("== 6. a foreign process on the device's port is reported, not killed ==")
foreign = subprocess.Popen([sys.executable, str(HOLD_PY), "5731"], start_new_session=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
started.append(foreign)
time.sleep(1.5)
try:
    make_device("foreign")
    save_fields("foreign", pid=None, serial="emulator-5731",
                console_port=5731, adb_port=5732)
    body = stop_and_check("foreign", "foreign", expect_success=False)
    message = (body.get("error") or body.get("message") or "")
    check("the message names the process holding the port",
          str(foreign.pid) in message, message[:300])
    check("the foreign process is not killed", foreign.poll() is None)
finally:
    if foreign.poll() is None:
        foreign.kill()

# ------------------------------------------------------------------ 7. normal lifecycle
print("== 7. a real launch still stops cleanly (and can be launched again) ==")
make_device("lifecycle")
client.post("/api/devices/lifecycle/launch", json={"frida": False})
deadline = time.time() + 60
state = {}
while time.time() < deadline:
    state = client.get("/api/devices/lifecycle/status").get_json()
    if state.get("state") in ("running", "error"):
        break
    time.sleep(0.3)
check("device launched", state.get("state") == "running", json.dumps(state)[:300])
body = stop_and_check("lifecycle", "lifecycle")
check("stop response says idle", body.get("state") == "idle", json.dumps(body)[:200])
r = client.post("/api/devices/lifecycle/launch", json={"frida": False})
check("it can be launched again right after",
      r.get_json().get("message") != "Launch already in progress",
      json.dumps(r.get_json()))
deadline = time.time() + 60
while time.time() < deadline:
    state = client.get("/api/devices/lifecycle/status").get_json()
    if state.get("state") in ("running", "error"):
        break
    time.sleep(0.3)
check("second launch reaches running", state.get("state") == "running",
      json.dumps(state)[:300])
client.post("/api/devices/lifecycle/stop", json={"force": True})

# ------------------------------------------------------------------ 8. static guards
print("== 8. wiring ==")
DM = (REPO / "core" / "device_manager.py").read_text(encoding="utf-8")
APP = (REPO / "gui" / "app.py").read_text(encoding="utf-8")
JS = (REPO / "gui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
check("stop sweeps lingering emulators", "_sweep_lingering_emulator" in DM)
check("the sweep finds the port owner", "_port_owner_pid" in DM)
check("kill escalates SIGTERM -> SIGKILL", "_signal_tree(15)" in DM and "_signal_tree(9)" in DM)
check("_pid_alive ignores zombies", 'state not in ("Z", "X")' in DM)
check("a recycled pid is never killed", "_pid_looks_like_emulator" in DM
      and "_pid_exe_name" in DM)
check("kills target the session leader, not an ancestor of ours", "_emulator_root" in DM
      and "own_sid" in DM)
check("port probing does not trust bind()/connect() alone",
      "_listener_ports" in DM and "_connect_says_busy" in DM)
check("stop failure is stored separately from a launch failure",
      'error_key="stop_error"' in APP)
check("a stop that could not finish returns a real error",
      '"still_running"' in APP)
check("the card shows and retries a failed stop",
      "Stop didn't finish" in JS and "↻ Retry stop" in JS)
check("the dashboard verifies the device is really down after stop",
      "confirmDeviceStopped" in JS)

print()
for proc in started:
    if proc.poll() is None:
        proc.kill()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All 'stop really stops' checks passed.")
