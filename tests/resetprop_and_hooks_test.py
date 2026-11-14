#!/usr/bin/env python3
"""Comprehensive regression tests for all anti-detect spoofing & Frida hook fixes.

Covers:
1. ResetpropManager ELF/ABI validation & fast-path spoofing via resetprop-rs
   + soft zygote restart (zero full reboots) under strict-offline + adb root.
2. Fallback path (-writable-system + avbctl + disable-verity + remount +
   build.prop) under strict-offline + require-writable-system + adb root.
3. DeviceManager passes -writable-system for rootable images, propagates
   android_version into identity, and cleans up simulators/Frida sessions on stop.
4. HookManager persistent _LIVE_SESSIONS (survives GC), Frida 17 Java bridge
   wrapping, script error reporting, attach-only handling for GMS, and JS
   syntax/safety checks across native_hooks.js / anti_detect.js / safetynet_bypass.js.
5. GUI API & app.js fixes (version field, hooks_applied persistence, error key,
   VERSION_TO_API 15, live #spoofStatus sync).
"""
import gc
import json
import os
import struct
import subprocess
import sys
import tempfile
import types
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


def make_fake_elf(path: Path, machine: int = 62, size: int = 65536):
    header = b"\x7fELF" + b"\x02\x01\x01\x00" + b"\x00" * 10 + struct.pack("<H", machine)
    header += b"\x00" * (64 - len(header))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + b"\x00" * size)


import config  # noqa: E402

config.DATA_DIR = TMP / "data"
config.SAVED_DEVICES_DIR = TMP / "devices"
config.LOGS_DIR = TMP / "logs"
for d in (config.DATA_DIR, config.SAVED_DEVICES_DIR, config.LOGS_DIR):
    d.mkdir(parents=True, exist_ok=True)

from core.device_manager import DeviceManager  # noqa: E402
from core.hook_manager import (  # noqa: E402
    HookManager,
    active_sessions_for,
    detach_device_sessions,
    wrap_with_java_bridge,
)
from spoof.hardware_spoof import build_identity_prop_maps  # noqa: E402
from spoof.resetprop_manager import (  # noqa: E402
    RESETPROP_VERSION,
    cache_path,
    verify_local_binary,
    version_path,
)


def _gp(serial, key):
    r = subprocess.run(
        [config.ADB_PATH, "-s", serial, "shell", "getprop", key],
        capture_output=True,
        text=True,
        timeout=20,
    )
    return r.stdout.strip()


# ------------------------------------------------------------------ 1. ELF validation
print("== 1. ResetpropManager ELF & ABI validation ==")
bad_small = TMP / "small.bin"
bad_small.write_bytes(b"\x7fELF" + b"\x00" * 100)
check("rejects too-small binary", not verify_local_binary(bad_small, "x86_64")[0])

bad_magic = TMP / "not_elf.bin"
bad_magic.write_bytes(b"HTML" + b"\x00" * 70000)
check("rejects non-ELF binary", not verify_local_binary(bad_magic, "x86_64")[0])

arm64_bin = TMP / "arm64.bin"
make_fake_elf(arm64_bin, machine=183)
check("rejects arm64 ELF for x86_64", not verify_local_binary(arm64_bin, "x86_64")[0])
check("accepts arm64 ELF for arm64-v8a", verify_local_binary(arm64_bin, "arm64-v8a")[0])

x64_bin = cache_path("x86_64")
make_fake_elf(x64_bin, machine=62)
version_path("x86_64").write_text(RESETPROP_VERSION + "\n", encoding="utf-8")
check("accepts x86_64 ELF for x86_64", verify_local_binary(x64_bin, "x86_64")[0])


# ------------------------------------------------------------------ 2. resetprop-rs fast path
print("== 2. resetprop-rs fast path + soft zygote reload + strict-offline ==")
(STATE / "root-allowed").write_text("1\n")
(STATE / "strict-offline").write_text("1\n")
(STATE / "require-writable-system").write_text("1\n")

mgr = DeviceManager()
dev = mgr.create_device("Google Pixel 8 Pro", android_version="14", name="rp_fast")
check("identity has wifi_mac & bluetooth_mac",
      bool(dev["identity"].get("wifi_mac")) and bool(dev["identity"].get("bluetooth_mac")))

# Also verify non-14 android_version propagates into identity
# (create image dir for Android 12 first)
img12 = paths["sdk"] / "system-images/android-31/google_apis/x86_64"
img12.mkdir(parents=True, exist_ok=True)
(img12 / "system.img").write_bytes(b"fake")
dev12 = mgr.create_device("Samsung Galaxy S24", android_version="12", name="a12_check")
check("android_version=12 propagated into identity",
      dev12["identity"].get("android_version") == "12",
      str(dev12["identity"].get("android_version")))
mgr.delete_device("a12_check")

LOG.write_text("")
launched = mgr.launch_device("rp_fast", enable_frida=False, wait_for_boot=True)
serial = launched["serial"]
saved = mgr.get_device("rp_fast")
report = saved.get("spoof_report") or {}
log_text = LOG.read_text()

check("emulator launched with -writable-system",
      "-writable-system" in log_text, log_text[:300])
check("spoof_report method used resetprop-rs + zygote reload",
      "resetprop-rs" in (report.get("method") or "") and "zygote" in (report.get("method") or ""),
      str(report.get("method")))
check("zero failed props on resetprop fast path",
      report.get("failed") == [],
      str(report.get("failed")))
check("all 22 core props verified",
      len(report.get("verified") or {}) == 22,
      str(len(report.get("verified") or {})))
check("no spoof_error recorded", not saved.get("spoof_error"), str(saved.get("spoof_error")))
check("zero full adb reboots on resetprop fast path",
      f"adb -s {serial} reboot" not in log_text)
check("zygote soft-restart triggered",
      (STATE / f"{serial}.zygote_restarts").exists())
check("ro.product.model matches identity",
      _gp(serial, "ro.product.model") == dev["identity"]["model"],
      _gp(serial, "ro.product.model"))
check("ro.serialno matches identity",
      _gp(serial, "ro.serialno") == dev["identity"]["serial"],
      _gp(serial, "ro.serialno"))
check("ro.build.type spoofed to user",
      _gp(serial, "ro.build.type") == "user",
      _gp(serial, "ro.build.type"))
check("ro.kernel.qemu hidden (0) after zygote reload",
      _gp(serial, "ro.kernel.qemu") == "0",
      _gp(serial, "ro.kernel.qemu"))
check("ro.boot.qemu hidden (0)",
      _gp(serial, "ro.boot.qemu") == "0",
      _gp(serial, "ro.boot.qemu"))
check("partition prop ro.product.system.model matches",
      _gp(serial, "ro.product.system.model") == dev["identity"]["model"],
      _gp(serial, "ro.product.system.model"))
check("verified boot state green",
      _gp(serial, "ro.boot.verifiedbootstate") == "green",
      _gp(serial, "ro.boot.verifiedbootstate"))

mgr.stop_device("rp_fast")
mgr.delete_device("rp_fast")


# ------------------------------------------------------------------ 3. build.prop fallback with strict-offline + require-writable-system
print("== 3. Fallback build.prop path with -writable-system + strict-offline + adb root ==")
x64_bin.unlink(missing_ok=True)
version_path("x86_64").unlink(missing_ok=True)
(STATE / "resetprop-installed").unlink(missing_ok=True)
(STATE / "image-build.prop").unlink(missing_ok=True)
LOG.write_text("")

dev_fb = mgr.create_device("Samsung Galaxy S24 Ultra", android_version="14", name="fb_dev")
launched_fb = mgr.launch_device("fb_dev", enable_frida=False, wait_for_boot=True)
serial_fb = launched_fb["serial"]
saved_fb = mgr.get_device("fb_dev")
report_fb = saved_fb.get("spoof_report") or {}
log_fb = LOG.read_text()

check("fallback passed -writable-system", "-writable-system" in log_fb)
check("fallback ran avbctl disable-verification", "avbctl disable-verification" in log_fb)
check("fallback ran disable-verity", "disable-verity" in log_fb)
check("fallback created backup directory", "mkdir -p /data/local/antidetect-buildprop-backup" in log_fb)
check("fallback verified all props even under strict-offline + adb root",
      report_fb.get("failed") == [] and len(report_fb.get("verified") or {}) >= 22,
      f"method={report_fb.get('method')} failed={report_fb.get('failed')}")
check("fallback method reports build.prop",
      "build.prop" in (report_fb.get("method") or ""),
      str(report_fb.get("method")))

mgr.stop_device("fb_dev")
mgr.delete_device("fb_dev")

# Verify Play Store (rootable=False) does NOT get -writable-system
LOG.write_text("")
dev_ps = mgr.create_device(
    "Google Pixel 8", android_version="14", name="ps_dev", image_variant="google_apis_playstore"
)
mgr.launch_device("ps_dev", enable_frida=False, wait_for_boot=False)
check("playstore device does not pass -writable-system",
      "-writable-system" not in LOG.read_text())
mgr.stop_device("ps_dev")
mgr.delete_device("ps_dev")


# ------------------------------------------------------------------ 4. HookManager + Frida 17 bridge + GC safety
print("== 4. HookManager persistent sessions, Frida 17 bridge & JS safety ==")

# Verify JS scripts syntax + safety invariants
scripts_dir = REPO / "frida_toolkit" / "scripts"
native_js = (scripts_dir / "native_hooks.js").read_text(encoding="utf-8")
anti_js = (scripts_dir / "anti_detect.js").read_text(encoding="utf-8")
safety_js = (scripts_dir / "safetynet_bypass.js").read_text(encoding="utf-8")

check("native_hooks.js uses Frida 17 Process.findModuleByName",
      "Process.findModuleByName" in native_js)
check("native_hooks.js closes cpuinfoFds on close()",
      'findExport("libc.so", "close")' in native_js and "delete cpuinfoFds[fd]" in native_js)
check("native_hooks.js removes global strstr hook",
      '"strstr"' not in native_js)
check("native_hooks.js removes dlopen blocking of libgoldfish/libqemu",
      '"libgoldfish"' not in native_js and '"libqemu"' not in native_js)
open_block = native_js.split("var openHiddenFiles = [")[1].split("];")[0]
check("native_hooks.js does NOT block /dev/qemu_pipe in open()",
      "/dev/qemu_pipe" not in open_block and "/dev/goldfish_pipe" not in open_block)
check("safetynet_bypass.js does NOT hardcode SDK_INT = 34",
      "SDK_INT.value = 34" not in safety_js)
check("anti_detect.js does NOT hardcode Adreno (TM) 660 in GLES20",
      'return "Adreno (TM) 660"' not in anti_js)

sample_id = dev["identity"]
hm_gen = HookManager("emulator-5590")
combined = hm_gen.generate_combined_script(sample_id)
bridged = wrap_with_java_bridge(combined)
check("Frida 17 Java bridge injected into wrapped script",
      'typeof Java === "undefined"' in bridged and 'Object.defineProperty(globalThis, "Java"' in bridged
      and len(bridged) > len(combined) + 50_000,
      f"combined={len(combined)} bridged={len(bridged)}")
check("combined script includes parsed fingerprint buildId",
      '"buildId":' in combined and '"buildDisplayId":' in combined)

for label, src in [
    ("native_hooks.js", native_js),
    ("anti_detect.js", anti_js),
    ("safetynet_bypass.js", safety_js),
    ("combined_hooks.js", combined),
    ("bridged_hooks.js", bridged),
]:
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as tf:
        tf.write(src)
        tf_path = tf.name
    try:
        r = subprocess.run(["node", "--check", tf_path], capture_output=True, text=True)
        check(f"node --check passes on {label}", r.returncode == 0, r.stderr.strip())
    finally:
        Path(tf_path).unlink(missing_ok=True)

# Mock frida module to verify session retention across GC, GMS attach-only, and error capture
events = {"spawned": [], "attached": [], "unloaded": [], "detached": []}


class FakeScript:
    def __init__(self, source, trigger_error=False):
        self.source = source
        self.callbacks = {}
        self.loaded = False
        self.trigger_error = trigger_error

    def on(self, event, cb):
        self.callbacks[event] = cb

    def load(self):
        self.loaded = True
        if self.trigger_error and "message" in self.callbacks:
            self.callbacks["message"]({"type": "error", "description": "ReferenceError: boom"}, None)

    def unload(self):
        events["unloaded"].append(True)


class FakeSession:
    def __init__(self, pid, trigger_error=False):
        self.pid = pid
        self.trigger_error = trigger_error
        self.callbacks = {}
        self.last_script = None

    def on(self, event, cb):
        self.callbacks[event] = cb

    def create_script(self, source):
        self.last_script = FakeScript(source, trigger_error=self.trigger_error)
        return self.last_script

    def detach(self):
        events["detached"].append(self.pid)


class FakeProc:
    def __init__(self, pid, name):
        self.pid = pid
        self.name = name


class FakeDevice:
    id = "emulator-5590"
    gms_running = True
    trigger_error = False

    def enumerate_processes(self):
        procs = [FakeProc(2001, "com.android.settings")]
        if self.gms_running:
            procs.append(FakeProc(2002, "com.google.android.gms"))
        return procs

    def spawn(self, argv):
        pkg = argv[0]
        events["spawned"].append(pkg)
        if pkg == "com.google.android.gms":
            raise RuntimeError("GMS has no launcher activity")
        return 3001

    def attach(self, pid):
        events["attached"].append(pid)
        return FakeSession(pid, trigger_error=self.trigger_error)

    def resume(self, pid):
        pass


fake_dev = FakeDevice()
fake_frida = types.ModuleType("frida")
fake_frida.__version__ = "17.26.3"
fake_frida.get_device = lambda serial, timeout=10: fake_dev
fake_frida.get_usb_device = lambda timeout=10: fake_dev

orig_frida = sys.modules.get("frida")
sys.modules["frida"] = fake_frida
try:
    (STATE / "emulator-5590.state").write_text("device\n")
    (STATE / "emulator-5590.boot").write_text("1\n")
    (STATE / "frida-running").write_text("1\n")

    hm = HookManager("emulator-5590")
    hm._frida_server_started = True
    ok = hm.inject_system_wide(sample_id)
    check("inject_system_wide succeeds with mock frida", ok is True, str(hm.last_error))
    check("Settings spawned and GMS attached (never spawned)",
          events["spawned"] == ["com.android.settings"] and 2002 in events["attached"],
          f"spawned={events['spawned']} attached={events['attached']}")

    # Drop local HookManager reference and force GC -> sessions must remain live!
    del hm
    gc.collect()
    live_pkgs = set(active_sessions_for("emulator-5590"))
    check("sessions survive HookManager GC",
          {"com.android.settings", "com.google.android.gms"} <= live_pkgs,
          str(live_pkgs))
    check("neither session was detached prematurely", not events["detached"])

    # Detach cleanly on stop
    detach_device_sessions("emulator-5590")
    check("detach_device_sessions cleans all live sessions",
          active_sessions_for("emulator-5590") == [] and len(events["detached"]) == 2,
          f"remaining={active_sessions_for('emulator-5590')} detached={events['detached']}")

    # Missing GMS does NOT fail inject_system_wide
    fake_dev.gms_running = False
    hm2 = HookManager("emulator-5590")
    hm2._frida_server_started = True
    ok2 = hm2.inject_system_wide(sample_id)
    check("inject_system_wide still succeeds when GMS is absent", ok2 is True, str(hm2.last_error))
    detach_device_sessions("emulator-5590")

    # JS script error is captured and fails injection honestly
    fake_dev.trigger_error = True
    hm3 = HookManager("emulator-5590")
    hm3._frida_server_started = True
    ok3 = hm3.inject_hooks("com.android.settings", sample_id)
    check("JS script error returns False", ok3 is False)
    check("JS script error surfaces in last_error",
          "ReferenceError: boom" in (hm3.last_error or ""),
          str(hm3.last_error))
    fake_dev.trigger_error = False

    # ------------------------------------------------------------------ 5. GUI API & app.js
    print("== 5. GUI API & app.js fixes ==")
    from gui.app import create_app  # noqa: E402

    (STATE / "emulator-5590.avd").write_text("antidetect_guidev_hk\n")
    (STATE / "frida-installed").write_text("1000000\n")
    (STATE / "frida-version").write_text("17.26.3\n")
    mgr.create_device("Google Pixel 8", android_version="14", name="guidev_hk")
    app = create_app()
    client = app.test_client()

    frida_st = client.get("/api/status/frida", query_string={"serial": "emulator-5590"}).get_json()
    check("/api/status/frida exposes non-null version (fixes vundefined)",
          bool(frida_st.get("version")) and frida_st.get("version") == "17.26.3",
          json.dumps(frida_st)[:300])

    # Mark frida-server cache aligned so start_frida_server doesn't try network download
    from frida_toolkit.frida_server_manager import (  # noqa: E402
        FRIDA_SERVER_PATH,
        FRIDA_SERVER_VERSION_PATH,
        target_version,
    )
    make_fake_elf(FRIDA_SERVER_PATH, machine=62, size=1_100_000)
    FRIDA_SERVER_VERSION_PATH.write_text(target_version() + "\n", encoding="utf-8")

    r_ok = client.post("/api/devices/guidev_hk/apply-frida-hooks")
    b_ok = r_ok.get_json()
    check("apply-frida-hooks returns success=True",
          r_ok.status_code == 200 and b_ok.get("success") is True,
          json.dumps(b_ok))
    saved_hk = mgr.get_device("guidev_hk")
    check("apply-frida-hooks persists hooks_applied=True",
          saved_hk.get("hooks_applied") is True and bool(saved_hk.get("hooks_applied_at")),
          str(saved_hk.get("hooks_applied")))
    detach_device_sessions("emulator-5590")

    # Failure case returns 'error' key and persists hooks_error
    fake_dev.trigger_error = True
    r_err = client.post("/api/devices/guidev_hk/apply-frida-hooks")
    b_err = r_err.get_json()
    check("apply-frida-hooks failure includes 'error' key",
          b_err.get("success") is False and "ReferenceError: boom" in (b_err.get("error") or ""),
          json.dumps(b_err))
    saved_err = mgr.get_device("guidev_hk")
    check("apply-frida-hooks failure persists hooks_error",
          saved_err.get("hooks_applied") is False
          and "ReferenceError: boom" in (saved_err.get("hooks_error") or ""),
          str(saved_err.get("hooks_error")))

    # Cleanup
    mgr.delete_device("guidev_hk")
    FRIDA_SERVER_PATH.unlink(missing_ok=True)
    FRIDA_SERVER_VERSION_PATH.unlink(missing_ok=True)
finally:
    if orig_frida is not None:
        sys.modules["frida"] = orig_frida
    else:
        sys.modules.pop("frida", None)
    detach_device_sessions("emulator-5590")

# Static checks on app.js, frida_server_manager.py, and JS hook overloads
app_js = (REPO / "gui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
r_js = subprocess.run(["node", "--check", str(REPO / "gui" / "static" / "js" / "app.js")],
                      capture_output=True, text=True)
check("gui/static/js/app.js passes node --check", r_js.returncode == 0, r_js.stderr.strip())
check("VERSION_TO_API includes Android 15 (API 35)", "'15': 35" in app_js)
check("app.js no longer renders unguarded (v${data.version})", "(v${data.version})" not in app_js)
check("app.js syncs #spoofStatus in updateStats", "const card = document.getElementById('spoofStatus')" in app_js)
check("app.js refreshes devices inside refreshStatus()",
      "loadDevices()" in app_js.split("async function refreshStatus()", 1)[1].split("async function checkAdbStatus()", 1)[0])
check("app.js displays hooks_error in Details modal", "device.hooks_error" in app_js)

import re as _re  # noqa: E402
for js_name in ("anti_detect.js", "safetynet_bypass.js"):
    js_src = (REPO / "frida_toolkit" / "scripts" / js_name).read_text(encoding="utf-8")
    bare_impl = _re.findall(r"[A-Za-z0-9_]+\.[A-Za-z0-9_]+\.implementation\s*=", js_src)
    check(f"{js_name} has zero bare .implementation without .overload()",
          len(bare_impl) == 0, str(bare_impl))
    check(f"{js_name} hooks Android 13-15 PackageInfoFlags overload",
          "PackageManager$PackageInfoFlags" in js_src)

fsm_src = (REPO / "frida_toolkit" / "frida_server_manager.py").read_text(encoding="utf-8")
check("frida_server_manager uses pkill -x (not pkill -f)",
      "pkill -x frida-server" in fsm_src and "pkill -f frida-server" not in fsm_src)
check("frida_server_manager captures root server log to REMOTE_LOG",
      "nohup {REMOTE_PATH} -D >{REMOTE_LOG} 2>&1 &" in fsm_src)

if failures:
    print(f"\n{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("\nAll resetprop & Frida hook regression checks passed.")
