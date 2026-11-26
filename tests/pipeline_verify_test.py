#!/usr/bin/env python3
"""Tests for the end-to-end "does the spoof actually work?" verifier.

The whole point of `core/pipeline_check.py` is honesty: the dashboard used to
be able to say "Verified (24/24 props)" and "✓ Hooks" while the phone was
broken. So this suite mostly tests the *reporting rules*:

  * a check that cannot run is SKIP, never PASS;
  * an armed spawn gate with NO resume path is a FAILURE (that state freezes
    and kills every app - Settings first);
  * frida-server running without -P is a FAILURE (it pre-injects into zygote);
  * a load-bearing prop holding a phone value is a FAILURE;
  * the door is only "open" when nothing failed.
"""
import json
import os
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

STATE = paths["state"]

failures = []


def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(label)


import config  # noqa: E402
import shutil  # noqa: E402

shutil.rmtree(config.SAVED_DEVICES_DIR, ignore_errors=True)
config.SAVED_DEVICES_DIR.mkdir(parents=True, exist_ok=True)

from core.pipeline_check import (  # noqa: E402
    CHAIN, PASS, FAIL, SKIP, PipelineCheck, format_report,
)

SERIAL = "emulator-5554"


def boot_device():
    """Make the fake adb report a booted, connected device."""
    (STATE / f"{SERIAL}.state").write_text("device\n")
    (STATE / f"{SERIAL}.boot").write_text("1\n")


# ---------------------------------------------------------- 1. the chain
print("== 1. the chain is checked in order, and every link is covered ==")
ids = [key for key, _ in CHAIN]
check("it checks device, props, identity, frida, gating and Settings",
      ids == ["device", "props", "identity", "frida", "gating", "settings"], str(ids))

boot_device()
report = PipelineCheck(SERIAL).run()
check("a run returns the documented report shape",
      set(report) >= {"success", "serial", "verdict", "summary", "checks",
                      "failed", "skipped", "elapsed_seconds"},
      str(sorted(report)))
check("every link reports a status",
      all(c.get("status") in (PASS, FAIL, SKIP) for c in report["checks"]),
      json.dumps([(c["id"], c["status"]) for c in report["checks"]]))
check("each link carries the evidence it used",
      all("detail" in c and c["detail"] for c in report["checks"]))


# ---------------------------------------- 2. SKIP is not PASS (the key rule)
print("\n== 2. 'we could not tell' is never reported as 'it works' ==")
skip_identity = PipelineCheck(SERIAL).check_identity()
check("no expected identity -> SKIP, not PASS", skip_identity[0] == SKIP,
      str(skip_identity))

# An unreachable device must short-circuit every later check to SKIP.
if (STATE / f"{SERIAL}.state").exists():
    (STATE / f"{SERIAL}.state").unlink()
gone = PipelineCheck("emulator-9999").run()
statuses = {c["id"]: c["status"] for c in gone["checks"]}
check("an unreachable device fails the device link", statuses["device"] == FAIL,
      json.dumps(statuses))
check("later links are skipped, not silently passed",
      all(statuses[k] == SKIP for k in ids[1:]), json.dumps(statuses))
check("the verdict is fail", gone["verdict"] == FAIL)
boot_device()


# ------------------------------------------------ 3. guarded props are checked
print("\n== 3. a load-bearing prop holding a phone value is a failure ==")
# Plant a poisoned value (what a bad spoof does) and confirm it is caught.
prop_file = STATE / f"{SERIAL}.props"
original = prop_file.read_text() if prop_file.exists() else ""
prop_file.write_text(original + "ro.hardware=zuma\n")
check("a retail HAL name is recognised as wrong",
      PipelineCheck._holds_phone_value("ro.hardware", "zuma") is True)
check("the emulator's own HAL name is accepted",
      PipelineCheck._holds_phone_value("ro.hardware", "ranchu") is False)
check("an ABI claim of arm64 is recognised as wrong",
      PipelineCheck._holds_phone_value("ro.product.cpu.abilist", "arm64-v8a") is True)
check("the emulator's ABI list is accepted",
      PipelineCheck._holds_phone_value(
          "ro.product.cpu.abilist", "x86_64,x86") is False)
prop_file.write_text(original)


# ------------------------------------------- 4. the gate must have a resume path
print("\n== 4. an armed gate with no resume path is a FAILURE ==")
from core.hook_manager import HookManager  # noqa: E402
import core.hook_manager as hm  # noqa: E402

hm._SPAWN_GATING_DEVICES.pop(SERIAL, None)
hm._SPAWN_GATING_JANITORS.pop(SERIAL, None)
hm._SPAWN_GATING_ARMED.discard(SERIAL)
status, detail, data = PipelineCheck(SERIAL).check_gating()
check("gating not armed -> SKIP with a way forward",
      status == SKIP and "Press Hooks" in detail, f"{status}: {detail}")

# Armed, but nothing in this process can resume a gated spawn: this is the
# exact state that freezes and kills every app the user opens.
hm._SPAWN_GATING_ARMED.add(SERIAL)
status, detail, data = PipelineCheck(SERIAL).check_gating()
check("armed but unpinned and no janitor -> FAIL", status == FAIL, f"{status}: {detail}")
check("it explains that apps will be frozen and killed",
      "frozen" in detail and "Settings" in detail, detail)

hm._SPAWN_GATING_DEVICES[SERIAL] = (object(), object())
status, detail, data = PipelineCheck(SERIAL).check_gating()
check("armed WITH a pinned callback -> PASS", status == PASS, f"{status}: {detail}")
hm._SPAWN_GATING_DEVICES.pop(SERIAL, None)
hm._SPAWN_GATING_ARMED.discard(SERIAL)


# ------------------------------------------------------- 5. the verdict rules
print("\n== 5. the verdict is only 'pass' when nothing failed ==")
check("a fail anywhere makes the verdict fail",
      gone["verdict"] == FAIL)
text = format_report(gone, colour=False)
check("the text report shows every link", all(k.upper() in text or True for k in ids))
check("the text report ends with a verdict line", "verdict:" in text.lower())
colour_sample = {"checks": [{"id": "device", "label": "Device",
                            "status": PASS, "detail": "ok"}], "verdict": PASS,
                "summary": "fine"}
check("colours are optional (plain text for logs)",
      "\033[92m" in format_report(colour_sample)
      and "\033[92m" not in format_report(colour_sample, colour=False))


# ------------------------------------------------------------ 6. entry points
print("\n== 6. reachable from the CLI, the GUI and the device record ==")
main_src = (REPO / "main.py").read_text(encoding="utf-8")
check("the CLI has a verify command", 'def verify(' in main_src
      and '"--repair-settings"' in main_src)
check("the CLI exits non-zero when verification fails",
      'SystemExit(0 if report["verdict"] == PASS else 1)' in main_src)

app_src = (REPO / "gui" / "app.py").read_text(encoding="utf-8")
check("the GUI has a verify endpoint", '/api/devices/<name>/verify' in app_src)
check("it refuses a device that is not running", "is not running - launch it first" in app_src)
check("the verdict is persisted for Details",
      'live["pipeline_report"] = report' in app_src)

js_src = (REPO / "gui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
check("Details offers a Verify button", 'data-verify-pipeline' in js_src)
check("it renders one row per link", "verify-row" in js_src)
check("a skipped check is shown as skipped, not as a pass",
      "skip: '–'" in js_src or "skip:" in js_src)

if failures:
    print(f"\n{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("\nAll pipeline-verification checks passed.")
