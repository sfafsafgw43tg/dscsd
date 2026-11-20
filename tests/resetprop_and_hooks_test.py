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
import shutil
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
check("all 28 core props verified (22 identity + 6 SIM)",
      len(report.get("verified") or {}) == 28,
      str(len(report.get("verified") or {})))
verified_props = report.get("verified") or {}
check("the SIM country is Poland",
      verified_props.get("gsm.sim.operator.iso-country") == "pl",
      str(verified_props.get("gsm.sim.operator.iso-country")))
check("the SIM carrier and numeric are a real Polish pair",
      str(verified_props.get("gsm.operator.numeric", "")).startswith("260")
      and verified_props.get("gsm.operator.alpha") == verified_props.get("gsm.sim.operator.alpha"),
      str({k: v for k, v in verified_props.items() if k.startswith("gsm.")}))
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
check("native_hooks.js spoofs /proc/cpuinfo via memfd_create (zero read() Interceptor hook)",
      'findExport("libc.so", "memfd_create")' in native_js
      and "Interceptor.attach(readPtr" not in native_js
      and "Interceptor.attach(rawReadPtr" not in native_js)
check("native_hooks.js skips all libc hooks inside com.android.settings / com.android.systemui",
      "/proc/self/cmdline" in native_js
      and 'procName === "com.android.settings"' in native_js
      and 'procName === "com.android.systemui"' in native_js)
check("native_hooks.js closes leaked FD and FILE* before hiding open()/fopen()",
      "fcloseFn(retval)" in native_js and "closeFn(leakedFd)" in native_js)
check("native_hooks.js removes hot stat/lstat/getenv/strstr hooks (no ANR)",
      '"strstr"' not in native_js
      and '"getenv"' not in native_js
      and '"stat"' not in native_js
      and '"lstat"' not in native_js)
check("native_hooks.js removes dlopen blocking of libgoldfish/libqemu",
      '"libgoldfish"' not in native_js and '"libqemu"' not in native_js)
hidden_block = native_js.split("var hiddenFiles = [")[1].split("];")[0]
check("native_hooks.js does NOT block /dev/qemu_pipe or /dev/goldfish_pipe in libc",
      "/dev/qemu_pipe" not in hidden_block and "/dev/goldfish_pipe" not in hidden_block)
check("native_hooks.js skips __system_property_get hook when OS props already spoofed",
      "nativeAlreadySpoofed" in native_js)
check("anti_detect.js removes Resources.getString/getText hooks (prevents Settings ANR)",
      'Java.use("android.content.res.Resources")' not in anti_js)
check("anti_detect.js defers Java.perform via setImmediate (prevents spawn stall)",
      "setImmediate(_installAntiDetectHooks)" in anti_js)
check("anti_detect.js removes TextView.setText hook (prevents Android 14 ART GC crashes)",
      'Java.use("android.widget.TextView")' not in anti_js)
check("anti_detect.js fast-skips /data/user/ and /data/app/ in File.exists()",
      'path.indexOf("/data/user/") !== 0' in anti_js
      and 'path.indexOf("/data/app/") !== 0' in anti_js)
check("native_hooks.js sets this.errno = 2 on hidden paths",
      "this.errno = 2" in native_js)
check("safetynet_bypass.js staggers Java.perform via setTimeout to avoid ART race",
      "setTimeout(_runSafetyNetPerform" in safety_js)
check("safetynet_bypass.js uses O(1) packages.toString() fast-path (prevents Settings ANR)",
      "packages.toString()" in safety_js and "Java.cast(item, PackageInfo)" in safety_js)
check("safetynet_bypass.js defers Java.perform via setImmediate",
      "setImmediate(_installSafetyNetHooks)" in safety_js)
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
    unreachable = False

    def enumerate_processes(self):
        if self.unreachable:
            raise RuntimeError("frida-server connection refused")
        procs = [
            FakeProc(2001, "com.android.settings"),
            FakeProc(2003, "com.example.target"),
        ]
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
    check("inject_system_wide NEVER spawns or attaches com.android.settings (prevents Settings crash)",
          events["spawned"] == [] and 2001 not in events["attached"] and 2002 in events["attached"],
          f"spawned={events['spawned']} attached={events['attached']}")

    # Drop local HookManager reference and force GC -> GMS session must remain live!
    del hm
    gc.collect()
    live_pkgs = set(active_sessions_for("emulator-5590"))
    check("sessions survive HookManager GC and exclude com.android.settings",
          "com.google.android.gms" in live_pkgs and "com.android.settings" not in live_pkgs,
          str(live_pkgs))
    check("GMS session was not detached prematurely", not events["detached"])

    # Detach cleanly on stop
    detach_device_sessions("emulator-5590")
    check("detach_device_sessions cleans all live sessions",
          active_sessions_for("emulator-5590") == [] and len(events["detached"]) == 1,
          f"remaining={active_sessions_for('emulator-5590')} detached={events['detached']}")

    # Protected system UI packages are refused by default, and lightweight when forced
    hm_prot = HookManager("emulator-5590")
    hm_prot._frida_server_started = True
    ok_prot = hm_prot.inject_hooks("com.android.settings", sample_id, prefer_attach=True)
    check("inject_hooks refuses protected com.android.settings by default",
          ok_prot is False and "protected" in (hm_prot.last_error or "").lower(),
          str(hm_prot.last_error))
    light_src = hm_prot.generate_combined_script(sample_id, lightweight=True)
    check("lightweight script omits libc/Interceptor/TextView/PackageManager hooks",
          "Interceptor.attach" not in light_src
          and "libc.so" not in light_src
          and "TextView" not in light_src
          and "ApplicationPackageManager" not in light_src
          and "android.os.Build" in light_src)
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as tf:
        tf.write(light_src)
        tf_light = tf.name
    try:
        rl = subprocess.run(["node", "--check", tf_light], capture_output=True, text=True)
        check("node --check passes on lightweight script", rl.returncode == 0, rl.stderr.strip())
    finally:
        Path(tf_light).unlink(missing_ok=True)

    # Forced Settings attach works with lightweight mode, then inject_system_wide detaches it
    ok_force = hm_prot.inject_hooks(
        "com.android.settings", sample_id, prefer_attach=True, allow_protected=True
    )
    check("inject_hooks(allow_protected=True) attaches Settings in lightweight mode",
          ok_force is True and "com.android.settings" in active_sessions_for("emulator-5590"))
    hm_prot.inject_system_wide(sample_id)
    check("inject_system_wide detaches any stale com.android.settings session",
          "com.android.settings" not in active_sessions_for("emulator-5590"),
          str(active_sessions_for("emulator-5590")))
    detach_device_sessions("emulator-5590")

    # Missing GMS does NOT fail inject_system_wide (frida-server still verified ready)
    fake_dev.gms_running = False
    hm2 = HookManager("emulator-5590")
    hm2._frida_server_started = True
    ok2 = hm2.inject_system_wide(sample_id)
    check("inject_system_wide still succeeds when GMS is absent", ok2 is True, str(hm2.last_error))
    detach_device_sessions("emulator-5590")

    # JS script error on a target app is captured and fails injection honestly
    fake_dev.trigger_error = True
    hm3 = HookManager("emulator-5590")
    hm3._frida_server_started = True
    ok3 = hm3.inject_hooks("com.example.target", sample_id, prefer_attach=True)
    check("JS script error returns False", ok3 is False)
    check("JS script error surfaces in last_error",
          "ReferenceError: boom" in (hm3.last_error or ""),
          str(hm3.last_error))
    fake_dev.trigger_error = False

    # Explicit inject_package(spawn=True) still supports spawning when requested
    events["spawned"].clear()
    hm4 = HookManager("emulator-5590")
    hm4._frida_server_started = True
    ok4 = hm4.inject_package("com.example.target", sample_id, spawn=True)
    check("inject_package(spawn=True) spawns target explicitly",
          ok4 is True and events["spawned"] == ["com.example.target"],
          f"ok={ok4} spawned={events['spawned']}")
    detach_device_sessions("emulator-5590")

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
    # Verify _save_device stripped transient keys from on-disk JSON
    raw_json = json.loads((mgr.devices_dir / "guidev_hk.json").read_text(encoding="utf-8"))
    check("_save_device strips transient runtime keys (running/hooks_in_flight)",
          "running" not in raw_json and "hooks_in_flight" not in raw_json,
          str(sorted(raw_json.keys())))
    detach_device_sessions("emulator-5590")

    # New operator endpoints: proxy, hook-app, regenerate-identity, crash-log
    r_px = client.post("/api/devices/guidev_hk/proxy", json={"proxy": "http://user:pass@127.0.0.1:8080/"})
    b_px = r_px.get_json()
    check("/api/devices/<name>/proxy accepts auth+trailing-slash HTTP proxy",
          r_px.status_code == 200 and b_px.get("success") is True,
          json.dumps(b_px))
    r_px_bad = client.post("/api/devices/guidev_hk/proxy", json={"proxy": "ftp://bad:99999"})
    check("/api/devices/<name>/proxy rejects invalid scheme/port with 400",
          r_px_bad.status_code == 400 and r_px_bad.get_json().get("success") is False)
    r_px_clr = client.post("/api/devices/guidev_hk/proxy", json={"proxy": ""})
    check("/api/devices/<name>/proxy clears proxy cleanly",
          r_px_clr.status_code == 200 and r_px_clr.get_json().get("proxy") is None)

    old_imei = mgr.get_device("guidev_hk")["identity"]["imei"]
    r_reg = client.post("/api/devices/guidev_hk/regenerate-identity")
    b_reg = r_reg.get_json()
    new_imei = mgr.get_device("guidev_hk")["identity"]["imei"]
    check("/api/devices/<name>/regenerate-identity generates fresh identity",
          r_reg.status_code == 200 and b_reg.get("success") is True and new_imei != old_imei,
          f"old={old_imei} new={new_imei}")

    r_cl = client.get("/api/devices/guidev_hk/crash-log")
    check("/api/devices/<name>/crash-log returns 200 JSON",
          r_cl.status_code == 200 and r_cl.get_json().get("success") is True)
    r_cl_bad = client.get("/api/devices/guidev_hk/crash-log?lines=notanumber")
    check("/api/devices/<name>/crash-log tolerates non-numeric ?lines",
          r_cl_bad.status_code == 200 and r_cl_bad.get_json().get("success") is True)

    r_ha_prot = client.post("/api/devices/guidev_hk/hook-app", json={"package": "com.android.settings"})
    check("/api/devices/<name>/hook-app blocks protected com.android.settings by default (400)",
          r_ha_prot.status_code == 400 and r_ha_prot.get_json().get("success") is False,
          json.dumps(r_ha_prot.get_json()))

    r_ha = client.post("/api/devices/guidev_hk/hook-app", json={"package": "com.example.target"})
    check("/api/devices/<name>/hook-app hooks target app cleanly",
          r_ha.status_code == 200 and r_ha.get_json().get("success") is True,
          json.dumps(r_ha.get_json()))
    detach_device_sessions("emulator-5590")

    r_ha_force = client.post(
        "/api/devices/guidev_hk/hook-app",
        json={"package": "com.android.settings", "force": True},
    )
    check("/api/devices/<name>/hook-app supports explicit force=true lightweight mode",
          r_ha_force.status_code == 200 and r_ha_force.get_json().get("success") is True,
          json.dumps(r_ha_force.get_json()))
    detach_device_sessions("emulator-5590")

    # Failure case returns 'error' key and persists hooks_error
    fake_dev.unreachable = True
    r_err = client.post("/api/devices/guidev_hk/apply-frida-hooks")
    b_err = r_err.get_json()
    check("apply-frida-hooks failure includes 'error' key",
          b_err.get("success") is False and "connection refused" in (b_err.get("error") or ""),
          json.dumps(b_err))
    saved_err = mgr.get_device("guidev_hk")
    check("apply-frida-hooks failure persists hooks_error",
          saved_err.get("hooks_applied") is False
          and "connection refused" in (saved_err.get("hooks_error") or ""),
          str(saved_err.get("hooks_error")))
    fake_dev.unreachable = False

    # Guard import_system_image when ANDROID_SDK_ROOT is None
    orig_sdk_root = config.ANDROID_SDK_ROOT
    dummy_img_dir = Path(tempfile.mkdtemp(prefix="dummy_sysimg_"))
    try:
        config.ANDROID_SDK_ROOT = None
        (dummy_img_dir / "system.img").write_bytes(b"IMG" * 16)
        r_imp = client.post(
            "/api/system-images/import",
            json={"source_dir": str(dummy_img_dir), "api_level": 34, "tag": "google_apis", "abi": "x86_64"},
        )
        check("import_system_image returns 400 (not 500 TypeError) when ANDROID_SDK_ROOT is None",
              r_imp.status_code == 400 and r_imp.get_json().get("success") is False,
              f"status={r_imp.status_code} body={r_imp.get_json()}")
    finally:
        config.ANDROID_SDK_ROOT = orig_sdk_root
        shutil.rmtree(dummy_img_dir, ignore_errors=True)

    # Cleanup
    mgr.stop_device("guidev_hk")
    mgr.delete_device("guidev_hk")
    FRIDA_SERVER_PATH.unlink(missing_ok=True)
    FRIDA_SERVER_VERSION_PATH.unlink(missing_ok=True)
finally:
    if orig_frida is not None:
        sys.modules["frida"] = orig_frida
    else:
        sys.modules.pop("frida", None)
    detach_device_sessions("emulator-5590")

# ------------------------------------------------------------------ 6. Settings-crash & full-app regression checks
print("== 6. Settings-crash & full-app regression checks ==")
from spoof.system_props import generate_props_config  # noqa: E402
from spoof.battery_spoof import BatterySimulator  # noqa: E402
from spoof.sensor_simulator import SensorSimulator  # noqa: E402
from anonymity.proxy_manager import ProxyManager  # noqa: E402

boot_props_txt = generate_props_config(sample_id, "Samsung", "Samsung Galaxy S24")
for toxic in ("ro.kernel.qemu.gles=", "ro.kernel.qemu=", "ro.boot.qemu=", "ro.hardware=", "ro.boot.hardware="):
    check(f"system_props.py does NOT pass toxic boot -prop {toxic}",
          toxic not in boot_props_txt, boot_props_txt)
check("system_props.py still passes safe identity props",
      "ro.product.model=" in boot_props_txt and "ro.serialno=" in boot_props_txt)

sys_spoof_src = (REPO / "spoof" / "system_spoof.py").read_text(encoding="utf-8")
gpu_spoof_src = (REPO / "spoof" / "gpu_spoof.py").read_text(encoding="utf-8")
check("system_spoof.py does NOT poison ro.hardware.egl / ro.hardware.vulkan",
      '"ro.hardware.egl":' not in sys_spoof_src and '"ro.hardware.vulkan":' not in sys_spoof_src)
check("gpu_spoof.py does NOT poison ro.hardware.egl / ro.hardware.vulkan",
      '"ro.hardware.egl":' not in gpu_spoof_src and '"ro.hardware.vulkan":' not in gpu_spoof_src)

# Simulators expose .stop() and shut down promptly (no 60s sleep hang)
bsim = BatterySimulator("emulator-5590")
ssim = SensorSimulator("emulator-5590")
check("BatterySimulator and SensorSimulator expose .stop()",
      callable(getattr(bsim, "stop", None)) and callable(getattr(ssim, "stop", None)))
bsim.stop()
ssim.stop()

# ProxyManager URL parsing & safe SOCKS behavior
pm = ProxyManager("emulator-5590")
check("ProxyManager parses http with user:pass and trailing slash",
      pm._parse_proxy_url("http://u:p@10.0.0.1:8080/") == ("http", "10.0.0.1", 8080))
check("ProxyManager parses https proxy URL",
      pm._parse_proxy_url("https://proxy.example.com:8443") == ("https", "proxy.example.com", 8443))
check("ProxyManager parses bare host:port as http",
      pm._parse_proxy_url("127.0.0.1:8888") == ("http", "127.0.0.1", 8888))
# SOCKS is applied through Android's global proxy setting (no destructive
# iptables), and a live failure keeps the profile value for the next launch.
check("ProxyManager parses socks5h/socks aliases as socks5",
      pm._parse_proxy_url("socks5h://1.2.3.4:1080") == ("socks5", "1.2.3.4", 1080)
      and pm._parse_proxy_url("socks://1.2.3.4:1080") == ("socks5", "1.2.3.4", 1080))
_socks_src = (REPO / "anonymity" / "proxy_manager.py").read_text(encoding="utf-8")
_socks_body = _socks_src.split("def _set_socks_proxy")[1].split("def clear_proxy")[0]
check("ProxyManager never RUNS iptables for SOCKS",
      "subprocess" not in _socks_body and "iptables -t nat OUTPUT -" not in _socks_body)
# The live switch goes through adb; point it at the fake SDK explicitly so the
# assertion is about the message, not about which adb happened to be on PATH.
pm.adb = str(config.ADB_PATH)
_socks_live = pm.set_proxy("socks5://127.0.0.1:1080")
check("a SOCKS proxy is applied live (no 'relaunch to apply' dead end)",
      _socks_live is True, f"live={_socks_live} err={pm.last_error}")
# ...and when adb cannot apply it, the message must say it waits for a launch.
pm.adb = str(Path(TMP) / "no-such-adb")
_failing = pm.set_proxy("socks5://127.0.0.1:1080")
check("a failed SOCKS live-switch still says it applies on the next launch",
      _failing is False and "next launch" in (pm.last_error or "").lower(),
      f"live={_failing} err={pm.last_error}")

# Stopped devices must not permanently reserve console ports
stopped_dev_path = mgr.devices_dir / "stopped_port_test.json"
stopped_dev_path.write_text(json.dumps({"name": "stopped_port_test", "console_port": 5566}), encoding="utf-8")
try:
    check("_used_console_ports ignores stopped devices (prevents port exhaustion)",
          5566 not in mgr._used_console_ports())
finally:
    stopped_dev_path.unlink(missing_ok=True)

# sdk_setup.py uses config.is_system_image_installed (not bare image_dir.exists()) and avoids JAVA_HOME=""
sdk_setup_src = (REPO / "core" / "sdk_setup.py").read_text(encoding="utf-8")
check("sdk_setup.py checks config.is_system_image_installed (not empty dir)",
      "config.is_system_image_installed(package)" in sdk_setup_src
      and "or image_dir.exists()" not in sdk_setup_src)
check("sdk_setup.py does not inject empty JAVA_HOME",
      'env["JAVA_HOME"] = env.get("JAVA_HOME", "")' not in sdk_setup_src)

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
check("app.js listens for hooks_updated Socket.IO event", "socket.on('hooks_updated'" in app_js)

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
check("frida_server_manager launches with -P -D (--disable-preload) and captures log",
      "nohup {REMOTE_PATH} -P -D >{REMOTE_LOG} 2>&1 &" in fsm_src
      and "running_without_disable_preload" in fsm_src)
check("frida_server_manager stops running server before pushing update (no stale process)",
      "self.stop_server()" in fsm_src.split("def push_to_device", 1)[1].split("def running_without_disable_preload", 1)[0])
check("frida_server_manager does not invoke awk in shell commands (missing on Android Toybox)",
      "| awk" not in fsm_src and "awk '{" not in fsm_src)

hw_src = (REPO / "spoof" / "hardware_spoof.py").read_text(encoding="utf-8")
check("hardware_spoof defers ro.hardware & ro.boot.hardware until after zygote restart",
      '"ro.hardware"' in hw_src.split("_POST_ZYGOTE_KEYS =", 1)[1].splitlines()[0]
      and '"ro.boot.hardware"' in hw_src.split("_POST_ZYGOTE_KEYS =", 1)[1].splitlines()[0])
check("hardware_spoof pins emulator HAL ro.hardware.<class> props before mutating ro.hardware",
      "def _pin_emulator_hal_props" in hw_src and "self._pin_emulator_hal_props(rp_mgr)" in hw_src)
# CarrierConfig breaks when the numeric is not a real MCC/MNC. The historical
# hazard was writing imei[:6] (an IMEI TAC) there. Rather than grepping source
# text (which false-positives on neighbouring code), verify the value the code
# actually produces for a device.
from spoof.hardware_spoof import build_identity_prop_maps  # noqa: E402

_probe_identity = {
    "model": "SM-S928B", "brand": "Samsung", "manufacturer": "Samsung",
    "device": "e2s", "board": "s5e9945", "serial": "R58W12345AB",
    "imei": "354879654321098",
    "fingerprint": "samsung/e2sxxx/e2s:14/UP1A.231005.007/S928BXXS1AXL1:user/release-keys",
    "android_version": "14", "sim_carrier": "Play", "sim_operator_numeric": "26006",
}
_props, _extended = build_identity_prop_maps(_probe_identity)
_sim_numeric = _props.get("gsm.sim.operator.numeric", "")
check("the SIM numeric is the carrier's MCC/MNC, never the IMEI prefix",
      _sim_numeric == "26006" and not _probe_identity["imei"].startswith(_sim_numeric),
      f"{_sim_numeric!r} vs imei {_probe_identity['imei']}")
check("the SIM numeric is a real Polish MCC/MNC (260xx)",
      _sim_numeric.startswith("260") and len(_sim_numeric) == 5, repr(_sim_numeric))
check("SIM alpha and operator alpha name the same carrier",
      _props.get("gsm.sim.operator.alpha") == _props.get("gsm.operator.alpha") == "Play",
      str({k: v for k, v in _props.items() if k.startswith("gsm.")}))
check("hardware_spoof never calls the IPMI getter as a setter",
      'self._shell_root(f"service call iphonesubinfo' not in hw_src)
check("hardware_spoof clears stale com.android.settings cache after zygote reload",
      "/data/user/0/com.android.settings/cache" in hw_src)
check("hardware_spoof & system_spoof shell-quote device_name (supports spaces)",
      "shlex.quote" in hw_src and "shlex.quote" in sys_spoof_src)
check("system_spoof uses settings delete global emulator_device_name",
      "settings delete global emulator_device_name" in sys_spoof_src)

# Functional test of _pin_emulator_hal_props with simulated ranchu /vendor/lib64/hw modules
from spoof.hardware_spoof import HardwareSpoofer  # noqa: E402

class _FakeRpMgr:
    def __init__(self):
        self.applied = {}
    def apply_props(self, mapping):
        self.applied.update(mapping)
        return True, "ok", "resetprop-rs"

hal_spoofer = HardwareSpoofer(device_serial="emulator-5590")
hal_props = {"ro.hardware": "ranchu", "ro.hardware.power": "ranchu"}
hal_spoofer._getprop = lambda k: hal_props.get(k, "")
hal_spoofer._adb_full = lambda args, timeout=15: (
    True,
    "/vendor/lib64/hw/gralloc.ranchu.so\n"
    "/vendor/lib64/hw/vulkan.ranchu.so\n"
    "/vendor/lib64/hw/audio.primary.ranchu.so\n"
    "/vendor/lib64/hw/power.ranchu.so\n"
    "/vendor/lib64/hw/sensors.goldfish.so\n"
    "EGL:emulation\n",
    "",
)
fake_rp = _FakeRpMgr()
pinned = hal_spoofer._pin_emulator_hal_props(fake_rp)
check("_pin_emulator_hal_props pins missing ranchu/goldfish/egl HAL props and preserves existing",
      pinned.get("ro.hardware.gralloc") == "ranchu"
      and pinned.get("ro.hardware.vulkan") == "ranchu"
      and pinned.get("ro.hardware.audio.primary") == "ranchu"
      and pinned.get("ro.hardware.sensors") == "goldfish"
      and pinned.get("ro.hardware.egl") == "emulation"
      and "ro.hardware.power" not in pinned,
      str(pinned))

# Doctor detects known-crashy Frida 17.25.0 / 17.25.1 Android 14 x86_64 SIGSEGV releases
from core import doctor as _doctor  # noqa: E402
import frida_toolkit.frida_server_manager as _fsm  # noqa: E402
_orig_cv = _fsm.client_version
try:
    _fsm.client_version = lambda: "17.25.1"
    doc_bad = _doctor.check_frida_versions()
    check("doctor flags crashy Frida 17.25.1 (frida-core#1280) as error",
          doc_bad["ok"] is False and doc_bad["severity"] == "error" and "17.25.1" in doc_bad["detail"],
          str(doc_bad))
finally:
    _fsm.client_version = _orig_cv

dm_src = (REPO / "core" / "device_manager.py").read_text(encoding="utf-8")
check("device_manager strips legacy toxic boot_props at launch time",
      "_UNSAFE_BOOT_PROPS" in dm_src and '"ro.kernel.qemu.gles"' in dm_src)

# create_device rejects malformed proxy cleanly
bad_proxy_rejected = False
try:
    mgr.create_device("Google Pixel 8", android_version="14", name="bad_px_dev", proxy="ftp://nope:99999")
except ValueError:
    bad_proxy_rejected = True
check("create_device rejects malformed proxy URL cleanly", bad_proxy_rejected)

if failures:
    print(f"\n{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("\nAll resetprop & Frida hook regression checks passed.")
