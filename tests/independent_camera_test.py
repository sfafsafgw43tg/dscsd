#!/usr/bin/env python3
"""Front/back cameras must be independent: different sources, and to apps they
must look like two unrelated physical sensors.

Covers:
- per-facing sources survive create / reload / config.ini / launch flags;
- the generated camera specs are independent (different sensor model, serial,
  resolution, orientation) and stable for a device;
- pre-existing devices (no specs) get specs generated when the cameras change;
- the Frida payload ships the specs, and camera_hooks.js spoofs them per facing;
- the "same source on both facings" tell is reported, not silently accepted;
- CLI/GUI/UI wiring exists (presets, spec rows, usb sources).
"""
import json
import os
import re
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

failures = []


def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(label)


import config  # noqa: E402
from core.device_manager import (  # noqa: E402
    CAMERA_DEFAULT_BACK,
    CAMERA_DEFAULT_FRONT,
    DeviceManager,
    camera_role_conflicts,
    validate_camera_mode,
)
from profiles.profile_generator import generate_device_identity  # noqa: E402

manager = DeviceManager()

# ------------------------------------------------------------ validation
print("== source validation ==")
check("webcam1 accepted", validate_camera_mode("webcam1", None, "front") == "webcam1")
check("usb2 accepted", validate_camera_mode("usb2", None, "back") == "usb2")
check("bare 'usb' becomes usb0", validate_camera_mode("usb", None, "back") == "usb0")
check("case/space normalised", validate_camera_mode("  WEBCAM3 ", None, "back") == "webcam3")
check("empty falls back to default", validate_camera_mode("", "emulated", "front") == "emulated")
try:
    validate_camera_mode("webcamera", None, "front")
    check("invalid webcam rejected", False, "no ValueError")
except ValueError as exc:
    check("invalid webcam rejected", "webcam" in str(exc).lower())

# ------------------------------------------------------- spec generation
print("== generated specs are independent ==")
identity = generate_device_identity({"brand": "Samsung", "model": "SM-S921B",
                                     "name": "Samsung Galaxy S24",
                                     "board": "s5e9945", "device": "e2s"})
specs = identity["camera_specs"]
back, front = specs["back"], specs["front"]
check("both facings have specs", bool(back) and bool(front))
check("distinct sensor models", back["model"] != front["model"],
      f"{back['model']} vs {front['model']}")
check("distinct serials", back["serial"] != front["serial"])
check("distinct resolutions",
      (back["res_width"], back["res_height"]) != (front["res_width"], front["res_height"]),
      f"{back['res_width']}x{back['res_height']} vs {front['res_width']}x{front['res_height']}")
check("opposite lens orientations", back["orientation"] != front["orientation"])
check("front reports facing=front", front["facing"] == "front" and back["facing"] == "back")
check("back focal > front focal (wide vs selfie)",
      back["focal_length"] > front["focal_length"],
      f"{back['focal_length']} vs {front['focal_length']}")
check("specs stable for the same device",
      generate_device_identity({"brand": "Samsung", "model": "SM-S921B",
                                "name": "Samsung Galaxy S24",
                                "board": "s5e9945", "device": "e2s"})["camera_specs"] == specs)

print("== role-conflict hint ==")
check("both emulated flagged", "shared camera" in (camera_role_conflicts("emulated", "emulated") or ""))
check("same webcam flagged", camera_role_conflicts("webcam0", "webcam0") is not None)
check("webcam0+webcam1 is fine", camera_role_conflicts("webcam0", "webcam1") is None)
check("virtualscene+emulated is fine", camera_role_conflicts("emulated", "virtualscene") is None)
check("none is fine", camera_role_conflicts("none", "webcam0") is None)

# ------------------------------------------------------------- creation
print("== create: two different real cameras ==")
device = manager.create_device(
    "Samsung Galaxy S24", android_version="14", name="camsplit",
    camera_front="webcam1", camera_back="webcam0",
)
check("sources kept per facing",
      device["camera"] == {"front": "webcam1", "back": "webcam0"}, str(device["camera"]))
check("no warning for webcam0+webcam1", "camera_warning" not in device)
check("specs stored on the device", bool(device.get("camera_specs", {}).get("back")))

avd_config = config.AVD_HOME / f"{device['avd_name']}.avd" / "config.ini"
text = avd_config.read_text()
check("config.ini back=webcam0", "hw.camera.back=webcam0" in text)
check("config.ini front=webcam1", "hw.camera.front=webcam1" in text)

reloaded = manager.get_device("camsplit")
check("sources persisted", reloaded["camera"] == {"front": "webcam1", "back": "webcam0"})
check("specs persisted", reloaded.get("camera_specs", {}).get("front", {}).get("serial")
      == device["camera_specs"]["front"]["serial"])

print("== create: same source warns ==")
same = manager.create_device(
    "Xiaomi 13", android_version="14", name="camsame",
    camera_front="webcam0", camera_back="webcam0",
)
check("shared-source create returns a warning", "shared camera" in same.get("camera_warning", ""))

# ------------------------------------------------------- set-camera path
print("== switching sources keeps identities independent ==")
result = manager.set_device_cameras("camsplit", camera_front="webcam0", camera_back="webcam0")
check("warning surfaced on update", result["camera_warning"] is not None)
check("specs unchanged by a source switch",
      result["camera_specs"]["back"]["serial"] == device["camera_specs"]["back"]["serial"])
check("config.ini rewritten without duplicate keys",
      (config.AVD_HOME / f"{device['avd_name']}.avd" / "config.ini").read_text().count("hw.camera.back") == 1)

# A device created before per-camera specs existed.
print("== legacy device gets specs on camera change ==")
legacy = manager.get_device("camsplit")
identity_legacy = dict(legacy["identity"])
identity_legacy.pop("camera_specs", None)
legacy["identity"] = identity_legacy
legacy.pop("camera_specs", None)
manager._save_device(legacy)
backfilled = manager.set_device_cameras("camsplit", camera_front="emulated", camera_back="virtualscene")
check("legacy device backfilled with specs", bool(backfilled["camera_specs"].get("back")))
check("legacy specs are independent",
      backfilled["camera_specs"]["back"]["serial"] != backfilled["camera_specs"]["front"]["serial"])
check("identity also updated",
      manager.get_device("camsplit")["identity"].get("camera_specs", {}).get("back") is not None)

# ------------------------------------------------------- Frida payload
print("== Frida hook ships the per-camera identity ==")
hooks_js = (REPO / "frida_toolkit" / "scripts" / "camera_hooks.js").read_text(encoding="utf-8")
check("camera_hooks.js exists and defines the installer", "_installCameraHooks" in hooks_js)
check("hooks Camera2 characteristics",
      "getCameraCharacteristics" in hooks_js and "LENS_FACING" in hooks_js)
check("spoofs sensor serial per facing", "android.sensor.info.serialNumber" in hooks_js)
check("spoofs physical size / pixel array",
      "android.sensor.info.physicalSize" in hooks_js and "android.sensor.info.pixelArraySize" in hooks_js)
check("spoofs focal length + aperture",
      "availableFocalLengths" in hooks_js and "availableApertures" in hooks_js)
check("spoofs orientation and pose",
      "android.sensor.orientation" in hooks_js and "android.lens.poseTranslation" in hooks_js)
check("hooks Camera1 API", "android.hardware.Camera" in hooks_js and "getCameraInfo" in hooks_js)
check("never throws into the app",
      hooks_js.count("catch (e)") >= 8 and "setImmediate" in hooks_js)
check("no obvious placeholder / undefined helper",
      "new android.graphics.Rect" not in hooks_js and "ImageFormat" not in hooks_js)

hook_manager_src = (REPO / "core" / "hook_manager.py").read_text(encoding="utf-8")
check("camera_hooks.js is loaded with the combined script", '"camera_hooks.js"' in hook_manager_src)
check("camera specs are in the generated JS config", '"cameras": identity.get("camera_specs")' in hook_manager_src)

from core.hook_manager import HookManager  # noqa: E402

combined = HookManager("emulator-5554").generate_combined_script(
    manager.get_device("camsplit")["identity"]
)
check("combined JS contains camera specs", '"cameras"' in combined and '"serial"' in combined)
check("combined JS runs the camera installer", "_installCameraHooks" in combined)
check("combined JS has the sensor serials baked in",
      manager.get_device("camsplit")["camera_specs"]["back"]["serial"] in combined)

# ------------------------------------------------------------------ GUI
print("== GUI wiring ==")
app_src = (REPO / "gui" / "app.py").read_text(encoding="utf-8")
check("/api/cameras lists usb sources", '"usb0"' in app_src)
check("/api/cameras reports host webcams", '"webcams"' in app_src)
check("identity endpoint exposes camera specs", 'identity["camera_specs"]' in app_src)
check("camera update returns the warning", 'result["warning"] = result["camera_warning"]' in app_src)

js = (REPO / "gui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
check("details panel renders both sensors", "camera-spec-row" in js)
check("per-facing preset exists", "applyCameraPreset" in js and "'webcam1'" in js)
check("create-form preset exists", "applyCreateCameraPreset" in js)
check("settings preset exists", "applySettingsCameraPreset" in js)
check("shared-webcam shortcut now warns", "same images" in js)
check("camera warning shown after save", "showToast(data.warning" in js)

html = (REPO / "gui" / "templates" / "index.html").read_text(encoding="utf-8")
check("two-different-webcams button on the create form", "Two different webcams" in html)
check("two-virtual-sources button on the create form", "Two virtual sources" in html)
check("settings offer the independent preset", "applySettingsCameraPreset('independent')" in html)

css = (REPO / "gui" / "static" / "css" / "style.css").read_text(encoding="utf-8")
check("spec row styles present", ".camera-spec-row" in css)

cli = (REPO / "main.py").read_text(encoding="utf-8")
check("CLI prints both sensors", "sensor:" in cli)
check("CLI warns on a shared source", "camera_warning" in cli)

if failures:
    print(f"\n{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("\nAll independent-camera checks passed.")
