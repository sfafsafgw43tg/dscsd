#!/usr/bin/env python3
"""Closing the emulator must show "Stopping…", and a dead Launch gets Kill.

Two reports, one workflow:

1. *"after I close the running device make it show stopping in the app"* — when
   the user closes the emulator window (or the app asks it to stop), the card
   must show a **Stopping…** state that ends when the emulator is really gone.
   Before this, the stop ran inside the request: the card sat on "Running" for
   the whole teardown, then flipped to "Stopped" with nothing in between.

2. *"after its stopped if i cant click launch make appear a kill device
   button"* — a closed emulator can leave a qemu child (or a ghost adb serial,
   or a recycled pid) holding its ports, and the next Launch is then refused
   with "Device already running" while the card says Stopped. The card now says
   **Emulator closed** and offers **Kill device**, and the kill endpoint (and
   ``main.py kill``) really frees the device.

Run: ``python tests/stop_state_test.py`` (exit 1 = fail).
"""
import json
import os
import shutil
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

app = ga.create_app()
client = app.test_client()
mgr = DeviceManager()
JS = (REPO / "gui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
CSS = (REPO / "gui" / "static" / "css" / "style.css").read_text(encoding="utf-8")

BASH = shutil.which("bash")
FAKE_EMU = TMP / "emulator"
if BASH:
    shutil.copy(BASH, FAKE_EMU)

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


def saved(name):
    return json.loads((config.SAVED_DEVICES_DIR / f"{name}.json").read_text(encoding="utf-8"))


def spawn_lingering(port=None):
    """An `emulator`-named process that ignores SIGTERM (a wedged VM)."""
    script = TMP / f"linger_{time.time_ns()}.sh"
    body = "#!/bin/bash\ntrap '' TERM\n"
    if port:
        body += f"python3 -c \"import socket,time;s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('127.0.0.1',{port}));s.listen(0);time.sleep(120)\" &\n"
    body += "sleep 300\n"
    script.write_text(body)
    proc = subprocess.Popen([str(FAKE_EMU), str(script)], start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    started.append(proc)
    return proc


# ------------------------------------------------ 1. "stopping" is a real state
print("== 1. a stop reports 'stopping' immediately and finishes in the background ==")
make_device("slowstop")
proc = spawn_lingering(port=5741)
time.sleep(1.0)
save_fields("slowstop", pid=proc.pid, serial="emulator-5741",
            console_port=5741, adb_port=5742)

t0 = time.time()
r = client.post("/api/devices/slowstop/stop", json={"wait": False})
body = r.get_json()
elapsed = time.time() - t0
check("the async stop answers immediately", elapsed < 3.0, f"{elapsed:.1f}s")
check("it reports state 'stopping'", body.get("state") == "stopping", json.dumps(body))
check("it is accepted (success)", body.get("success") is True, json.dumps(body))

listed = {d["name"]: d for d in client.get("/api/devices").get_json()}
dev = listed.get("slowstop") or {}
check("the dashboard is told the device is stopping", dev.get("stopping") is True,
      json.dumps({k: dev.get(k) for k in ("running", "stopping", "launch_state")}))
check("it carries when the stop started", bool(dev.get("stop_started_at")))

deadline = time.time() + 60
while time.time() < deadline:
    dev = next(d for d in client.get("/api/devices").get_json() if d["name"] == "slowstop")
    if not dev.get("stopping"):
        break
    time.sleep(0.4)
check("the stopping state clears by itself", dev.get("stopping") is False, json.dumps(dev)[:200])
check("the emulator really is gone", dev.get("running") is False, json.dumps(dev)[:200])
check("the wedged process is dead", proc.poll() is not None)

# ------------------------------------------------ 2. a synchronous stop still works
print("== 2. the default stop is still synchronous (scripts/tests rely on it) ==")
make_device("syncstop")
proc2 = spawn_lingering()
time.sleep(0.8)
save_fields("syncstop", pid=proc2.pid, serial="emulator-5743",
            console_port=5743, adb_port=5744)
body = client.post("/api/devices/syncstop/stop", json={"force": True}).get_json()
check("stop reports success", body.get("success") is True, json.dumps(body)[:300])
check("it ends in state idle", body.get("state") == "idle", json.dumps(body)[:200])
check("nothing is left running",
      client.get("/api/devices/syncstop/status").get_json().get("running") is False)

# ------------------------------------------------ 3. emulator closed -> Kill button
print("== 3. a closed emulator is reported, and gets a Kill device button ==")
make_device("closed")
save_fields("closed", pid=None, serial="emulator-5751", console_port=5751,
            adb_port=5752, launched_at="2026-01-01T10:00:00")
dev = next(d for d in client.get("/api/devices").get_json() if d["name"] == "closed")
check("the card learns the emulator was closed", dev.get("emulator_gone") is True,
      json.dumps({k: dev.get(k) for k in ("running", "emulator_gone", "stopped_at")}))
check("it is not reported as running", dev.get("running") is False)

# A device that was stopped by the app is *not* "gone":
make_device("stoppedok")
save_fields("stoppedok", stopped_at="2026-11-16T12:00:00",
            launched_at="2026-11-16T11:00:00")
dev2 = next(d for d in client.get("/api/devices").get_json() if d["name"] == "stoppedok")
check("a cleanly stopped device is not flagged as closed",
      dev2.get("emulator_gone") is False, json.dumps(dev2)[:200])

check("the card renders a Kill device button", 'data-action="kill"' in JS)
check("it says 'Emulator closed' when the window was closed", "Emulator closed" in JS)
check("the closed card explains why Kill is there", "stopped-notice" in JS and "stopped-notice" in CSS)

# ------------------------------------------------ 4. kill frees a wedged emulator
print("== 4. Kill device really frees a wedged emulator ==")
make_device("wedged")
proc3 = spawn_lingering(port=5761)
time.sleep(1.2)
save_fields("wedged", pid=proc3.pid, serial="emulator-5761",
            console_port=5761, adb_port=5762, launched_at="2026-11-16T09:00:00")
r = client.post("/api/devices/wedged/kill")
body = r.get_json()
check("kill answers success", body.get("success") is True, json.dumps(body)[:300])
check("kill says what it did", bool(body.get("note")), json.dumps(body)[:300])
check("the wedged process is dead", proc3.poll() is not None)
dev = next(d for d in client.get("/api/devices").get_json() if d["name"] == "wedged")
check("the device no longer looks running", dev.get("running") is False, json.dumps(dev)[:200])
check("its launch state is cleared", dev.get("launch_state") in ("idle", None),
      json.dumps(dev)[:200])
blocker = mgr._launch_blocker("wedged")
check("nothing blocks the next launch", blocker is False, "launch is still blocked")

# ------------------------------------------------ 5. kill on an idle device is honest
print("== 5. killing nothing says so instead of pretending ==")
make_device("idlekill")
r = client.post("/api/devices/idlekill/kill")
body = r.get_json()
check("kill on an idle device succeeds", body.get("success") is True, json.dumps(body)[:300])
check("it says there was nothing left", "Nothing was left to kill" in (body.get("note") or ""),
      json.dumps(body)[:300])
check("unknown devices are refused", client.post("/api/devices/nope/kill").status_code == 404)

# ------------------------------------------------ 6. closing the window is detected
print("== 6. a closed emulator no longer blocks the next launch ==")
make_device("reopen")
proc4 = spawn_lingering()
time.sleep(0.8)
save_fields("reopen", pid=proc4.pid, serial="emulator-5771",
            console_port=5771, adb_port=5772,
            launched_at="2026-01-01T10:00:00")
proc4.kill()                      # the user closes the emulator window
proc4.wait(timeout=10)
time.sleep(0.3)
check("a dead emulator leaves a ghost record behind",
      bool(saved("reopen").get("pid") or saved("reopen").get("serial")))
dev = next(d for d in client.get("/api/devices").get_json() if d["name"] == "reopen")
check("it is not reported as running", dev.get("running") is False, json.dumps(dev)[:200])
check("the ghost is not treated as a blocker anymore",
      mgr._launch_blocker("reopen") is False, "still blocked")
check("the stale pid/serial were cleared",
      not saved("reopen").get("pid"), json.dumps(saved("reopen"))[:200])

# ------------------------------------------------ 7. CLI + wiring
print("== 7. main.py kill and the dashboard wiring ==")
from click.testing import CliRunner  # noqa: E402

import main as cli_main  # noqa: E402

result = CliRunner().invoke(cli_main.cli, ["kill", "idlekill"])
check("main.py kill exits 0 on an idle device", result.exit_code == 0, result.output)
check("it prints what it found", "Nothing was left to kill" in result.output, result.output)
result = CliRunner().invoke(cli_main.cli, ["kill", "ghostdevice"])
check("main.py kill exits 1 for an unknown device", result.exit_code == 1, result.output)

check("the stop route started the teardown in a thread", "stop-" in (REPO / "gui" / "app.py").read_text(encoding="utf-8"))
check("a second stop does not start a parallel kill",
      "Stop already in progress" in (REPO / "gui" / "app.py").read_text(encoding="utf-8"))
check("the card polls fast while stopping", "d.starting || d.stopping" in JS)
check("the stopping pill exists", ".device-status.stopping" in CSS)
check("the elapsed counter is rendered", "data-stop-started" in JS)
check("the kill handler is wired to the delegated dispatcher",
      "action === 'kill'" in JS and "killDevice" in JS)
check("the stop error offers Kill device", "🔪 Kill device" in JS)

for proc in started:
    if proc.poll() is None:
        proc.kill()

if failures:
    print(f"\n{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("\nAll stop-state / kill-device checks passed.")
