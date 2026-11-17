#!/usr/bin/env python3
"""Regression test: "No free emulator console port available" on a clean machine.

Reported (screenshot of Device Details + toast): the card is **Stopped**, the
device was never launched - and the launch dies with

    No free emulator console port available (tried 6 ports starting at 5554).
    Stop any leftover emulator processes and try again.

Nothing was running. The port probes were the problem:

* ``_connect_says_busy`` treated *any* non-refused answer as "taken" - on
  Windows a closed port regularly answers with ``WSAEINVAL`` (unreachable)
  rather than a refusal, so every candidate looked busy;
* ``_listener_ports`` counted a listener as ours when its pid was *any* live
  process (on Windows every pid qualified), so an unrelated local server made
  its port permanently unusable;
* the allocator gave up after a few ports instead of falling back.

Pinned here:

1. a closed port is free even when ``connect`` cannot prove it;
2. a listener that is not an emulator never makes a port unusable;
3. the allocator skips busy ports, and when *every* probe is wrong it still
   hands back a port instead of refusing the launch;
4. leftover emulators are killed and their ports reused;
5. the doctor reports port state, and the toast shows the real reason.
"""
import json
import os
import socket
import sys
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

from core.device_manager import DeviceManager  # noqa: E402
import core.device_manager as dm  # noqa: E402
import gui.app as ga  # noqa: E402

JS = (REPO / "gui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
APP = (REPO / "gui" / "app.py").read_text(encoding="utf-8")
SRC = (REPO / "core" / "device_manager.py").read_text(encoding="utf-8")

mgr = DeviceManager()

# ------------------------------------------------------- a real closed port
def closed_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def listening_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


print("== 1. a closed port is free, even when connect() gives up ==")
free_port = closed_port()
check("a genuinely closed port is not 'in use'",
      DeviceManager._port_in_use(free_port) is False, str(free_port))

# The Windows signature: connect() raises something other than ECONNREFUSED
# (WSAEINVAL / unreachable) for a port nobody listens on. That must not mean
# "taken" any more.
orig_connect = DeviceManager.__dict__["_connect_says_busy"]
DeviceManager._connect_says_busy = staticmethod(lambda port, timeout=0.3: True)
try:
    check("connect() saying 'busy' on its own no longer blocks a port",
          DeviceManager._port_in_use(closed_port()) is False)
    body = SRC.split("def _port_in_use")[1].split("def _connect_says_busy")[0]
    check("the deciding path never trusts a bare connect()",
          "return port in listeners" in body and "return DeviceManager._connect_says_busy" not in body,
          body[:200])
finally:
    DeviceManager._connect_says_busy = orig_connect

print("== 2. a foreign listener never makes a port unusable ==")
srv, busy_port = listening_port()
try:
    check("a listener we cannot attribute is not counted as an emulator",
          busy_port not in DeviceManager._listener_ports()
          or DeviceManager._pid_looks_like_emulator(DeviceManager._port_owner_pid(busy_port)) is False,
          str(busy_port))
    # Even if the listener table somehow reports it, the port must not be
    # treated as one of ours (that was the Windows bug).
    orig_lp = DeviceManager.__dict__["_listener_ports"]
    DeviceManager._listener_ports = staticmethod(lambda: {busy_port})
    try:
        owner = DeviceManager._port_owner_pid(busy_port)
        if owner:
            check("an unrelated process is not 'ours'",
                  DeviceManager._pid_is_ours(owner) is False, f"pid {owner}")
        else:
            check("an unrelated process is not 'ours' (no owner readable)", True)
        check("_emulator_evidence does not use a bare live pid",
              "if owner and self._pid_is_ours(owner)" not in SRC)
    finally:
        DeviceManager._listener_ports = orig_lp
finally:
    srv.close()

print("== 3. allocation skips busy ports and never dead-ends ==")
check("the scan window is bounded",
      "PORT_SCAN_SPAN" in SRC and "for port in range(" in SRC)
check("the old unbounded while-loop is gone",
      "while port in used or self._port_in_use(port)" not in SRC)

orig_in_use = DeviceManager.__dict__["_port_in_use"]
try:
    # Two ports busy: the allocator must skip them and say which.
    busy = {int(config.EMULATOR_PORT_START), int(config.EMULATOR_PORT_START) + 2}
    DeviceManager._port_in_use = staticmethod(lambda port: port in busy)
    mgr.begin_launch_progress("allocdev")
    picked = mgr._allocate_console_port("allocdev")
    check("it skips the busy ports", picked not in busy, str(picked))
    check("and reports what it skipped",
          "busy" in (mgr.launch_progress("allocdev")["detail"] or "").lower(),
          mgr.launch_progress("allocdev")["detail"])

    # Every probe lies: the launch must still get a port to try.
    DeviceManager._port_in_use = staticmethod(lambda port: True)
    mgr.begin_launch_progress("alldev")
    try:
        port = mgr._allocate_console_port("alldev")
        raised = None
    except Exception as exc:
        port, raised = None, exc
    check("when every probe says busy it still returns a port to try",
          raised is None and port is not None, repr(raised))
    check("...and says the emulator will decide",
          "letting the emulator decide" in (mgr.launch_progress("alldev")["detail"] or ""),
          (mgr.launch_progress("alldev")["detail"] or "")[:120])
finally:
    DeviceManager._port_in_use = orig_in_use

print("== 4. leftover emulators are freed, their ports reused ==")
check("there is a cleanup for orphaned emulators",
      hasattr(DeviceManager, "_release_leftover_emulators"))
killed = []
busy_ports = {5554, 5556}
orig_lp = DeviceManager.__dict__["_listener_ports"]
orig_owner = DeviceManager.__dict__["_port_owner_pid"]
orig_looks = DeviceManager.__dict__["_pid_looks_like_emulator"]
try:
    DeviceManager._listener_ports = staticmethod(lambda: set(busy_ports))
    DeviceManager._port_owner_pid = staticmethod(lambda port: 4242)
    DeviceManager._pid_looks_like_emulator = staticmethod(lambda pid: int(pid) == 4242)
    DeviceManager._port_in_use = staticmethod(lambda port: port in busy_ports)

    def fake_kill(pid):
        killed.append(pid)
        busy_ports.clear()

    mgr._kill_process_tree = staticmethod(fake_kill)
    freed = mgr._release_leftover_emulators()
    check("it kills the leftover emulator", set(killed) == {4242}, str(killed))
    check("it reports the ports it freed",
          [p for p, _pid in freed] == [5554, 5556], str(freed))
    check("the freed port is used by the next launch",
          mgr._allocate_console_port() == 5554)

    # A foreign program must never be killed.
    killed.clear()
    busy_ports_backup = set(busy_ports)
    busy_ports.update({5554})
    DeviceManager._listener_ports = staticmethod(lambda: {5554})
    DeviceManager._pid_looks_like_emulator = staticmethod(lambda pid: False)
    mgr._release_leftover_emulators()
    check("a non-emulator listener is left alone", killed == [], str(killed))
    busy_ports.clear()
    busy_ports.update(busy_ports_backup)
finally:
    DeviceManager._listener_ports = orig_lp
    DeviceManager._port_owner_pid = orig_owner
    DeviceManager._pid_looks_like_emulator = orig_looks
    DeviceManager._port_in_use = orig_in_use

print("== 5. a launch on a clean machine really works end to end ==")
app = ga.create_app()
client = app.test_client()
r = client.post("/api/devices", json={
    "model": "Google Pixel 8 Pro", "android_version": "14", "name": "portdev"})
check("device created", r.get_json().get("success") is True, str(r.json)[:200])
r = client.post("/api/devices/portdev/launch", json={"frida": False})
check("launch accepted", r.get_json().get("success") is True, str(r.json)[:200])
import time  # noqa: E402

status = {}
deadline = time.time() + 90
while time.time() < deadline:
    status = client.get("/api/devices/portdev/status").get_json()
    if status.get("launch_state") in ("running", "error", "idle"):
        break
    time.sleep(0.3)
error_text = str(status.get("error") or "")
check("the launch did not fail on ports", "no free emulator console port" not in error_text.lower(),
      error_text[:200])
check("the device is running", status.get("running") is True, json.dumps(status)[:250])
check("boot_progress shows the port step",
      any("port" in (h.get("text") or "").lower()
          for h in (status.get("boot_progress") or {}).get("history", [])),
      json.dumps((status.get("boot_progress") or {}).get("history"))[:300])

print("== 6. the UI and doctor explain a port problem ==")
check("the toast shows the first line of the error, not a generic follow-up",
      "firstLine" in JS and "Launch failed: ${firstLine}" in JS)
check("the old 'open Details for the log tail' toast wording is gone",
      "Open Details for the log tail, or Settings" not in JS)
from core.doctor import check_emulator_ports, run_doctor  # noqa: E402

result = check_emulator_ports()
check("the doctor has a port check",
      result["id"] == "emulator_ports" and result["title"] == "Emulator ports",
      str(result))
check("it reports a severity", result["severity"] in ("ok", "info", "warn", "error"))
check("it names the next console port",
      str(config.EMULATOR_PORT_START) in result["detail"], result["detail"])
titles = [c["title"] for c in run_doctor()[0]]
check("run_doctor includes it", "Emulator ports" in titles, str(titles))

if failures:
    print(f"\n{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("\nAll port-allocation checks passed.")
