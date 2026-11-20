#!/usr/bin/env python3
"""Regression test: airplane mode must never be switched on automatically.

The app used to hold a device in airplane mode until its spoof verified: on
launch, after a restore, after a recovery, and it re-closed the gate whenever a
spoof failed. The user asked for it to be removed outright.

Pinned here:

1. the gate API is gone (`enforce_airplane_gate`, `_airplane_required`) and no
   source file calls it;
2. a launch that ends unspoofed leaves the radios exactly as they were;
3. a recovery of a drifted device does not toggle airplane mode;
4. a failed spoof is reported and the device stays online;
5. the dashboard never claims the spoof is holding the radios off, and shows
   the observed state instead;
6. the manual switch still works (and is still honest about failures);
7. the doctor no longer treats "radios on while unspoofed" as a problem.
"""
import json
import os
import re
import subprocess
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
(paths["state"] / "root-allowed").write_text("1\n")

from core.device_manager import DeviceManager  # noqa: E402
import gui.app as ga  # noqa: E402

DM_SRC = (REPO / "core" / "device_manager.py").read_text(encoding="utf-8")
APP_SRC = (REPO / "gui" / "app.py").read_text(encoding="utf-8")
JS = (REPO / "gui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
DOCTOR = (REPO / "core" / "doctor.py").read_text(encoding="utf-8")

SERIAL = "emulator-5554"


def settings_get(key):
    """Read a guest setting the same way the app does (via adb)."""
    import subprocess

    out = subprocess.run(
        [str(config.ADB_PATH), "-s", SERIAL, "shell", "settings", "get", key],
        capture_output=True, text=True, timeout=30,
    )
    return (out.stdout or "").strip()


def radio_state():
    return settings_get("global airplane_mode_on")


mgr = DeviceManager()

# ------------------------------------------------------------- 1. code is gone
print("== 1. the automatic gate no longer exists ==")
check("enforce_airplane_gate is gone", not hasattr(DeviceManager, "enforce_airplane_gate"))
check("_airplane_required is gone", not hasattr(DeviceManager, "_airplane_required"))
check("_spoof_current (the gate's rule) is gone", not hasattr(DeviceManager, "_spoof_current"))
for path in list(REPO.rglob("core/*.py")) + list(REPO.rglob("gui/*.py")) + [REPO / "main.py"]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    check(f"{path.name} never calls a gate",
          "enforce_airplane_gate" not in text and "_airplane_required" not in text)
check("the launch chain does not say 'airplane gate'",
      "airplane gate" not in DM_SRC.lower(), "leftover message")
check("no source sets airplane mode on automatically",
      "set_airplane_mode(True)" not in DM_SRC and "set_airplane_mode(True)" not in APP_SRC)

# ------------------------------------------------- 2. launch leaves radios alone
print("== 2. a launch never switches airplane mode ==")
app = ga.create_app()
client = app.test_client()
r = client.post("/api/devices", json={
    "model": "Google Pixel 7a", "android_version": "14", "name": "radiodev"})
check("device created", r.get_json().get("success") is True, str(r.json)[:150])

before = radio_state()
r = client.post("/api/devices/radiodev/launch", json={"frida": False})
check("launch accepted", r.get_json().get("success") is True, str(r.json)[:150])
import time  # noqa: E402

status = {}
deadline = time.time() + 120
while time.time() < deadline:
    status = client.get("/api/devices/radiodev/status").get_json()
    if status.get("launch_state") in ("running", "error", "idle"):
        break
    time.sleep(0.3)
check("the launch finished", status.get("state") in ("running", "error"),
      json.dumps(status)[:220])
check("airplane mode was not touched by the launch",
      radio_state() in (before, "", "0"), f"before={before!r} after={radio_state()!r}")
card = next((d for d in client.get("/api/devices").get_json()
             if d["name"] == "radiodev"), {})
check("the card has no 'gated' state", card.get("airplane_gate") in (None, "off", "held-off"),
      str(card.get("airplane_gate")))
check("airplane_required is always false now", card.get("airplane_required") is False)
history = (mgr.launch_progress("radiodev") or {}).get("history", [])
check("the boot history never mentions airplane mode",
      all("airplane" not in (h.get("text") or "").lower() for h in history),
      str(history)[:220])

# ---------------------------------------- 3. failed spoof stays online, reported
print("== 3. a failed spoof is reported, not enforced ==")
live = mgr._load_device("radiodev")
live["spoof_applied"] = False
live["spoof_error"] = "Could not change props ['ro.product.model']"
mgr._save_device(live)
result = mgr.autorecover("radiodev")
check("the recovery ran", isinstance(result, dict), str(result)[:200])
check("airplane mode is still not the app's business",
      radio_state() in (before, "", "0"), radio_state())
shown = mgr.get_device("radiodev") or {}
check("an unspoofed device is still flagged for the user (never silently green)",
      shown.get("spoof_applied") is True or bool(shown.get("spoof_error"))
      or bool(shown.get("needs_restore")) or bool(shown.get("runtime_stale")),
      json.dumps({k: shown.get(k) for k in
                  ("spoof_applied", "spoof_error", "needs_restore", "runtime_stale")}))
check("the card does not claim the radios were cut",
      shown.get("airplane_gate") != "airplane", str(shown.get("airplane_gate")))
check("the radios were not switched because the spoof had failed",
      radio_state() in (before, "", "0"), radio_state())

# ------------------------------------------------- 4. the UI says the real thing
print("== 4. the dashboard reports the observed radio state only ==")
check("the 'spoof pending' airplane tag is gone",
      "Airplane (spoof pending)" not in JS)
check("the 'airplane mode is on because the spoof is not applied' banner is gone",
      "spoof not applied" not in JS)
check("the 'turn airplane off anyway' override is gone",
      "Turn airplane off anyway" not in JS)
check("the radios cell still reports Airplane / Online",
      "✈ Airplane" in JS and "'Online'" in JS)
check("the details alert for a radio error is kept",
      "Airplane mode could not be changed" in JS)

# ------------------------------------------------- 5. manual control still works
print("== 5. the manual switch still works ==")
ok, msg = mgr.set_airplane_mode("radiodev", True)
check("airplane on is applied and verified",
      ok is True and settings_get("global airplane_mode_on") == "1", msg)
card = next(d for d in client.get("/api/devices").get_json() if d["name"] == "radiodev")
check("the card shows Airplane because the user turned it on",
      card.get("airplane_mode") is True and card.get("airplane_gate") == "held-off",
      json.dumps(card)[:200])
ok, msg = mgr.set_airplane_mode("radiodev", False)
check("airplane off is applied and verified",
      ok is True and settings_get("global airplane_mode_on") == "0", msg)

r = client.post("/api/devices/radiodev/airplane", json={"auto": True})
body = r.get_json()
check("the legacy {'auto': true} call is a no-op that reports state",
      r.status_code == 200 and body.get("success") is True
      and "nothing changes it automatically" in (body.get("message") or ""),
      json.dumps(body)[:220])

try:
    mgr.set_airplane_mode("missing-device", True)
    check("an unknown device raises instead of pretending", False)
except ValueError as exc:
    check("an unknown device raises instead of pretending", "not found" in str(exc))

# ------------------------------------------------------------- 6. doctor
print("== 6. the doctor no longer judges the radios ==")
live = mgr._load_device("radiodev")
live["gpu_mode"] = "swiftshader_indirect"
live["spoof_applied"] = False
live["airplane_mode"] = False
mgr._save_device(live)
from core.doctor import check_renderer_and_radios  # noqa: E402

report = check_renderer_and_radios()
check("an unspoofed device with radios on is not a warning",
      report["severity"] in ("ok", "info"), str(report)[:220])
check("it explains that radios are never forced",
      "never forced into airplane mode" in report["detail"], str(report)[:220])
check("the host-GPU check is still there",
      "host-GPU passthrough" in DOCTOR and "GUEST_GPU_MODE" in DOCTOR)

# ------------------------------------------------- 7. docs tell the truth
print("== 7. the docs match the code ==")
README = (REPO / "README.md").read_text(encoding="utf-8")
FIXES = (REPO / "FIXES.md").read_text(encoding="utf-8")
MAIN_SRC = (REPO / "main.py").read_text(encoding="utf-8")

check("README says airplane mode is manual only",
      "Airplane mode is **manual only**" in README)
check("README documents the airplane command that actually exists",
      "def airplane(" in MAIN_SRC and "python main.py airplane" in README)
check("README no longer promises a gate",
      "spoof pending" not in README and "held **off** while the" not in README)
check("FIXES.md warns that the gate write-up is superseded",
      "Superseded" in FIXES and "Round 24" in FIXES)
check("FIXES.md lists what was removed vs what replaced it",
      "Removed / superseded" in FIXES)

# Every suite the README lists must exist, and every suite must be listed.
listed = set(re.findall(r"python (tests/\w+_test\.py)", README))
actual = {f"tests/{p.name}" for p in (REPO / "tests").glob("*_test.py")}
check("the README test list is complete", not (actual - listed), str(sorted(actual - listed)))
check("the README test list has no ghosts", not (listed - actual), str(sorted(listed - actual)))
check("the README names the right suite count",
      f"All {len(actual)} suites" in README, f"expected {len(actual)}")

# Every CLI command the README shows must exist on the CLI.
help_out = subprocess.run(
    [sys.executable, str(REPO / "main.py"), "--help"],
    capture_output=True, text=True, timeout=180,
).stdout
for cmd in sorted(set(re.findall(r"python main\.py ([a-z][a-z-]+)", README))):
    check(f"documented command exists: {cmd}", cmd in help_out, cmd)

if failures:
    print(f"\n{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("\nAll airplane-mode checks passed (automatic gate removed).")
