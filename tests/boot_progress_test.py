#!/usr/bin/env python3
"""Regression test: a slow boot must say *what* it is doing.

Reported: "the device takes a long time to boot and idk if it even boots —
make a text on the left to starting and show what is loading".

Before this, the card showed one unchanging line for the whole chain
("Booting AVD → auto-root → spoof → Frida hooks…") plus a "Starting…" pill.
A three-minute first boot and a dead emulator looked identical.

Now the launch chain reports each phase to the manager, the API exposes it as
``boot_progress``, and the card renders a step list with a bar, the elapsed
time and one sentence describing the current step.

Checks:
1. the manager only lists sane phases and computes a monotonic percentage;
2. a real (fake-SDK) launch walks preparing → starting → booting → … and the
   API exposes the running phase while the boot is in flight;
3. a failed launch freezes a progress record with the error, instead of
   spinning forever;
4. stop clears the record (no stale "booting" after the user closed it);
5. the dashboard source renders the panel from ``boot_progress`` and keeps a
   fast refresh while something is starting.
"""
import json
import os
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

from core.device_manager import DeviceManager  # noqa: E402
import gui.app as ga  # noqa: E402

JS = (REPO / "gui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
CSS = (REPO / "gui" / "static" / "css" / "style.css").read_text(encoding="utf-8")
APP = (REPO / "gui" / "app.py").read_text(encoding="utf-8")

# ------------------------------------------------------------------ 1. the model
print("== 1. phases and percentage ==")
mgr = DeviceManager()
keys = [k for k, _l, _w in DeviceManager.LAUNCH_PHASES]
check("a boot has the phases the user cares about",
      keys == ["preparing", "starting", "booting", "rooting", "spoofing",
               "hooking", "finishing"], str(keys))
check("no progress before a launch", mgr.launch_progress("nothing_here") is None)

mgr.begin_launch_progress("progdev")
first = mgr.launch_progress("progdev")
check("a fresh record starts at preparing",
      first["phase"] == "preparing" and first["index"] == 0, json.dumps(first)[:200])
check("it has a label and a percentage",
      bool(first["label"]) and isinstance(first["percent"], int), json.dumps(first)[:200])
check("elapsed time is tracked", "elapsed" in first and first["elapsed"] >= 0)

percents = [first["percent"]]
seen = []
for phase in keys[1:]:
    mgr.report_launch_progress("progdev", phase, f"doing {phase}")
    info = mgr.launch_progress("progdev")
    seen.append(info["phase"])
    percents.append(info["percent"])
check("each phase is reported in order", seen == keys[1:], str(seen))
check("the percentage never goes backwards",
      percents == sorted(percents), str(percents))
check("finishing is not 100% before anything else ran", percents[-1] >= 90,
      str(percents))
check("the detail sentence is carried through",
      mgr.launch_progress("progdev")["detail"] == "doing finishing")

# a marker like "(60/120)" animates the bar inside one long phase
mgr.begin_launch_progress("progdev")
mgr.report_launch_progress("progdev", "booting", "Booting Android (0/120)")
early = mgr.launch_progress("progdev")["percent"]
mgr.report_launch_progress("progdev", "booting", "Booting Android (60/120)")
mid = mgr.launch_progress("progdev")["percent"]
mgr.report_launch_progress("progdev", "booting", "Booting Android (120/120)")
late = mgr.launch_progress("progdev")["percent"]
check("progress inside the boot phase moves the bar", early < mid < late,
      f"{early} < {mid} < {late}")
try:
    mgr.report_launch_progress("progdev", "spoofin", "typo")
    refused = False
except ValueError:
    refused = True
check("an unknown phase is refused (a typo cannot freeze the UI)", refused)
mgr.clear_launch_progress("progdev")
check("clearing removes the record", mgr.launch_progress("progdev") is None)

# ------------------------------------------------------------------ 2. live launch
print("== 2. a real launch reports the phase it is in ==")
app = ga.create_app()
client = app.test_client()
r = client.post("/api/devices", json={
    "model": "Google Pixel 7a", "android_version": "14", "name": "slowdev"})
check("device created", r.status_code == 200 and r.get_json().get("success") is True,
      str(r.status_code))

r = client.post("/api/devices/slowdev/launch", json={"frida": False})
check("launch accepted", r.get_json().get("success") is True, str(r.get_json()))

status = client.get("/api/devices/slowdev/status").get_json()
check("status exposes boot_progress right away",
      isinstance(status.get("boot_progress"), dict), json.dumps(status)[:300])
check("the first report names the phase it is preparing",
      status["boot_progress"]["phase"] in ("preparing", "starting", "booting"),
      json.dumps(status.get("boot_progress"))[:300])

listing = client.get("/api/devices").get_json()
card = next(d for d in listing if d["name"] == "slowdev")
check("the dashboard list carries boot_progress for a starting card",
      card.get("starting") is True and isinstance(card.get("boot_progress"), dict),
      json.dumps(card)[:300])
check("the card has a label to show",
      bool(card["boot_progress"].get("label")), json.dumps(card.get("boot_progress"))[:300])

# Watch the phases advance while the fake SDK boots. The fake boots in about a
# second, so record every report the manager receives instead of hoping to
# catch each one by polling.
from core.device_manager import DeviceManager as _DM  # noqa: E402

observed = []
_orig_report = _DM.report_launch_progress


def _record(self, name, phase, detail="", error=None):
    if name == "walkdev" and phase not in observed:
        observed.append(phase)
    return _orig_report(self, name, phase, detail, error)


_DM.report_launch_progress = _record
try:
    r = client.post("/api/devices", json={
        "model": "Google Pixel 7a", "android_version": "14", "name": "walkdev"})
    r = client.post("/api/devices/walkdev/launch", json={"frida": False})
    # Wait for the *chain* to finish (launch_state), not for "running": the
    # emulator process is up long before the spoof/hooks steps are done.
    deadline = time.time() + 120
    while time.time() < deadline:
        st = client.get("/api/devices/walkdev/status").get_json()
        if st.get("launch_state") in ("running", "error", "idle"):
            break
        time.sleep(0.3)
finally:
    _DM.report_launch_progress = _orig_report

check("the launch walked several phases", len(observed) >= 3, str(observed))
check("it reached booting", "booting" in observed, str(observed))
check("it finished the chain", observed[-1] == "finishing", str(observed))
check("the phases arrive in order",
      observed == [k for k in keys if k in observed], str(observed))
check("every observed phase is a declared phase", set(observed) <= set(keys), str(observed))
final = client.get("/api/devices/walkdev/status").get_json()
check("the chain ends with a 'Ready' sentence",
      (final.get("boot_progress") or {}).get("phase") == "finishing"
      and "Ready" in ((final.get("boot_progress") or {}).get("detail") or ""),
      json.dumps(final.get("boot_progress"))[:300])
check("...at (nearly) 100%", (final.get("boot_progress") or {}).get("percent", 0) >= 90,
      str((final.get("boot_progress") or {}).get("percent")))

# ------------------------------------------------------------------ 3. failure
print("== 3. a failed launch stops spinning and says why ==")
mgr.begin_launch_progress("faildev")
mgr.report_launch_progress("faildev", "starting", "Starting the emulator process")
mgr.fail_launch_progress("faildev", "Emulator exited immediately (code 1)")
bp = mgr.launch_progress("faildev")
check("the failure is recorded on the progress record",
      bp["failed"] is True and "code 1" in (bp["error"] or ""), json.dumps(bp)[:300])
check("the detail line is the error, not a spinner",
      "code 1" in (bp["detail"] or ""), json.dumps(bp)[:300])

# ------------------------------------------------------------------ 4. stop
print("== 4. stopping clears the boot panel ==")
r = client.post("/api/devices/slowdev/stop", json={"force": True})
check("stop accepted", r.get_json().get("success") is True, str(r.get_json()))
check("no boot_progress survives a stop",
      client.get("/api/devices/slowdev/status").get_json().get("boot_progress") in (None, {}),
      json.dumps(client.get("/api/devices/slowdev/status").get_json())[:300])
check("the stopped card is not 'starting'",
      not next(d for d in client.get("/api/devices").get_json()
               if d["name"] == "slowdev").get("starting"))

# ------------------------------------------------------------------ 5. the UI
print("== 5. the card renders the steps, the bar and the current step ==")
check("the JS has a boot panel builder", "function buildBootPanel(" in JS)
check("it reads boot_progress from the API",
      "device.boot_progress" in JS and "boot_progress" in APP)
for phase in ("Checking SDK", "Starting the emulator", "Booting Android", "Enabling root",
              "spoof", "Frida hooks", "Finishing up"):
    check(f"the panel can show '{phase}'", phase in JS or phase in APP)
check("the pill shows a percentage while starting",
      "Starting${device.boot_progress" in JS.replace(" ", "").replace("\n", "")
      or "boot_progress.percent" in JS)
check("elapsed time is formatted for humans", "function formatElapsed(" in JS)
check("a failed boot is not a spinner",
      "Launch failed" in JS and "boot-panel" in CSS and ".boot-panel.is-failed" in CSS)
check("the steps are announced to screen readers",
      'role="progressbar"' in JS and "aria-valuenow" in JS)
check("a starting card refreshes faster than the 10s idle poll",
      "ensureBootPolling" in JS and "1200" in JS)
check("the panel is animated for the active step",
      "@keyframes boot-spin" in CSS and ".boot-step.active" in CSS)
check("the old one-line banner is only the fallback",
      "Booting AVD → auto-root → spoof → Frida hooks" in JS)

# The old API shape must not break: boot_progress is additive.
check("boot_progress is additive to the status payload",
      '"boot_progress": manager.launch_progress(name)' in APP)

# ------------------------------------------------------------------ 6. no silent step
print("== 6. a step that goes quiet says so (and the wheel is not broken) ==")
mgr.PROGRESS_QUIET_BUDGET = 1.0
mgr.begin_launch_progress("quietdev")
mgr.report_launch_progress("quietdev", "preparing", "Checking hardware acceleration")
check("a fresh step is not marked stalled",
      mgr.launch_progress("quietdev")["stalled"] is False)
mgr.set_launch_running("quietdev")
mgr.start_progress_watchdog("quietdev")
time.sleep(3.5)
quiet = mgr.launch_progress("quietdev")
check("a quiet step is flagged after the budget", quiet["stalled"] is True,
      json.dumps(quiet)[:200])
check("the card is told how long it has been quiet", quiet["since_change"] >= 1.0,
      str(quiet["since_change"]))
check("the sentence names the step it is really on, not the phase name",
      "checking hardware acceleration" in quiet["detail"].lower(), quiet["detail"][:160])
check("it admits when the emulator has not logged anything",
      "has not written a log line" in quiet["detail"].lower(), quiet["detail"][:160])
mgr.report_launch_progress("quietdev", "starting", "Starting the emulator process")
after = mgr.launch_progress("quietdev")
check("a new step clears the stall flag", after["stalled"] is False)
check("...and the real sentence comes back",
      after["detail"] == "Starting the emulator process", after["detail"])
mgr.set_launch_finished("quietdev", "running")
time.sleep(2.5)  # the watchdog must not keep annotating a finished chain
finished = mgr.launch_progress("quietdev")
check("the watchdog leaves a finished chain alone",
      finished["stalled"] is False and finished["detail"] == "Starting the emulator process",
      json.dumps(finished)[:200])
check("the history records the steps that really happened",
      [h["phase"] for h in finished["history"]] == ["preparing", "preparing", "starting"],
      str([h["phase"] for h in finished["history"]]))
mgr.clear_launch_progress("quietdev")

print("== 7. nothing in 'preparing' can loop forever ==")
import inspect  # noqa: E402

src = inspect.getsource(DeviceManager._allocate_console_port)
check("the console-port scan is bounded by a count", "for _attempt in range(" in src)
check("...and by a wall-clock deadline", "time.time() + 20" in src)
check("a busy port is reported instead of a silent loop",
      "is in use - trying" in src)
accel_src = inspect.getsource(DeviceManager._acceleration_status)
check("the acceleration probe cannot hold the launch for 30s",
      "timeout=8" in accel_src and "TimeoutExpired" in accel_src)
check("the acceleration result is shown as a step",
      "Acceleration: usable" in inspect.getsource(DeviceManager.launch_device))
launch_src = inspect.getsource(DeviceManager.launch_device)
for label in ("Checking the emulator binary", "Checking adb", "Checking the AVD",
              "Checking the system image", "Choosing the renderer"):
    check(f"preparing reports '{label}'", label in launch_src)
check("the GUI starts the stall watchdog on every launch",
      "start_progress_watchdog" in APP)
check("the CLI streams the same phases",
      "launch_progress(name)" in (REPO / "main.py").read_text(encoding="utf-8"))

print("== 8. the spinner is a real ring, not a clipped arc ==")
ring = CSS[CSS.index(".boot-icon-active"):CSS.index(".boot-icon-failed")]
check("the active icon is a square box", "width: 0.78rem" in ring and "height: 0.78rem" in ring)
check("it keeps its size in the flex row", "flex: 0 0 0.78rem" in ring)
check("it uses box-sizing so the border cannot squash it",
      "box-sizing: border-box" in ring)
check("it still spins", "animation: boot-spin" in ring)
check("the keyframes rotate a full turn",
      "@keyframes boot-spin" in CSS and "rotate(360deg)" in CSS)
check("reduced-motion users get a slow ring, not a frozen one",
      "prefers-reduced-motion" in CSS)
check("the panel shows what a finished step reported", "boot-step-note" in JS and
      "boot-step-note" in CSS)
check("the quiet state is visually distinct", ".boot-now.is-quiet" in CSS and "boot-quiet" in CSS)

if failures:
    print(f"\n{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("\nAll boot-progress checks passed.")
