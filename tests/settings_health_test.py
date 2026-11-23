#!/usr/bin/env python3
"""Regression test: "Settings keeps stopping" is diagnosed, not guessed.

The user's report was a screenshot: the Settings app opens and is killed
instantly, on a device where spoofing reported **24/24 props verified** and
Frida said **"Hooks failed"**. Two conclusions follow, and this test pins both:

1. Verified props are not the same as a usable device. Something else is
   killing Settings, and the app has to find out *what* instead of assuming.
2. Frida spawn gating is a global, device-side latch. If it is armed and no
   handler is left to resume processes, every app the user opens is held at
   exec and then reaped - Settings first, because that is the app people check
   the spoof with. A failed hook attempt must never leave it armed.

Covers:
  * the classifier maps each known crash signature to the right cause,
  * a healthy device is reported healthy without being restarted,
  * a Frida-frozen device is detected, repaired and re-verified,
  * a prop-poisoned device is detected, repaired and re-verified,
  * an unknown crash is reported honestly (log surfaced, no false success),
  * spawn gating is rolled back when hooking fails,
  * a stale gate is cleared at launch,
  * the API endpoint and CLI both expose the check.
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
STATE = paths["state"]
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

from core.settings_doctor import CAUSES, SettingsHealth  # noqa: E402

# --------------------------------------------------------------- 1. classify
print("== 1. every known crash signature maps to a cause ==")
classify = SettingsHealth.classify

CASES = [
    (
        "frida",
        "FATAL EXCEPTION: main\nProcess: com.android.settings\n"
        "Fatal signal 11 (SIGSEGV) at 0x0\nbacktrace:\n#00 libfrida-agent.so",
        {},
        "frida_injection",
    ),
    (
        "native bridge / arm lib",
        "java.lang.UnsatisfiedLinkError: dlopen failed: "
        "library \"libEGL_adreno.so\" not found",
        {},
        "guard_props",
    ),
    (
        "hal module",
        "hw_get_module: module power.zuma.so not found",
        {},
        "guard_props",
    ),
    (
        "bad parcel",
        "android.os.BadParcelableException: ClassNotFoundException when "
        "unmarshalling",
        {},
        "settings_data",
    ),
    (
        "sqlite",
        "android.database.sqlite.SQLiteException: no such table: secure",
        {},
        "settings_data",
    ),
]
for label, log, kwargs, expected in CASES:
    got = classify(log, **kwargs)
    check(f"'{label}' -> {expected}", got == expected, got)

check("an explicit attached session counts as Frida even with no log",
      classify("", frida_attached=True) == "frida_injection")
check("an armed spawn gate counts as Frida even with no log",
      classify("", spawn_gating=True) == "frida_injection")
check("a phone-valued system prop is enough to blame props",
      classify("", spoofed_qemu={"ro.hardware": "zuma"}) == "guard_props")
check("merely having frida-server running is NOT evidence",
      classify("", frida_server_running=True) != "frida_injection")
check("an unrecognised crash stays 'unknown' (never a false cause)",
      classify("Something completely unexpected happened") == "unknown")
check("every cause has a human label", set(CAUSES) >= {
    "frida_injection", "guard_props", "settings_data", "not_running", "unknown"})

# ------------------------------------------------------- 2. a healthy device
print("\n== 2. a healthy device is reported healthy ==")
serial = "emulator-5554"
(STATE / f"{serial}.state").write_text("device\n")
for marker in ("settings-crash", "settings-crash-reason", "crash-buffer", "settings-pid"):
    (STATE / marker).unlink(missing_ok=True)

health = SettingsHealth(serial)
report = health.diagnose()
check("healthy device is detected", report["healthy"] is True, json.dumps(report)[:300])
# Leaving that run behind would make later phases read a stale "success".
for marker in ("settings-pid", "crash-buffer"):
    (STATE / marker).unlink(missing_ok=True)
check("no cause is invented for a healthy device", report["cause"] is None)
check("it explains what it observed",
      any("survived a launch" in s or "already running" in s for s in report["steps"]),
      str(report["steps"]))
check("a healthy check does not repair anything", report["repaired"] is False)

# ------------------------------------- 3. the reported case: apps are frozen
print("\n== 3. the screenshot case: a Frida-frozen Settings is repaired ==")
# Settings appears and dies, and the crash names Frida.
(STATE / "settings-crash").touch()
(STATE / "settings-crash-reason").write_text(
    "FATAL EXCEPTION: main\nProcess: com.android.settings\n"
    "Fatal signal 11 (SIGSEGV) in libfrida-agent.so\n"
)

# Section 2 left a healthy Settings running; "keeps stopping" starts from a
# device where nothing of it survives, so clear that first.
for marker in ("settings-pid", "settings-repaired", "crash-buffer"):
    (STATE / marker).unlink(missing_ok=True)
# A Frida crash only makes sense with frida-server running; the fake SDK gates
# that on $STATE/frida-running.
(STATE / "frida-running").touch()
# Diagnose-only first, so "it saw the crash" is asserted before any repair can
# make the device healthy.
diag = health.diagnose(repair=False)
check("the crash is detected (not reported healthy)", diag["healthy"] is False,
      json.dumps(diag["steps"])[:300])
report = health.diagnose()
check("the cause is Frida injection",
      report["cause"] == "frida_injection", str(report["cause"]))
check("the cause is explained in words",
      "Frida" in (report["cause_label"] or ""), str(report["cause_label"]))
check("the repair was attempted", report["repaired"] is True, str(report["repair_detail"]))
check("the repair mentions detaching Frida",
      "Frida" in (report["repair_detail"] or ""), str(report["repair_detail"]))
check("Settings opens again after the repair", report["healthy"] is True,
      json.dumps(report["steps"])[-400:])
check("the raw crash log is kept for the user",
      "settings" in (report["crash_log"] or "").lower(), str(report["crash_log"])[:200])

# ---------------------------------------- 4. prop-poisoned device gets fixed
print("\n== 4. a device with a phone-valued system prop is repaired ==")
(STATE / "settings-crash").touch()
(STATE / "settings-crash-reason").write_text(
    "Fatal signal 6 (SIGABRT)\nProcess: com.android.settings\n"
    "hw_get_module: module not found for ro.hardware=zuma\n"
)
# Seed the poison the previous release left behind.
(STATE / f"{serial}.resetprops").write_text("ro.hardware=zuma\n", encoding="utf-8")
(STATE / "resetprop-installed").write_text("450000\n")
(STATE / "root-allowed").write_text("1\n")

report = health.diagnose()
check("the cause is the poisoned prop", report["cause"] == "guard_props", str(report["cause"]))
check("the offending prop is named in the report",
      "ro.hardware" in (report.get("guarded_props") or {}),
      str(report.get("guarded_props")))
check("the prop is reported as repaired", report["repaired"] is True)
check("Settings works after the prop repair", report["healthy"] is True,
      json.dumps(report["steps"])[-400:])
probe = __import__("subprocess").run(
    [config.ADB_PATH, "-s", serial, "shell", "getprop", "ro.hardware"],
    capture_output=True, text=True, timeout=20,
).stdout.strip()
check("ro.hardware is back to the emulator HAL", probe == "ranchu", probe)

# ------------------------------------------------ 5. unknown crash: honesty
print("\n== 5. an unrecognised crash is reported, not papered over ==")
(STATE / "settings-crash").touch()
(STATE / "settings-crash-reason").write_text("Something nobody has seen before\n")
(STATE / f"{serial}.resetprops").write_text("", encoding="utf-8")

report = health.diagnose()
check("the cause is reported as unknown", report["cause"] == "unknown", str(report["cause"]))
check("it does NOT claim the device is healthy", report["healthy"] is False)
check("the raw log is surfaced for a bug report",
      "nobody has seen" in (report["crash_log"] or ""), str(report["crash_log"])[:200])

# Diagnose-only mode must not touch anything.
(STATE / "settings-crash-reason").write_text(
    "FATAL EXCEPTION: main\nProcess: com.android.settings\nFatal signal 11 in libfrida-agent.so\n"
)
report = health.diagnose(repair=False)
check("repair=False only diagnoses", report["repaired"] is False)
check("diagnose-only still identifies the cause",
      report["cause"] == "frida_injection", str(report["cause"]))

# ------------------------------------- 6. a failed hook attempt cleans up
print("\n== 6. a failed hook attempt cannot leave apps frozen ==")
from core.hook_manager import HookManager  # noqa: E402

hook_src = (REPO / "core" / "hook_manager.py").read_text(encoding="utf-8")
apply_block = hook_src.split("def apply_all_protections", 1)[1].split("def _abandon", 1)[0]
check("apply_all_protections rolls spawn gating back on failure",
      "_abandon_spawn_gating()" in apply_block)
check("the rollback exists and stops frida-server as a last resort",
      "def _abandon_spawn_gating" in hook_src
      and "def _restart_frida_server_for_rollback" in hook_src)
check("spawn gating can now be turned off at all",
      "def disable_spawn_gating" in hook_src)
check("the rollback resumes processes the gate is still holding",
      "enumerate_pending_spawn" in hook_src)

# The manager must report failure *and* leave gating off.
class _FakeManager(HookManager):
    def __init__(self):
        super().__init__("emulator-5554")
        self.rolled_back = False

    def wait_for_device(self, timeout=15):
        return True

    def start_frida_server(self):
        return True

    def inject_system_wide(self, identity):
        self.last_error = "simulated Frida failure"
        return False

    def _abandon_spawn_gating(self):
        self.rolled_back = True
        self._spawn_gating_enabled = False


mgr = _FakeManager()
mgr._spawn_gating_enabled = True
ok = mgr.apply_all_protections({"model": "Pixel 8 Pro"})
check("the GUI's hook path goes through the rollback (not around it)",
      "apply_all_protections(device[\"identity\"])" in
      (REPO / "core" / "device_manager.py").read_text(encoding="utf-8"))
check("a failed hook attempt returns False", ok is False)
check("a failed hook attempt disarms the gate", mgr.rolled_back is True)
check("no stale 'enabled' flag survives the failure", mgr._spawn_gating_enabled is False)

# --------------------------------- 7. a stale gate is cleared at launch time
print("\n== 7. launch clears a gate left armed by an earlier run ==")
dm_src = (REPO / "core" / "device_manager.py").read_text(encoding="utf-8")
check("launch clears stale spawn gating before spoofing",
      "_clear_stale_spawn_gating(name, serial)" in dm_src)
check("the stale-gate clear happens after root and before spoofing",
      dm_src.index("_clear_stale_spawn_gating(name, serial)")
      < dm_src.index("self._apply_hardware_spoof(device, serial)"))
check("it explains the symptom it prevents",
      "Settings keeps stopping" in dm_src or "app the user just opened" in dm_src)

# ------------------------------------------- 8. API + CLI expose the check
print("\n== 8. the check is reachable from the GUI and the CLI ==")
app_src = (REPO / "gui" / "app.py").read_text(encoding="utf-8")
check("the GUI has a settings-health endpoint",
      '/api/devices/<name>/settings-health' in app_src)
check("the endpoint can diagnose without repairing",
      "repair" in app_src.split("settings-health", 1)[1][:2000])
check("the endpoint refuses a device that is not running",
      "is not running - launch it first" in app_src)
check("the GUI persists the outcome for Details",
      'live["settings_health"] = report' in app_src)

js_src = (REPO / "gui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
check("Details offers the check as a button", 'data-check-settings' in js_src)
check("the hook-failure warning warns that it can freeze apps, not only that it broke",
      "spawn gate armed" in js_src or "freezes every app" in js_src)
check("the hook-failure warning points at the Settings check",
      "data-check-settings" in js_src.split("Frida Hooks Warning", 1)[-1][:900])

main_src = (REPO / "main.py").read_text(encoding="utf-8")
check("the CLI has a check-settings command", 'def check_settings(' in main_src)
check("the CLI exits non-zero when Settings still fails",
      "raise SystemExit(1)" in main_src.split("def check_settings", 1)[1])
check("the CLI prints the guarded props it found",
      "guarded_props" in main_src.split("def check_settings", 1)[1])

if failures:
    print(f"\n{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("\nAll Settings-health checks passed.")
