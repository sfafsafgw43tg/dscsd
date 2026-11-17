#!/usr/bin/env python3
"""The app must never create a device on its own.

The reported behaviour: a fresh install already had a phone in it. `bootstrap`
(what START.bat / start.sh run) created a starter device called
``first_device`` whenever the saved list was empty, so the dashboard opened
with somebody else's Samsung Galaxy S24 - default cameras, default everything -
and every later bug report was filed against ``first_device``.

Pin the fix:

1. an empty install stays empty through bootstrap, the dashboard and the API;
2. an empty ``POST /api/devices`` is rejected instead of falling through to a
   default model;
3. creating a device is only possible by naming a model (dashboard dialog /
   ``main.py create`` / ``bootstrap.py --create NAME``);
4. nothing in the shipped code calls ``create_device`` implicitly, and no
   "first device" shortcut survives;
5. the dashboard shows the empty state with a button that names the choice.
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
from core.device_manager import DeviceManager  # noqa: E402

DEVICES_DIR = config.SAVED_DEVICES_DIR


def saved_names():
    return sorted(p.stem for p in DEVICES_DIR.glob("*.json"))


# --------------------------------------------------------------- clean slate
for stale in DEVICES_DIR.glob("*.json"):
    stale.unlink()
check("start with no saved devices", saved_names() == [], str(saved_names()))

# ------------------------------------------------------ 1. bootstrap behaviour
print("== 1. bootstrap creates nothing when the list is empty ==")
sys.path.insert(0, str(REPO))
import argparse  # noqa: E402

import bootstrap  # noqa: E402

args = argparse.Namespace(model="Samsung Galaxy S24", android="14", device=None, create=None,
                          no_device=False)
status = bootstrap.prepare_devices(args, sdk_ready=True)
check("prepare_devices reports 'empty'", status == "empty", str(status))
check("no device was created", saved_names() == [], str(saved_names()))
check("it says nothing was created",
      "nothing was created for you" in Path(REPO / "bootstrap.py").read_text(encoding="utf-8").lower())

# The legacy opt-out flag must behave the same as the default now.
args.no_device = True
check("--no-device is harmless (same empty result)",
      bootstrap.prepare_devices(args, sdk_ready=True) == "empty" and saved_names() == [])

# No SDK: still nothing.
args.no_device = False
check("without an SDK nothing is created",
      bootstrap.prepare_devices(args, sdk_ready=False) == "unavailable" and saved_names() == [])

# ------------------------------------------------------- 2. explicit --create
print("== 2. --create NAME is the only launcher path that creates ==")
created_args = argparse.Namespace(model="Google Pixel 8", android="14",
                                  device=None, create="my_phone", no_device=False)
# Do not launch (the fake emulator would boot); check creation only.
from unittest import mock  # noqa: E402

with mock.patch.object(DeviceManager, "launch_device",
                       side_effect=RuntimeError("skip launch in test")):
    status = bootstrap.prepare_devices(created_args, sdk_ready=True)
check("--create makes exactly the named device", saved_names() == ["my_phone"], str(saved_names()))
check("it is the requested model",
      DeviceManager().get_device("my_phone")["model"] == "Google Pixel 8")
check("a failed launch is reported, not hidden", status == "created", str(status))
check("no 'first_device' ever appears", "first_device" not in saved_names())

# ------------------------------------------------------------- 3. the GUI/API
print("== 3. the dashboard and API never create implicitly ==")
from gui.app import create_app  # noqa: E402

for stale in DEVICES_DIR.glob("*.json"):
    stale.unlink()
client = create_app().test_client()
check("loading the dashboard creates nothing",
      client.get("/").status_code == 200 and saved_names() == [], str(saved_names()))
check("listing devices creates nothing",
      client.get("/api/devices").get_json() == [] and saved_names() == [], str(saved_names()))

r = client.post("/api/devices", json={})
body = r.get_json()
check("an empty create request is refused", r.status_code == 400, str(r.status_code))
check("the refusal explains why",
      "never creates a device" in (body.get("error") or ""), json.dumps(body)[:200])
check("...and nothing was saved", saved_names() == [], str(saved_names()))

r = client.post("/api/devices", json={"name": "nameless"})
check("a name without a model is refused too",
      r.status_code == 400 and saved_names() == [], str(r.status_code))
r = client.post("/api/devices", json={"model": "   "})
check("a blank model is refused",
      r.status_code == 400 and saved_names() == [], str(r.status_code))

r = client.post("/api/devices", json={"model": "Google Pixel 8", "name": "chosen"})
check("a real, named model still creates a device",
      r.status_code == 200 and r.get_json().get("success") is True, str(r.status_code))
check("exactly that device exists", saved_names() == ["chosen"], str(saved_names()))

# -------------------------------------------------------- 4. no implicit code
print("== 4. the only create call sites name a model ==")
call_sites = {}
for path in REPO.rglob("*.py"):
    rel = path.relative_to(REPO)
    if str(rel).startswith(("tests/", ".")) or rel.parts[0] in ("node_modules", ".venv"):
        continue
    text = path.read_text(encoding="utf-8", errors="ignore")
    for m in re.finditer(r"create_device\(", text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        call_sites.setdefault(str(rel), []).append(text[line_start:m.start() + 40])
check("create_device has no call sites outside the explicit paths",
      set(call_sites) <= {"bootstrap.py", "gui/app.py", "main.py", "core/device_manager.py"},
      str(sorted(call_sites)))
check("bootstrap only creates when asked (guarded by `wanted`)",
      "wanted = getattr(args, \"create\", None) or args.device" in
      Path(REPO / "bootstrap.py").read_text(encoding="utf-8"))
bootstrap_src = Path(REPO / "bootstrap.py").read_text(encoding="utf-8")
check("no hardcoded starter-device name left",
      not re.search(r"[\"']first_device[\"']", bootstrap_src))
check("no starter device is created on the empty path",
      "if args.no_device:" not in bootstrap_src or "manager.create_device" not in
      bootstrap_src.split("if args.no_device:")[1].split("return")[0])
check("`python main.py` opens the dashboard instead of printing help",
      "invoke_without_command=True" in Path(REPO / "main.py").read_text(encoding="utf-8"))

# ------------------------------------------------------------ 5. the empty UI
print("== 5. the dashboard says nothing was created for you ==")
ui = (REPO / "gui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
check("the empty state explains it", "Nothing is created for you" in ui)
check("the button names the choice, not 'first device'",
      "Choose a model &amp; create a device" in ui)
check("the old 'Create Your First Device' wording is gone",
      "Create Your First Device" not in ui)
check("it still opens the New Device dialog", "showCreateDialog()" in ui)

# ------------------------------------------------------ 6. end-to-end, once more
print("== 6. a fresh install survives a full bootstrap + dashboard load ==")
for stale in DEVICES_DIR.glob("*.json"):
    stale.unlink()
env = dict(os.environ)
r = subprocess.run([sys.executable, str(REPO / "main.py"), "--help"],
                   capture_output=True, text=True, env=env, timeout=120)
check("main.py --help mentions the no-command dashboard",
      "No command" in r.stdout or "no command" in r.stdout or "Running it with no command" in r.stdout,
      r.stdout[:200])
check("help still lists create", "create" in r.stdout)
check("no device materialised from running the CLI", saved_names() == [], str(saved_names()))

if failures:
    print(f"\n{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("\nAll no-auto-device checks passed.")
