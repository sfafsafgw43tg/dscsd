#!/usr/bin/env python3
"""Deleting a device must delete its AVD - wherever the AVD actually lives.

The reported bug (screenshot of ``C:\\Users\\user\\.android\\avd`` still full of
``antidetect_google_*.avd`` folders after deleting every device):

    FIX AFTER DELETION THE AVD ANDROID DOES NOT GET DELETED IN
    C:\\Users\\user\\.android\\avd

Root cause: ``delete_device()`` removed ``config.AVD_HOME / <avd>.avd`` and
nothing else, and ``config.AVD_HOME`` came from ``data/settings.json``
(``avd_home``) or the ``ANDROID_AVD_HOME`` env var. A saved path that no longer
matches the machine - written by another install, left over after the SDK was
moved, or simply stale - pointed at a folder that held no AVDs, so the real
``~/.android/avd/<avd>.avd`` was never touched while the app happily reported
``success`` and dropped the device record. The AVD was orphaned, and
``emulator -list-avds`` kept listing a phone the app no longer knew about.

Pin the fix:

1. a stale saved ``avd_home`` is detected and corrected instead of trusted;
2. delete removes the AVD from **every** AVD home it is present in;
3. a delete that cannot remove the AVD raises instead of claiming success
   (CLI exits 1, API returns ``success: false`` with a 400);
4. the device record is only removed together with the AVD;
5. the CLI prints the AVD folder it removed and the AVD home it used;
6. nothing else about deletion changes (unknown device still raises).
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

failures = []


def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(label)


import config  # noqa: E402
from core.device_manager import DeviceManager  # noqa: E402

DEVICES_DIR = config.SAVED_DEVICES_DIR
NATIVE = Path(TMP / "home" / ".android" / "avd")
STALE = TMP / "stale_avd_home"


def make_avd(home, name):
    """Write the two files an AVD actually consists of."""
    avd = Path(home) / f"{name}.avd"
    avd.mkdir(parents=True, exist_ok=True)
    (avd / "config.ini").write_text("hw.cpu.ncore=4\n", encoding="utf-8")
    (avd / "userdata-qemu.img").write_bytes(b"\0" * 64)
    (Path(home) / f"{name}.ini").write_text(
        f"path={avd}\npath.rel=avd/{name}.avd\n", encoding="utf-8")
    return avd


def avd_exists(home, name):
    home = Path(home)
    return (home / f"{name}.avd").exists() or (home / f"{name}.ini").exists()


def save_device(name, avd_name):
    (DEVICES_DIR / f"{name}.json").write_text(
        json.dumps({"name": name, "avd_name": avd_name, "model": "Samsung Galaxy S24",
                    "android_version": "14"}), encoding="utf-8")


for stale in list(DEVICES_DIR.glob("*.json")):
    stale.unlink()
STALE.mkdir(parents=True, exist_ok=True)
NATIVE.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------- 1. stale setting corrected
print("== 1. a saved avd_home that holds no AVDs is not trusted ==")
os.environ.pop("ANDROID_AVD_HOME", None)
real_avd = make_avd(NATIVE, "antidetect_google_9e571d")
config.save_settings({"avd_home": str(STALE)})
resolved = config._resolve_avd_home()
check("stale avd_home is ignored in favour of where the AVDs are",
      resolved == NATIVE, str(resolved))
check("the setting is corrected, not left wrong",
      config.load_settings().get("avd_home") == str(NATIVE),
      str(config.load_settings().get("avd_home")))
check("ANDROID_AVD_HOME still wins over everything", (
    os.environ.__setitem__("ANDROID_AVD_HOME", str(STALE))
    or config._resolve_avd_home() == STALE),
    "env var was second-guessed")
os.environ.pop("ANDROID_AVD_HOME", None)


# ------------------------------------------- 2. delete removes the real AVD
print("== 2. delete removes the AVD from ~/.android/avd ==")
MGR = DeviceManager()
MGR.devices_dir = DEVICES_DIR
MGR._running_processes.clear()
save_device("ghost", "antidetect_google_9e571d")
check("fixture AVD exists before delete", real_avd.exists(), str(real_avd))
ok = MGR.delete_device("ghost")
check("delete returned True", ok is True)
check("the .avd folder is gone", not (NATIVE / "antidetect_google_9e571d.avd").exists())
check("the .ini is gone", not (NATIVE / "antidetect_google_9e571d.ini").exists())
check("no AVD files at all for that name", not avd_exists(NATIVE, "antidetect_google_9e571d"))
check("the device record is gone too",
      not (DEVICES_DIR / "ghost.json").exists())


# --------------------------------- 3. several AVD homes are all cleaned up
print("== 3. a copy in another AVD home is removed as well ==")
save_device("dup", "antidetect_google_9e571d")
make_avd(NATIVE, "antidetect_google_9e571d")
make_avd(STALE, "antidetect_google_9e571d")
os.environ["ANDROID_AVD_HOME"] = str(STALE)      # pretend the app points here
import importlib  # noqa: E402

config = importlib.reload(config)                # AVD_HOME now == STALE
MGR.delete_device("dup")
check("removed from the configured home", not avd_exists(STALE, "antidetect_google_9e571d"))
check("removed from ~/.android/avd as well",
      not avd_exists(NATIVE, "antidetect_google_9e571d"),
      "a duplicate AVD survived in another home")
os.environ.pop("ANDROID_AVD_HOME", None)
config = importlib.reload(config)


# ------------------------------- 4. a failure is reported, never hidden
print("== 4. a delete that cannot remove the AVD raises ==")
MGR = DeviceManager()
MGR.devices_dir = DEVICES_DIR
save_device("locked", "antidetect_locked_1")
make_avd(NATIVE, "antidetect_locked_1")
real_rmtree = __import__("shutil").rmtree


def refuse(path, *a, **kw):
    raise OSError(13, "Access is denied", str(path))


__import__("shutil").rmtree = refuse
try:
    raised = None
    try:
        MGR.delete_device("locked")
    except Exception as exc:            # noqa: BLE001 - we want any failure type
        raised = exc
finally:
    __import__("shutil").rmtree = real_rmtree
check("a locked AVD raises instead of reporting success", raised is not None,
      "delete_device returned success while the AVD stayed on disk")
check("the error names the path",
      raised is not None and "antidetect_locked_1.avd" in str(raised), str(raised))
check("the error suggests closing the emulator",
      raised is not None and "emulator" in str(raised).lower(), str(raised))
MGR.delete_device("locked")     # cleaning up for the next sections


# --------------------------------------------- 5. the CLI tells the truth
print("== 5. main.py delete reports the AVD it removed ==")
from click.testing import CliRunner  # noqa: E402

import main as cli_main  # noqa: E402

save_device("clidev", "antidetect_cli_9")
make_avd(NATIVE, "antidetect_cli_9")
result = CliRunner().invoke(cli_main.cli, ["delete", "clidev", "--yes"])
check("delete exits 0", result.exit_code == 0, result.output)
check("it prints the AVD folder", "antidetect_cli_9.avd" in result.output, result.output)
check("it prints the AVD home", "AVD home" in result.output, result.output)
check("the AVD really is gone", not avd_exists(NATIVE, "antidetect_cli_9"))
check("it says it removed it", "removed" in result.output, result.output)

save_device("missingavd", "antidetect_absent_1")
result = CliRunner().invoke(cli_main.cli, ["delete", "missingavd", "--yes"])
check("deleting a device whose AVD is already gone still succeeds",
      result.exit_code == 0, result.output)

result = CliRunner().invoke(cli_main.cli, ["delete", "nosuchdevice", "--yes"])
check("an unknown device exits 1", result.exit_code == 1, result.output)
check("the error names the device", "nosuchdevice" in result.output, result.output)


# ----------------------------------------------------- 6. API surfaces it
print("== 6. the API does not report success when the AVD survives ==")
import gui.app as gui_app  # noqa: E402

CLIENT = gui_app.create_app().test_client()

save_device("apidev", "antidetect_api_7")
make_avd(NATIVE, "antidetect_api_7")
__import__("shutil").rmtree = refuse
try:
    resp = CLIENT.delete("/api/devices/apidev")
finally:
    __import__("shutil").rmtree = real_rmtree
body = resp.get_json() or {}
check("a locked AVD returns 400", resp.status_code == 400, str(resp.status_code))
check("the response says success: false", body.get("success") is False, str(body))
check("the response carries the reason",
      "antidetect_api_7.avd" in str(body.get("error", "")), str(body))

resp = CLIENT.delete("/api/devices/apidev")
body = resp.get_json() or {}
check("a clean delete returns success", body.get("success") is True, str(body))
check("the AVD is gone after the API delete",
      not avd_exists(NATIVE, "antidetect_api_7"))
check("the toast can tell 'AVD already gone' apart",
      "avd_removed" in (REPO / "gui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
      or "already gone" in (REPO / "gui" / "static" / "js" / "app.js").read_text(encoding="utf-8"))

# -------------------------------- 7. leftovers from earlier failed deletes
print("== 7. orphaned AVDs are visible and cleanable ==")
from core.doctor import check_orphaned_avds, orphaned_avds  # noqa: E402

KEEP = "antidetect_keep_1"
save_device("keeper", KEEP)
make_avd(NATIVE, KEEP)
ORPHANS = ["antidetect_google_9e571d", "antidetect_google_77fbeb",
           "antidetect_honor_f9e3a5", "samsung_f14578"]
for name in ORPHANS:
    make_avd(NATIVE, name)

found = {name for _home, name, _size in orphaned_avds()}
check("every orphan is reported", set(ORPHANS) <= found, str(sorted(found)))
check("a saved device's AVD is not called an orphan", KEEP not in found, str(sorted(found)))
check("sizes are measured",
      all(size > 0 for _h, n, size in orphaned_avds() if n in ORPHANS))

row = check_orphaned_avds()
check("doctor flags orphaned AVDs", row["ok"] is False, str(row))
check("doctor names the cleanup command",
      "clean-avds" in (row.get("fix") or ""), str(row))
check("doctor counts them", str(len(ORPHANS)) in (row.get("detail") or ""), str(row))

dry = CliRunner().invoke(cli_main.cli, ["clean-avds", "--dry-run"])
check("--dry-run lists them", all(n in dry.output for n in ORPHANS[:2]), dry.output)
check("--dry-run removes nothing", (NATIVE / f"{ORPHANS[0]}.avd").exists())

run = CliRunner().invoke(cli_main.cli, ["clean-avds", "--yes"])
check("clean-avds exits 0", run.exit_code == 0, run.output)
check("all orphans are gone", all(not avd_exists(NATIVE, n) for n in ORPHANS))
check("the saved device survives", (NATIVE / f"{KEEP}.avd").exists(),
      "clean-avds deleted a device that is still in the app!")
check("doctor is clean afterwards", check_orphaned_avds()["ok"] is True,
      str(check_orphaned_avds()))

again = CliRunner().invoke(cli_main.cli, ["clean-avds", "--yes"])
check("a second run is a no-op that says so", "No orphaned AVDs" in again.output, again.output)


if failures:
    print(f"\n{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("\nAll delete/AVD cleanup checks passed.")
