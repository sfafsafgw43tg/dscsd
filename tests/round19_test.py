#!/usr/bin/env python3
"""Round 19: cameras match the phone, GPU never reveals the host, spoof
self-recovers on launch, and the radios are gated by the spoof.

Four user reports, each pinned here:

1. "make the cameras match the device"
   The camerametadata described the *emulator* (one synthetic sensor on both
   facings, a generic 640x480/1280x720 size list). It must describe the
   selected phone: per-model sensor, resolution, aperture, focal length and
   orientation, front and back independent, AVD config.ini in sync.

2. "change the GPU render cause OpenGL reveals it as my GPU still"
   The emulator ran with ``-gpu auto`` (host GL passthrough), so the guest
   answered with the *host* card -
   ``Android Emulator OpenGL ES Translator (AMD Radeon RX 6700 XT)``. The
   renderer is now a guest/software stack and every GL query path (including
   the Chromium WebGL/GPU process) answers from the spoofed chip.

3. "make the spoof always automatically recover and apply into the device on
   the launch"
   A durable intent + boot-id + prop-drift check, re-applied unattended on
   every launch and by a startup sweep, with hooks re-attached from intent.

4. "make the device be on airplane mode when spoof and fingerprint not
   applied"
   The radios are held off while the spoof is unverified and released as soon
   as it verifies, verified through the device's own settings DB.
"""
import json
import os
import re
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
from core.device_manager import (  # noqa: E402
    GUEST_GPU_MODE,
    DeviceManager,
    resolve_gpu_mode,
)
from profiles.device_profiles import (  # noqa: E402
    ALL_DEVICES,
    CAMERA_PROFILES,
    get_camera_profile,
)
from profiles.profile_generator import (  # noqa: E402
    generate_camera_specs,
    generate_device_identity,
)

SERIAL = "emulator-5554"


def gp(key, serial=SERIAL):
    r = subprocess.run([config.ADB_PATH, "-s", serial, "shell", "getprop", key],
                       capture_output=True, text=True, timeout=20)
    return r.stdout.strip()


def settings_get(key, serial=SERIAL):
    r = subprocess.run([config.ADB_PATH, "-s", serial, "shell", "settings", "get", key],
                       capture_output=True, text=True, timeout=20)
    return r.stdout.strip()


# ===========================================================================
# 1. CAMERAS MATCH THE SELECTED DEVICE
# ===========================================================================
print("== 1. every model has its own camera hardware ==")
all_names = [d["name"] for brand in ALL_DEVICES.values() for d in brand]
check("every model has a camera profile",
      all(n in CAMERA_PROFILES for n in all_names),
      str([n for n in all_names if n not in CAMERA_PROFILES]))
check("no orphan camera profiles",
      not [n for n in CAMERA_PROFILES if n not in all_names],
      str([n for n in CAMERA_PROFILES if n not in all_names]))
check("profile lookups work by hardware code too",
      get_camera_profile("SM-S928B") == CAMERA_PROFILES["Samsung Galaxy S24 Ultra"])
check("unknown models return None (fallback path)", get_camera_profile("Nokia 3310") is None)

bad = []
for name, entry in CAMERA_PROFILES.items():
    back, front = entry.get("back"), entry.get("front")
    if not back or not front:
        bad.append((name, "missing facing"))
        continue
    if back[0] == front[0]:
        bad.append((name, "same sensor on both facings"))
    if back[1] == front[1]:
        bad.append((name, "same resolution on both facings"))
    if back[5] == front[5]:
        bad.append((name, "same orientation on both facings"))
    if back[3] <= front[3]:
        bad.append((name, "back focal not longer than front"))
    if back[1][0] < 4000 or front[1][0] < 3000:
        bad.append((name, "resolution too low to be a real phone"))
check("all camera profiles are plausible and independent", not bad, str(bad[:5]))

print("== the specs describe the phone, not the emulator ==")
s24u = generate_device_identity(
    {"brand": "Samsung", "model": "SM-S928B", "name": "Samsung Galaxy S24 Ultra",
     "board": "s5e9945", "device": "e2s"})["camera_specs"]
check("S24 Ultra back sensor is the profile sensor",
      s24u["back"]["model"] == CAMERA_PROFILES["Samsung Galaxy S24 Ultra"]["back"][0],
      s24u["back"]["model"])
check("S24 Ultra back is 200 MP, not the emulator's 640x480",
      (s24u["back"]["res_width"], s24u["back"]["res_height"]) == (16320, 12240),
      f"{s24u['back']['res_width']}x{s24u['back']['res_height']}")
check("front and back are different sensors", s24u["back"]["model"] != s24u["front"]["model"])
check("front and back have different serials", s24u["back"]["serial"] != s24u["front"]["serial"])
check("front faces front and back faces back (pose rotation differs)",
      s24u["front"]["pose_rotation"] != s24u["back"]["pose_rotation"])
check("a stream-size list is generated from the sensor",
      len(s24u["back"].get("stream_sizes") or []) >= 4,
      str(s24u["back"].get("stream_sizes")))
check("the full sensor resolution is the biggest stream size",
      tuple(s24u["back"]["stream_sizes"][0]) == (16320, 12240))
check("smaller stream sizes are smaller",
      all(w * h <= 16320 * 12240 for w, h in s24u["back"]["stream_sizes"]))

pixel = generate_device_identity(
    {"brand": "Google", "model": "Pixel 8", "name": "Google Pixel 8",
     "board": "zuma", "device": "shiba"})["camera_specs"]
check("Pixel 8 gets the Pixel 8 sensor, not the S24's",
      pixel["back"]["model"] == "SAMSUNG ISOCELL GN2", pixel["back"]["model"])
check("two phones do not share a camera identity",
      pixel["back"]["model"] != s24u["back"]["model"])
check("a hardware-code model resolves the same as its name",
      generate_camera_specs("Samsung", "s5e9945", "SM-S921B")["back"]["model"]
      == generate_camera_specs("Samsung", "s5e9945", "Samsung Galaxy S24")["back"]["model"])
unknown = generate_camera_specs("Acme", "board", "Acme Phone 9")
check("unknown models still get two independent cameras",
      unknown["back"]["model"] != unknown["front"]["model"]
      and (unknown["back"]["res_width"], unknown["back"]["res_height"])
      != (unknown["front"]["res_width"], unknown["front"]["res_height"]))
check("specs stay stable for one device",
      generate_camera_specs("Samsung", "s5e9945", "SM-S921B")
      == generate_camera_specs("Samsung", "s5e9945", "SM-S921B"))

print("== the CLI shows the per-model cameras and the renderer ==")
main_src = (REPO / "main.py").read_text(encoding="utf-8")
check("create prints the per-model sensors",
      "for facing in (\"back\", \"front\"):" in main_src and "sensor: {spec['model']}" in main_src)
check("create prints the emulator renderer",
      "Emulator renderer:" in main_src and "resolve_gpu_mode(device)[0]" in main_src)

print("== the hooks report the phone model and its sizes ==")
camera_js = (REPO / "frida_toolkit" / "scripts" / "camera_hooks.js").read_text(encoding="utf-8")
check("stream configurations are rebuilt per facing",
      "android.scaler.availableStreamConfigurations" in camera_js)
check("stream sizes come from the spec, not a constant",
      "spec.stream_sizes" in camera_js and "640, 480" not in camera_js.replace(" ", " "))
check("StreamConfiguration objects are built element-by-element",
      "streamConfigArr" in camera_js and "setAccessible(true)" in camera_js)
check("EXIF reports the phone model for both facings",
      'tag === "LensModel" || tag === "LensMake"' in camera_js)
check("camera hooks never throw into the app",
      camera_js.count("catch (e)") >= 10 and "setImmediate" in camera_js)

# ===========================================================================
# 2. THE GPU NEVER REVEALS THE HOST
# ===========================================================================
print("== 2. the emulator renders with a guest stack, never host GL ==")
check("the default GPU mode is not host passthrough",
      config.DEFAULT_GPU_MODE not in config.HOST_GPU_MODES, config.DEFAULT_GPU_MODE)
check("the default is the guest/software renderer",
      config.DEFAULT_GPU_MODE == "swiftshader_indirect", config.DEFAULT_GPU_MODE)
check("host modes are named explicitly",
      set(config.HOST_GPU_MODES) == {"auto", "host", "angle", "desktop"},
      str(config.HOST_GPU_MODES))
check("a spoofed device never launches with host GL",
      resolve_gpu_mode({"auto_spoof": True}, requested="host")[0] == GUEST_GPU_MODE)
check("...including the emulator's 'auto' (which is host passthrough)",
      resolve_gpu_mode({"auto_spoof": True}, requested="auto")[0] == GUEST_GPU_MODE)
mode, note = resolve_gpu_mode({"auto_spoof": True}, requested="auto")
check("the substitution is explained, not silent",
      note and "AMD Radeon" in note and GUEST_GPU_MODE in note, str(note))
check("a device that opted out of spoofing may keep its choice",
      resolve_gpu_mode({"auto_spoof": False}, requested="host") == ("host", None))
check("a saved guest mode is respected",
      resolve_gpu_mode({"auto_spoof": True, "gpu_mode": "swiftshader"})[0] == "swiftshader")

dm_src = (REPO / "core" / "device_manager.py").read_text(encoding="utf-8")
check("the launch flags use the resolved mode",
      '"-gpu", gpu_mode' in dm_src and '"-gpu", config.DEFAULT_GPU_MODE' not in dm_src)
check("config.ini agrees with the launch flags",
      "hw.gpu.mode={resolve_gpu_mode(device)[0]}" in dm_src)
check("the resolved mode is persisted on the device",
      'device["gpu_mode"] = gpu_mode' in dm_src)
check("the host-GPU leak is explained in config.py",
      "AMD Radeon RX 6700 XT" in (REPO / "config.py").read_text(encoding="utf-8"))

print("== every GL query path answers from the spoofed chip ==")
gpu_js = (REPO / "frida_toolkit" / "scripts" / "gpu_hooks.js").read_text(encoding="utf-8")
anti_js = (REPO / "frida_toolkit" / "scripts" / "anti_detect.js").read_text(encoding="utf-8")
for label, needle in (
    ("native glGetString", 'findExport(GL_LIBS, "glGetString")'),
    ("native glGetStringi", 'findExport(GL_LIBS, "glGetStringi")'),
    ("native glGetIntegerv limits", 'findExport(GL_LIBS, "glGetIntegerv")'),
    ("native glGetShaderPrecisionFormat",
     'findExport(GL_LIBS, "glGetShaderPrecisionFormat")'),
    ("native eglQueryString", 'findExport(["libEGL.so"], "eglQueryString")'),
    ("Java GLES20.glGetString", "GLES20.glGetString"),
    ("Java GLES20.glGetShaderPrecisionFormat", "GLES20.glGetShaderPrecisionFormat"),
    ("Java EGL14.eglQueryString", "EGL14.eglQueryString"),
    ("SystemProperties (Chromium reads these)", "android.os.SystemProperties"),
):
    check(f"gpu_hooks.js covers {label}", needle in gpu_js)
check("the renderer script still leaves ro.hardware.egl alone",
      'if (key === "ro.hardware.egl") return null;' in gpu_js
      and 'if (key === "ro.hardware.vulkan") return null;' in gpu_js)
check("anti_detect.js keeps the full GL coverage",
      all(n in anti_js for n in ("0x1F00", "0x1F01", "0x1F02", "0x1F03", "0x1F04",
                                 "0x3053", "0x3054", "0x3055")))
check("no hook ever passes a host string through",
      "Android Emulator|SwiftShader|ANGLE" in anti_js  # documented as a *tell* only
      and "if (spoof) return spoof;" in anti_js and "if (spoof) return spoof;" in gpu_js)

# ===========================================================================
# 3. THE SPOOF ALWAYS RECOVERS AND RE-APPLIES ON LAUNCH
# ===========================================================================
print("== 3. launch re-applies and self-heals the whole chain ==")
mgr = DeviceManager()
device = mgr.create_device("Google Pixel 8", android_version="14", name="recoverdev")
launched = mgr.launch_device("recoverdev", enable_frida=False, wait_for_boot=True)
check("launch spoofs automatically", mgr.get_device("recoverdev").get("spoof_applied") is True)
check("the intent is recorded for the next boot",
      mgr.get_device("recoverdev").get("auto_spoof") is True)
check("the boot the spoof belongs to is pinned",
      bool(mgr.get_device("recoverdev").get("runtime_boot_id")))
check("a fresh device holds no drift", mgr.spoof_drift(mgr.get_device("recoverdev"))[0] is False)

# Simulate a *partial* spoof: one prop reverted without a reboot. This used to
# stay "applied" forever.
live = mgr._load_device("recoverdev")
live["spoof_report"] = {"verified": {"ro.product.model": "sdk_gphone64_x86_64",
                                     "ro.product.brand": live["identity"]["brand"]},
                        "failed": [], "method": "resetprop"}
# (the report is the device's own getprop output; a prop that reads back as the
# emulator default is exactly the drift this must catch)
mgr._save_device(live)
drift, reason = mgr.spoof_drift(mgr.get_device("recoverdev"))
check("a drifted prop is detected", drift is True and "ro.product.model" in (reason or ""),
      str(reason))
check("drift marks the device stale in the dashboard",
      mgr.get_device("recoverdev").get("runtime_stale") is True)
recovered = mgr.autorecover("recoverdev")
check("autorecover re-applies the drifted spoof", recovered.get("spoofed") is True,
      json.dumps(recovered)[:300])
check("the value is really back", gp("ro.product.model") == device["identity"]["model"],
      gp("ro.product.model"))
check("a second autorecover is a no-op",
      mgr.autorecover("recoverdev").get("changed") is False)
check("the startup sweep finds nothing to do once healed",
      mgr.autorecover_all() == [])

print("== a reboot is healed without any click ==")
def reboot_guest():
    subprocess.run([config.ADB_PATH, "-s", SERIAL, "reboot"], capture_output=True, timeout=30)
    # The boot id is cached for 15s; a real dashboard poll would be later.
    mgr._boot_id_cache.clear()
    time.sleep(0.3)

reboot_guest()
after_reboot = mgr.get_device("recoverdev")
check("the reboot is detected as lost state", after_reboot.get("runtime_stale") is True)
check("the intent survived the reboot", after_reboot.get("auto_spoof") is True)
sweep = mgr.autorecover_all()
check("the startup sweep re-applies it", len(sweep) == 1 and sweep[0].get("success") is True,
      json.dumps(sweep)[:300])
check("the spoof is verified again after the sweep",
      gp("ro.product.model") == device["identity"]["model"], gp("ro.product.model"))
check("stale is cleared afterwards", mgr.get_device("recoverdev").get("runtime_stale") is False)

print("== hooks are re-attached from the saved intent ==")
mgr.mark_runtime_applied("recoverdev", serial=SERIAL, hooks=True)
check("hooks intent saved", mgr._load_device("recoverdev").get("auto_hooks") is True)
check("a launch re-attaches hooks the user asked for",
      mgr.wants_hooks_on_launch({"auto_hooks": True, "hooks_applied": False}) is True)
check("hooks that are already live are not re-attached",
      mgr.wants_hooks_on_launch({"auto_hooks": True, "hooks_applied": True}) is False)
check("an opt-out is honoured",
      mgr.wants_hooks_on_launch({"auto_hooks": False, "hooks_applied_at": "x"}) is False)
check("a never-hooked device is left alone",
      mgr.wants_hooks_on_launch({}) is False)
dm_src2 = dm_src
check("launch_device re-attaches hooks without the frida flag",
      "elif wait_for_boot and self.wants_hooks_on_launch(live)" in dm_src2)
check("the dashboard sweep uses autorecover",
      "manager.autorecover_all()" in (REPO / "gui" / "app.py").read_text(encoding="utf-8"))
mgr.set_restore_intent("recoverdev", spoof=False, hooks=False)
check("opting out stops autorecover",
      mgr.autorecover("recoverdev").get("changed") is False)

# ===========================================================================
# 4. AIRPLANE MODE IS NEVER AUTOMATIC (the old gate was removed)
# ===========================================================================
print("== 4. nothing turns airplane mode on by itself ==")
mgr.set_restore_intent("recoverdev", spoof=True)
live = mgr._load_device("recoverdev")
live["spoof_applied"] = False
live.pop("runtime_boot_id", None)
mgr._save_device(live)

check("the automatic gate is gone from the manager",
      not hasattr(DeviceManager, "enforce_airplane_gate"))
check("...and so is the 'should the radios be off' rule",
      not hasattr(DeviceManager, "_airplane_required"))
check("no source file calls a gate", "enforce_airplane_gate" not in dm_src)
check("the GUI does not gate either",
      "enforce_airplane_gate" not in (REPO / "gui" / "app.py").read_text(encoding="utf-8"))

# An unspoofed, running device must stay online through a recovery.
state_before = settings_get("global airplane_mode_on")
healed = mgr.autorecover("recoverdev")
check("recovery still re-applies the spoof", healed.get("spoofed") is True,
      json.dumps(healed)[:200])
check("a recovery never switches airplane mode on",
      settings_get("global airplane_mode_on") != "1" or state_before == "1",
      settings_get("global airplane_mode_on"))
check("the device is reported as online, not gated",
      mgr.get_device("recoverdev").get("airplane_gate") in ("off", "held-off", None),
      str(mgr.get_device("recoverdev").get("airplane_gate")))
check("airplane_required is no longer asserted by the app",
      mgr.get_device("recoverdev").get("airplane_required") is False)

# A failed spoof is reported, never 'fixed' by cutting the radios.
live = mgr._load_device("recoverdev")
live["spoof_applied"] = False
live["spoof_error"] = "Could not change props ['ro.product.model']"
mgr._save_device(live)
mgr.set_restore_intent("recoverdev", spoof=False, hooks=False)
check("a failed spoof keeps the error visible",
      bool(mgr.get_device("recoverdev").get("spoof_error")))
check("...and does not put the device in airplane mode",
      settings_get("global airplane_mode_on") != "1", settings_get("global airplane_mode_on"))

# The manual switch still works, and is still honest.
ok, msg = mgr.set_airplane_mode("recoverdev", True)
check("the user can still turn airplane mode on", ok is True and
      settings_get("global airplane_mode_on") == "1", msg)
ok, msg = mgr.set_airplane_mode("recoverdev", False)
check("...and off again", ok is True and settings_get("global airplane_mode_on") == "0", msg)
try:
    mgr.set_airplane_mode("missing-device", True)
    check("an unknown device raises instead of pretending", False)
except ValueError as exc:
    check("an unknown device raises instead of pretending", "not found" in str(exc))

mgr.set_restore_intent("recoverdev", spoof=True, hooks=True)

print("== a failed spoof is reported, not enforced ==")
check("a failed spoof is reported instead of a green launch",
      "is still NOT spoofed after launch" in dm_src)
check("SystemSpoofer can still set and read airplane mode",
      all(n in (REPO / "spoof" / "system_spoof.py").read_text(encoding="utf-8")
          for n in ("def set_airplane_mode", "def airplane_mode_enabled",
                    "AIRPLANE_MODE")))
check("a manual spoof never touches the radios",
      "enforce_airplane_gate" not in (REPO / "gui" / "app.py").read_text(encoding="utf-8"))
check("SystemSpoofer can set and read airplane mode",
      all(n in (REPO / "spoof" / "system_spoof.py").read_text(encoding="utf-8")
          for n in ("def set_airplane_mode", "def airplane_mode_enabled",
                    "AIRPLANE_MODE")))
ui_js = (REPO / "gui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
check("the UI no longer claims the spoof is holding the radios off",
      "Airplane (spoof pending)" not in ui_js)
check("the UI still shows the real radio state",
      "✈ Airplane" in ui_js and "'Online'" in ui_js)
check("the card offers a one-click recovery action",
      "data-action=\"recover\"" in ui_js and "runCardAction" in ui_js
      and "async function recoverDevice" in ui_js)
check("recover calls the autorecover endpoint",
      "/autorecover`" in ui_js)
check("the airplane override parses 'off' instead of coercing it",
      "enable === 'on' || enable === 'true'" in ui_js)
check("the details dialog shows the per-model sensor rows",
      "camera-spec-row" in ui_js and "identity.camera_specs" in ui_js)
check("the details dialog names the emulator renderer",
      "Emulator renderer" in ui_js and "device.gpu_mode" in ui_js)

# ===========================================================================
# 5. END-TO-END: a real launch spoofs, and leaves the radios alone
# ===========================================================================
print("== 5. end-to-end launch: spoof verifies, radios untouched ==")
mgr.stop_device("recoverdev")
second = mgr.launch_device("recoverdev", enable_frida=False, wait_for_boot=True)
check("launch succeeded", second.get("serial") == SERIAL)
after_launch = mgr.get_device("recoverdev")
check("the spoof is verified after launch", after_launch.get("spoof_applied") is True)
check("the launch left the radios alone",
      settings_get("global airplane_mode_on") != "1",
      settings_get("global airplane_mode_on"))
check("the card shows no gate machinery, just the real state",
      after_launch.get("airplane_gate") in ("off", "held-off"),
      str(after_launch.get("airplane_gate")))
check("the launch history mentions no airplane gate",
      all("airplane" not in (h.get("text") or "").lower()
          for h in (mgr.launch_progress("recoverdev") or {}).get("history", [])),
      str((mgr.launch_progress("recoverdev") or {}).get("history"))[:200])

# ===========================================================================
# 6. DOCTOR
# ===========================================================================
print("== 6. doctor flags a host-GPU device and a leaked radio ==")
from core.doctor import check_renderer_and_radios  # noqa: E402

live = mgr._load_device("recoverdev")
live["gpu_mode"] = "auto"
mgr._save_device(live)
report = check_renderer_and_radios()
check("doctor reports host-GL configuration as a warning",
      report["severity"] == "warn" and "host-GPU passthrough" in report["detail"],
      str(report)[:200])
check("doctor names the offending device", "recoverdev" in report["detail"])
check("doctor's fix says the launch is already protected",
      "forces -gpu swiftshader_indirect" in (report.get("fix") or ""), str(report.get("fix")))

live = mgr._load_device("recoverdev")
live["gpu_mode"] = "swiftshader_indirect"
live["spoof_applied"] = False
live["airplane_mode"] = False
mgr._save_device(live)
report = check_renderer_and_radios()
check("doctor does not flag an unspoofed device for keeping its radios on",
      report["severity"] == "ok" and "radios on" not in report["detail"],
      str(report)[:200])
check("doctor says radios are never forced",
      "never forced into airplane mode" in report["detail"], str(report)[:200])

live = mgr._load_device("recoverdev")
live["spoof_applied"] = True
live["airplane_mode"] = False
mgr._save_device(live)
report = check_renderer_and_radios()
check("doctor is happy once the spoof is applied",
      report["severity"] == "ok" and "host-GPU" not in report["detail"], str(report)[:200])

if failures:
    print(f"\n{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print(f"\nAll round-19 checks passed ({0} failures).")
