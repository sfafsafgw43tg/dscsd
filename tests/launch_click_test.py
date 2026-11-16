#!/usr/bin/env python3
"""Regression test: the Launch button must never be a dead click.

Reported symptom (screenshot: *first_device*, Samsung Galaxy S24, **Stopped**,
"✓ Root capable"): "after closing the device I see Launch but I cannot click
it". Nothing happened - no emulator, no error, no toast.

Three separate causes were found and are locked down here:

1. **A launch chain that outlives its emulator.** Stopping a device while it
   was still booting left the background launch thread waiting out the whole
   boot timeout. `POST /api/devices/<name>/launch` answered
   ``{"success": true, "message": "Launch already in progress"}`` - a *success*
   for a request that does nothing - so the UI said "Launching…" and the
   device stayed Stopped forever. Stop now cancels the chain (epoch token),
   and a chain state with no live chain is never trusted.
2. **A card that looked clickable but was not.** Busy buttons were rendered
   with the ``disabled`` attribute: right-click, faded, and completely
   silent - the click vanished. They are now ``aria-disabled`` so the click
   still reaches the handler and explains itself.
3. **A click lost to a re-render.** The grid re-renders every 10 s; a press
   whose mousedown landed on the old button and mouseup on the new one fired
   on the grid and was dropped. The handler now recovers the action from
   where the press started.

Run: ``python tests/launch_click_test.py`` (exit 1 = fail).
"""
import json
import os
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

import core.device_manager as dm  # noqa: E402
from core.device_manager import DeviceManager, LaunchCancelledError  # noqa: E402
import gui.app as ga  # noqa: E402

JS = (REPO / "gui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
CSS = (REPO / "gui" / "static" / "css" / "style.css").read_text(encoding="utf-8")
APP = (REPO / "gui" / "app.py").read_text(encoding="utf-8")


def wait_state(client, name, wanted, timeout=40.0):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = client.get(f"/api/devices/{name}/status").get_json()
        if last.get("state") in wanted:
            return last
        time.sleep(0.25)
    return last


# ------------------------------------------------------------------ 1. cancel tokens
print("== 1. stop cancels a launch that is still booting ==")
mgr = DeviceManager()
token = mgr.begin_launch_token("epochdev")
check("a fresh launch token is live", token() is True)
mgr.cancel_launch("epochdev")
check("stop cancels the in-flight launch token", token() is False)
check("a new launch gets a fresh, live token after the cancel",
      mgr.begin_launch_token("epochdev")() is True)

# wait_for_boot honours the cancel predicate instead of burning the timeout
started = time.time()
try:
    mgr.wait_for_boot("emulator-5554", timeout=30, cancel=lambda: True)
    check("wait_for_boot aborts when the launch is cancelled", False)
except LaunchCancelledError:
    check("wait_for_boot aborts when the launch is cancelled",
          time.time() - started < 5, f"{time.time() - started:.1f}s")
except Exception as exc:  # pragma: no cover - wrong exception type
    check("wait_for_boot aborts when the launch is cancelled", False, repr(exc))
mgr.cancel_launch("epochdev")

# ------------------------------------------------------------------ 2. GUI end-to-end
print("== 2. launch click works after a stop (API) ==")
app = ga.create_app()
client = app.test_client()

r = client.post("/api/devices", json={
    "model": "Samsung Galaxy S24", "android_version": "14", "name": "first_device"})
check("device created", r.status_code == 200 and r.get_json().get("success") is True)

r = client.post("/api/devices/first_device/launch", json={"frida": False})
body = r.get_json()
check("first launch accepted", body.get("success") is True and body.get("state") == "starting",
      json.dumps(body))
status = wait_state(client, "first_device", {"running", "error"})
check("first launch reaches running", status.get("state") == "running",
      json.dumps(status)[:300])

r = client.post("/api/devices/first_device/stop", json={"force": True})
check("stop succeeds", r.get_json().get("success") is True, json.dumps(r.get_json()))
after_stop = client.get("/api/devices/first_device/status").get_json()
check("stop leaves the chain state idle (no phantom 'booting')",
      after_stop.get("launch_state") == "idle" and after_stop.get("state") == "idle",
      json.dumps(after_stop)[:300])
listing = client.get("/api/devices").get_json()
card = next(d for d in listing if d["name"] == "first_device")
check("card is not 'starting' after stop", not card.get("starting"), json.dumps(card)[:300])

r = client.post("/api/devices/first_device/launch", json={"frida": False})
body = r.get_json()
check("Launch click after stop really starts a launch",
      body.get("success") is True and body.get("message") != "Launch already in progress",
      json.dumps(body))
status = wait_state(client, "first_device", {"running", "error"})
check("relaunch reaches running", status.get("state") == "running", json.dumps(status)[:300])

# ------------------------------------------------------------------ 3. stop mid-boot
print("== 3. stopping a device that is still booting is not a dead launch ==")
client.post("/api/devices", json={
    "model": "Google Pixel 8", "android_version": "14", "name": "midboot"})
original = dm.DeviceManager.launch_device
gate = threading.Event()


def gated(self, *args, **kwargs):
    gate.wait(10)
    return original(self, *args, **kwargs)


dm.DeviceManager.launch_device = gated
try:
    r = client.post("/api/devices/midboot/launch", json={"frida": False})
    check("launch accepted before the chain starts", r.get_json().get("success") is True)
    r2 = client.post("/api/devices/midboot/launch", json={"frida": False})
    check("a second click *while the chain is alive* is reported as in progress",
          r2.get_json().get("message") == "Launch already in progress", json.dumps(r2.get_json()))
    r = client.post("/api/devices/midboot/stop", json={"force": True})
    check("stop during boot succeeds", r.get_json().get("success") is True, json.dumps(r.get_json()))
    check("stop cleared the chain state", ga._launch_state.get("midboot", {}).get("state") == "idle",
          json.dumps(ga._launch_state.get("midboot")))
finally:
    gate.set()
    dm.DeviceManager.launch_device = original

deadline = time.time() + 20
settled = {}
while time.time() < deadline:
    settled = ga._launch_state.get("midboot", {})
    if settled.get("state") not in ("starting", "booting"):
        break
    time.sleep(0.25)
check("cancelled chain does not resurrect 'booting'", settled.get("state") == "idle",
      json.dumps(settled))
check("cancelled chain left no running emulator",
      client.get("/api/devices/midboot/status").get_json().get("running") is False)
r = client.post("/api/devices/midboot/launch", json={"frida": False})
check("launch works after a cancelled boot",
      r.get_json().get("success") is True
      and r.get_json().get("message") != "Launch already in progress",
      json.dumps(r.get_json()))
status = wait_state(client, "midboot", {"running", "error"})
check("device runs after a cancelled boot", status.get("state") == "running",
      json.dumps(status)[:300])
client.post("/api/devices/midboot/stop", json={"force": True})

# ------------------------------------------------------------------ 4. stale chain state
print("== 4. a chain state with no chain behind it cannot eat clicks ==")
client.post("/api/devices", json={
    "model": "Google Pixel 7a", "android_version": "14", "name": "stale"})
ga._set_state("stale", state="booting")
listing = client.get("/api/devices").get_json()
card = next(d for d in listing if d["name"] == "stale")
check("stale 'booting' is not shown as starting", not card.get("starting"),
      json.dumps(card)[:300])
check("stale 'booting' is reported as idle",
      client.get("/api/devices/stale/status").get_json().get("launch_state") == "idle")
r = client.post("/api/devices/stale/launch", json={"frida": False})
check("launch is not refused by the stale state",
      r.get_json().get("message") != "Launch already in progress",
      json.dumps(r.get_json()))
status = wait_state(client, "stale", {"running", "error"})
check("stale device launches", status.get("state") == "running", json.dumps(status)[:300])
client.post("/api/devices/stale/stop", json={"force": True})

# ------------------------------------------------------------------ 5. static guards
print("== 5. UI wiring ==")
check("busy card buttons are aria-disabled (clicks still explain themselves)",
      'aria-disabled="true"' in JS and '<button class="btn btn-success btn-sm' in JS)
check("card markup no longer uses the silent disabled attribute for actions",
      "${isDisabled ? 'disabled' : ''}" not in JS)
check("a click on a dead button produces a toast",
      '.btn[aria-disabled="true"], .btn[disabled]' in JS)
check("launch dispatcher is shared (re-render safe)",
      "function runCardAction(" in JS and "runCardAction(target.dataset.action" in JS)
check("press position is remembered across re-renders", "pressedAction" in JS
      and "pointerdown" in JS)
check("a no-op 'already in progress' answer is surfaced, not swallowed",
      "/already in progress/i.test" in JS and "retried" in JS)
check("launch re-asks once when the device is really down",
      "deviceIsRunning" in JS and "launchDevice(name, { retried: true })" in JS)
check("a failed launch becomes a one-click retry", "↻ Retry launch" in JS)
check("Launching state stays visible on the card", "⏳ Launching…" in JS)
check("busy buttons have a distinct style", ".btn.is-busy" in CSS)
check("aria-disabled buttons look inert", '.btn[aria-disabled="true"]' in CSS)
check("stop cancels the launch chain in the server",
      "self.cancel_launch(name)" in (REPO / "core" / "device_manager.py").read_text(encoding="utf-8"))
check("launch state is cleared when the chain ends", "set_launch_finished" in APP)
check("a chain state is only trusted while its chain is alive",
      "_launch_thread_alive" in APP)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    print("fake sdk log tail:\n" + paths["log"].read_text()[-3000:])
    sys.exit(1)
print("All launch-click checks passed.")
