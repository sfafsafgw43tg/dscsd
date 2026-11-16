#!/usr/bin/env python3
"""Regression test: spoofing + Frida hooks must survive a restart.

The reported bug: "when I apply spoofing and hooks it doesn't save after
restart". The property spoof is written into the running kernel's
``/dev/__properties__`` (resetprop-rs) and frida-server is just a process,
so *nothing* about them is on disk:

- an emulator reboot wiped every spoofed ``ro.*`` value and killed
  frida-server,
- restarting the app forgot what had been applied *and* had no record that
  the user wanted it applied at all,
- the saved ``spoof_report``/``hooks_applied`` flags kept claiming the
  device was protected, so the dashboard never even offered a re-apply.

This suite locks in the fix:

1. applying spoofing/hooks records the *intent* (``auto_spoof`` /
   ``auto_hooks``) and the boot id the values belong to;
2. after a device reboot the state is reported as stale (instead of
   pretending to be active) and ``needs_restore`` is set;
3. ``restore_device`` / ``restore_all_pending`` re-apply everything and the
   values really come back (verified with ``getprop``);
4. launching again re-applies automatically (intent survives stop/launch);
5. opting out (``set_restore_intent(spoof=False)``) is honoured;
6. the GUI endpoints (``/restore``, ``/restore-intent``,
   ``/restore-pending``) and the CLI ``restore`` command work.
"""
import json
import os
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

from core.device_manager import DeviceManager  # noqa: E402

# Models the google_apis userdebug image (adbd allows root).
(STATE / "root-allowed").write_text("1\n")

mgr = DeviceManager()
device = mgr.create_device("Samsung Galaxy S24", android_version="14", name="persist_dev")
launched = mgr.launch_device("persist_dev", enable_frida=False, wait_for_boot=True)
SERIAL = launched["serial"]


def gp(key):
    result = subprocess.run(
        [config.ADB_PATH, "-s", SERIAL, "shell", "getprop", key],
        capture_output=True, text=True, timeout=20,
    )
    return result.stdout.strip()


def device_json():
    return json.loads((config.SAVED_DEVICES_DIR / "persist_dev.json").read_text(encoding="utf-8"))


def reboot_device():
    """Reboot the guest the way the emulator would: RAM props are wiped."""
    subprocess.run([config.ADB_PATH, "-s", SERIAL, "reboot"],
                   capture_output=True, text=True, timeout=30)
    mgr._boot_id_cache.clear()


# ------------------------------------------------------------------ 1. intent is recorded
print("== 1. applying spoofing records the intent to keep it ==")
check("model spoofed at launch", gp("ro.product.model") == device["identity"]["model"],
      gp("ro.product.model"))
saved = device_json()
check("spoof_applied recorded on the device json", saved.get("spoof_applied") is True)
check("auto_spoof intent recorded (default: keep it)",
      saved.get("auto_spoof") is True, str(saved.get("auto_spoof")))
check("boot id recorded with the spoof", bool(saved.get("runtime_boot_id")),
      str(saved.get("runtime_boot_id")))
check("spoof_applied_at timestamp recorded", bool(saved.get("spoof_applied_at")))

# ------------------------------------------------------------------ 2. reboot -> honest stale state
print("== 2. after a device reboot the state is reported stale, not 'active' ==")
reboot_device()
time.sleep(0.5)
live = mgr.get_device("persist_dev")
check("device still reported running (emulator itself did not die)", live.get("running") is True)
check("runtime_stale flagged after the reboot", live.get("runtime_stale") is True,
      f"boot recorded={live.get('runtime_boot_id')} stale={live.get('runtime_stale')}")
check("stale reason is human readable",
      "reboot" in (live.get("runtime_stale_reason") or "").lower(),
      str(live.get("runtime_stale_reason")))

# ------------------------------------------------------------------ 3. restore re-applies
print("== 3. restore_device re-applies spoofing (values verified) ==")
result = mgr.restore_device("persist_dev")
check("restore reports success", result.get("success") is True, json.dumps(result))
check("restore reports spoofing restored", result.get("spoofed") is True, json.dumps(result))
check("model is spoofed again after restore",
      gp("ro.product.model") == device["identity"]["model"], gp("ro.product.model"))
check("serial is spoofed again after restore",
      gp("ro.serialno") == device["identity"]["serial"], gp("ro.serialno"))
restored = mgr.get_device("persist_dev")
check("stale flag cleared after restore", restored.get("runtime_stale") is False,
      str(restored.get("runtime_stale_reason")))
check("spoof_applied true again", restored.get("spoof_applied") is True)
check("boot id re-pinned to the current boot",
      restored.get("runtime_boot_id") == device_json().get("runtime_boot_id"))

# ------------------------------------------------------------------ 4. stop/launch keeps it
print("== 4. a normal stop + launch re-applies automatically ==")
mgr.stop_device("persist_dev")
stopped = device_json()
check("stop clears the live flag", stopped.get("spoof_applied") is False,
      str(stopped.get("spoof_applied")))
check("stop keeps the intent", stopped.get("auto_spoof") is True,
      str(stopped.get("auto_spoof")))
relaunched = mgr.launch_device("persist_dev", enable_frida=False, wait_for_boot=True)
SERIAL = relaunched["serial"]
again = mgr.get_device("persist_dev")
check("relaunch spoofs again", again.get("spoof_applied") is True)
check("relaunch values match the identity",
      gp("ro.product.model") == device["identity"]["model"], gp("ro.product.model"))

# ------------------------------------------------------------------ 5. restore_all_pending sweep
print("== 5. restore_all_pending finds and fixes stale devices ==")
reboot_device()
time.sleep(0.5)
pending = [d for d in mgr.list_devices() if d.get("runtime_stale")]
check("list_devices surfaces the stale device", len(pending) == 1,
      str([d.get("name") for d in mgr.list_devices()]))
results = mgr.restore_all_pending()
check("sweep restored one device", len(results) == 1 and results[0].get("success") is True,
      json.dumps(results))
check("values back after the sweep",
      gp("ro.product.model") == device["identity"]["model"], gp("ro.product.model"))
check("second sweep is a no-op", mgr.restore_all_pending() == [])

# ------------------------------------------------------------------ 6. opt-out is honoured
print("== 6. opting out stops the automatic restore ==")
mgr.set_restore_intent("persist_dev", spoof=False)
opt_out = device_json()
check("auto_spoof switched off", opt_out.get("auto_spoof") is False)
check("live flag cleared with the opt-out", opt_out.get("spoof_applied") is False)
check("opt-out survives on disk", device_json().get("auto_spoof") is False)
reboot_device()
time.sleep(0.5)
check("no stale-flag nagging once the user opted out",
      mgr.get_device("persist_dev").get("runtime_stale") is False)
check("sweep does not touch an opted-out device", mgr.restore_all_pending() == [])
opt_out_result = mgr.restore_device("persist_dev")
check("explicit restore explains there is nothing to restore",
      opt_out_result.get("success") is False
      and "nothing to restore" in opt_out_result.get("message", "").lower(),
      json.dumps(opt_out_result))
mgr.set_restore_intent("persist_dev", spoof=True)

# ------------------------------------------------------------------ 7. hooks intent + restore
print("== 7. Frida hook intent is persisted and restorable ==")
mgr.mark_runtime_applied("persist_dev", serial=SERIAL, hooks=True)
hooked = device_json()
check("hooks_applied persisted", hooked.get("hooks_applied") is True)
check("auto_hooks intent persisted", hooked.get("auto_hooks") is True)
check("hook timestamp persisted", bool(hooked.get("hooks_applied_at")))
check("stop clears the live hook flag but keeps the intent", True)
mgr.stop_device("persist_dev")
after_stop = device_json()
check("stop -> hooks_applied False", after_stop.get("hooks_applied") is False)
check("stop -> auto_hooks kept", after_stop.get("auto_hooks") is True)

# ------------------------------------------------------------------ 8. GUI endpoints
print("== 8. GUI endpoints ==")
from gui.app import create_app  # noqa: E402

mgr2 = DeviceManager()
started = mgr2.launch_device("persist_dev", enable_frida=False, wait_for_boot=True)
SERIAL = started["serial"]
client = create_app().test_client()

r_intent = client.post("/api/devices/persist_dev/restore-intent", json={"hooks": True})
check("/restore-intent returns 200 + success",
      r_intent.status_code == 200 and r_intent.get_json().get("success") is True,
      str(r_intent.get_json()))
check("/restore-intent persists auto_hooks",
      device_json().get("auto_hooks") is True, str(device_json().get("auto_hooks")))

r_off = client.post("/api/devices/persist_dev/restore-intent", json={"hooks": False})
check("/restore-intent can switch hooks back off",
      r_off.status_code == 200 and device_json().get("auto_hooks") is False)
check("/restore-intent reports the effective settings",
      r_off.get_json().get("wants_hooks") is False
      and r_off.get_json().get("wants_spoof") is True,
      json.dumps(r_off.get_json()))
check("/restore-intent message names what is on/off",
      "Frida hooks off" in (r_off.get_json().get("message") or ""),
      json.dumps(r_off.get_json()))
client.post("/api/devices/persist_dev/restore-intent", json={"hooks": False, "spoof": True})

reboot_device()
time.sleep(0.5)
status = client.get("/api/devices/persist_dev/status").get_json()
check("/status exposes runtime_stale", status.get("runtime_stale") is True, json.dumps(status)[:300])
check("/status exposes the restore intent fields",
      "auto_spoof" in status and "needs_restore" in status, json.dumps(status)[:300])

r_restore = client.post("/api/devices/persist_dev/restore")
body = r_restore.get_json()
check("/restore returns success", r_restore.status_code == 200 and body.get("success") is True,
      json.dumps(body)[:300])
check("/restore re-applies the values",
      gp("ro.product.model") == device["identity"]["model"], gp("ro.product.model"))

r_all = client.post("/api/restore-pending")
check("/restore-pending returns 200", r_all.status_code == 200 and r_all.get_json().get("success") is True,
      json.dumps(r_all.get_json())[:300])
check("/restore-pending reports how many it restored",
      "restored" in r_all.get_json(), json.dumps(r_all.get_json())[:200])
r_404 = client.post("/api/devices/nope/restore")
check("/restore 404s for an unknown device", r_404.status_code == 404)

# ------------------------------------------------------------------ 9. static frontend wiring
print("== 9. dashboard wiring ==")
js = (REPO / "gui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
check("app.js calls the restore endpoint",
      "/restore`" in js or "/restore'" in js or "/restore" in js)
check("app.js has a restore action button", "data-action=\"restore\"" in js or "'restore'" in js)
check("app.js shows the lost-on-restart state", "runtime_stale" in js)
check("app.js has the keep-applied toggles", "setRestoreIntent" in js)
check("app.js listens for background restores", "restore_finished" in js)
check("node --check passes",
      subprocess.run(["node", "--check", str(REPO / "gui" / "static" / "js" / "app.js")],
                     capture_output=True).returncode == 0)

# ------------------------------------------------------------------ 10. CLI
print("== 10. CLI commands ==")
help_out = subprocess.run(
    [sys.executable, str(REPO / "main.py"), "--help"],
    capture_output=True, text=True, timeout=120,
)
check("CLI advertises the restore command", "restore" in help_out.stdout, help_out.stdout[-400:])
check("CLI advertises keep-applied", "keep-applied" in help_out.stdout, help_out.stdout[-400:])

try:
    mgr2.stop_device("persist_dev")
except Exception:
    pass

if failures:
    print(f"\n{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("\nAll restart-persistence checks passed.")
