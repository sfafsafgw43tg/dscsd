"""
Hook Manager - Centralized Frida hook management.

Key reliability guarantees:
- **Persistent session registry** (``_LIVE_SESSIONS``): Python Frida
  ``Session`` and ``Script`` objects are retained at module level so hooks
  stay active after ``inject_hooks()`` / ``apply_all_protections()`` returns
  instead of being immediately detached by Python garbage collection.
- **Frida 17 Java bridge support**: Frida 17 removed the built-in ``Java``
  bridge from GumJS core. When using the Python ``frida`` API, this module
  automatically prepends ``frida_tools/bridges/java.js`` under a
  ``typeof Java === "undefined"`` guard (no-op on Frida 16).
- **Script error visibility**: attaches ``script.on("message", ...)`` to log
  and surface JS runtime errors instead of silently ignoring them.
- **Attach-first for background services**: packages without a launcher
  activity (e.g. ``com.google.android.gms``) are attached by PID instead of
  calling ``device.spawn()`` (which always fails for headless services).
"""
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import config
from core import adb


# Packages that run as headless background services (no launcher Activity ->
# `device.spawn` fails; attach to the running PID instead).
_ATTACH_ONLY_PACKAGES = {"com.google.android.gms"}

# Critical UID-1000 / System UI packages that MUST NOT be auto-hooked with
# Frida. On Android 14+ ART, attaching frida-java-bridge + libc interceptors
# to com.android.settings or com.android.systemui triggers native SIGSEGV /
# ART GC crashes (frida-core#1280, frida-java-bridge#387) and corrupts system
# dashboards. OS-level resetprop-rs + zygote reload + SystemSpoofer already
# spoof Build.*, properties, device_name, and android_id natively for them.
PROTECTED_SYSTEM_PACKAGES = frozenset({
    "com.android.settings",
    "com.android.systemui",
    "com.android.providers.settings",
    "system_server",
})

# Module-level live session registry so Frida sessions/scripts stay alive
# after HookManager methods return (keyed by (serial, package_name)).
_LIVE_SESSIONS: Dict[tuple, dict] = {}
_LIVE_LOCK = threading.Lock()
_JAVA_BRIDGE_CACHE: Optional[str] = None


def _hardware_payload(identity: Dict) -> Dict:
    """Hardware specs for the Frida side, rebuilt from the model when missing.

    Older device records (created before spec spoofing) have no
    ``hardware_specs`` block, so it is derived on the fly from the model -
    never invented for an unknown model, and never randomised per call.
    """
    identity = identity or {}
    specs = identity.get("hardware_specs")
    if isinstance(specs, dict) and specs.get("ram_reported_mb"):
        payload = dict(specs)
    else:
        try:
            from profiles.hardware_specs import hardware_spec

            payload = hardware_spec(
                identity.get("model", ""),
                identity.get("brand", ""),
                real_ram_mb=identity.get("ram_reported_mb") or None,
            )
        except Exception:  # noqa: BLE001 - never let specs break hook generation
            payload = {}

    # Flat mirrors the JS reads directly.
    payload["gpu"] = payload.get("gpu") or identity.get("gl_renderer") or ""
    for key in ("phone_number", "sim_carrier", "sim_operator_numeric"):
        if identity.get(key) and not payload.get(key):
            payload[key] = identity[key]
    payload["carrier"] = identity.get("sim_carrier") or identity.get("operator") or payload.get("carrier", "")
    payload["operator_numeric"] = identity.get("sim_operator_numeric") or payload.get("operator_numeric", "")
    return payload


def _load_java_bridge_source() -> str:
    """Return the Frida 17 Java bridge JS source if ``frida_tools`` provides it."""
    global _JAVA_BRIDGE_CACHE
    if _JAVA_BRIDGE_CACHE is not None:
        return _JAVA_BRIDGE_CACHE
    try:
        import frida_tools  # type: ignore

        bridge_path = Path(frida_tools.__file__).parent / "bridges" / "java.js"
        if bridge_path.is_file():
            _JAVA_BRIDGE_CACHE = bridge_path.read_text(encoding="utf-8")
            return _JAVA_BRIDGE_CACHE
    except Exception:
        pass
    _JAVA_BRIDGE_CACHE = ""
    return ""


def wrap_with_java_bridge(script_content: str) -> str:
    """Prepend Frida 17's ``java.js`` bridge when ``globalThis.Java`` is absent."""
    bridge_src = _load_java_bridge_source()
    if not bridge_src:
        return script_content
    preamble = (
        "// Frida 17 Java bridge bootstrap (no-op on Frida <=16)\n"
        'if (typeof Java === "undefined") {\n'
        "  try {\n"
        "    (function () {\n"
        f"{bridge_src}\n"
        '      Object.defineProperty(globalThis, "Java", {\n'
        "        value: bridge,\n"
        "        enumerable: true,\n"
        "        configurable: true\n"
        "      });\n"
        "    })();\n"
        "  } catch (e) {\n"
        '    console.log("[HookManager] Java bridge bootstrap note: " + e);\n'
        "  }\n"
        "}\n\n"
    )
    return preamble + script_content


def _close_entry(entry: dict):
    """Best-effort teardown of one ``_LIVE_SESSIONS`` entry."""
    if not entry:
        return
    script = entry.get("script")
    session = entry.get("session")
    proc = entry.get("process")
    if script is not None:
        try:
            script.unload()
        except Exception:
            pass
    if session is not None:
        try:
            session.detach()
        except Exception:
            pass
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass
        _terminate_pid(getattr(proc, "pid", None))


def detach_device_sessions(serial: str):
    """Detach all live Frida sessions belonging to *serial*."""
    if not serial:
        return
    to_close = []
    with _LIVE_LOCK:
        for key in list(_LIVE_SESSIONS.keys()):
            if key[0] == serial:
                to_close.append(_LIVE_SESSIONS.pop(key))
    for entry in to_close:
        _close_entry(entry)


def renderer_sessions_for(serial: str, package_name: str) -> List[str]:
    """Return the Chromium renderer/GPU process names hooked for *package_name*."""
    prefix = f"{package_name}:"
    return [pkg for pkg in active_sessions_for(serial) if pkg.startswith(prefix)]


def active_sessions_for(serial: str) -> List[str]:
    """Return the package names currently hooked on *serial*."""
    with _LIVE_LOCK:
        return [pkg for (s, pkg) in _LIVE_SESSIONS.keys() if s == serial]


class HookManager:
    """Centralized management for all Frida anti-detection hooks."""

    def __init__(self, device_serial: Optional[str] = None):
        self.device_serial = device_serial or None
        self.adb = config.ADB_PATH
        self.scripts_dir = Path(__file__).parent.parent / "frida_toolkit" / "scripts"
        self._active_sessions = {}
        self._frida_server_started = False
        self.last_error = None

    def resolve_serial(self) -> Optional[str]:
        """Current target serial, auto-detecting when none was supplied."""
        if self.device_serial and adb.device_state(self.device_serial) == "device":
            return self.device_serial
        detected = adb.detect_serial(preferred=self.device_serial)
        if detected:
            self.device_serial = detected
        return detected

    def _adb(self, command: List[str], timeout: int = 30, _retry: bool = True) -> Optional[str]:
        """Execute ADB command against the resolved device with offline retry."""
        serial = self.resolve_serial()
        if not serial:
            self.last_error = f"No device connected ({adb.describe_devices()})"
            print(f"[HookManager] {self.last_error}")
            return None
        cmd = [self.adb, "-s", serial] + command
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except Exception as e:
            self.last_error = f"adb {' '.join(command)} failed: {e}"
            print(f"[HookManager] ADB error: {e}")
            return None

        output = ((result.stdout or "") + (result.stderr or "")).strip()
        lowered = output.lower()
        if result.returncode != 0 and ("offline" in lowered or "no devices" in lowered):
            if _retry:
                ok, detail = adb.recover(serial)
                print(f"[HookManager] Device was offline - {detail}")
                if ok:
                    return self._adb(command, timeout=timeout, _retry=False)
            self.last_error = f"{serial} is offline: {output}"
            return None
        if result.returncode != 0 and output:
            self.last_error = output
        return output

    def is_device_ready(self) -> bool:
        """Check if device is booted and ready."""
        result = self._adb(["shell", "getprop", "sys.boot_completed"])
        return result == "1"

    def wait_for_device(self, timeout: int = 120) -> bool:
        """Wait for device to be ready."""
        start = time.time()
        while time.time() - start < timeout:
            if self.is_device_ready():
                return True
            time.sleep(2)
        return False

    def is_frida_server_running(self) -> bool:
        """Check if Frida server is running on device."""
        output = self._adb(["shell", "ps", "-A"])
        if output:
            return "frida-server" in output
        return False

    def start_frida_server(self) -> bool:
        """Make sure frida-server is installed *and* running on the device."""
        serial = self.resolve_serial()
        if not serial:
            self.last_error = (
                f"No running emulator matched this device. adb reports: "
                f"{adb.describe_devices(include_avd=True)}"
            )
            print(f"[HookManager] {self.last_error}")
            return False

        if self._frida_server_started and self.is_frida_server_running():
            return True

        ready, state, detail = adb.wait_until_ready(serial, timeout=60)
        if not ready:
            self.last_error = (
                f"{serial} is not usable (state: {state or 'not connected'}; {detail}). "
                "adb says: " + adb.device_listing().replace("\n", " | ")
            )
            print(f"[HookManager] {self.last_error}")
            return False

        try:
            from frida_toolkit.frida_server_manager import FridaServerManager, target_version

            manager = FridaServerManager(serial)
            if manager.is_running():
                remote_ver = manager.remote_version()
                wanted = target_version()
                ver_ok = (not wanted) or (remote_ver == wanted)
                preload_bad = manager.running_without_disable_preload()
                if ver_ok and not preload_bad:
                    self._frida_server_started = True
                    self.last_error = None
                    return True
                if ver_ok and preload_bad:
                    # Same binary version, just needs restart with -P -D
                    ok = manager.start_server()
                    message = "" if ok else (manager.last_error or "Could not restart frida-server with -P")
                else:
                    ok, message = manager.install()
            else:
                ok, message = manager.install()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"Frida install crashed: {type(exc).__name__}: {exc}"
            print(f"[HookManager] {self.last_error}")
            return False

        if ok:
            self._frida_server_started = True
            self.last_error = None
            return True

        self.last_error = manager.last_error or message
        print(f"[HookManager] {self.last_error}")
        return False

    def _push_frida_server(self) -> bool:
        """Push frida-server binary to device (downloads if needed)."""
        try:
            from frida_toolkit.frida_server_manager import FridaServerManager
            manager = FridaServerManager(self.device_serial)
            return manager.push_to_device()
        except Exception as e:
            print(f"[HookManager] Frida server manager error: {e}")
            possible_paths = [
                Path(config.DATA_DIR) / "frida-server",
                Path.home() / "frida-server",
                Path(__file__).parent.parent / "bin" / "frida-server",
            ]
            for path in possible_paths:
                if path.exists():
                    result = subprocess.run(
                        [self.adb, "-s", self.device_serial, "push", str(path), "/data/local/tmp/frida-server"],
                        capture_output=True
                    )
                    return result.returncode == 0
            return False

    def generate_combined_script(self, identity: Dict, lightweight: bool = False) -> str:
        """Generate a combined Frida script with all hooks and identity values.

        When *lightweight* is True (e.g. if a user explicitly forces hooking a
        protected system UI app), only read-only ``Build.*`` and
        ``Settings.Global.device_name`` fields are synced — skipping native
        ``libc.so`` interceptors, ``File.exists``, ``PackageManager``,
        ``BatteryManager``, and ``NetworkInfo`` hooks that destabilize Android 14
        system UI processes.
        """
        combined = "// Combined AntiDetect Frida Script\n\n"
        combined += self._generate_config_js(identity)
        combined += "\n\n"

        if lightweight:
            # For protected system processes (Settings, SystemUI): PURE JAVA ONLY.
            # NO native Interceptor hooks AT ALL - not even __system_property_get.
            # System properties are already spoofed natively by resetprop-rs at
            # boot time (ro.product.model, ro.hardware, ro.build.*, etc.), so
            # Build.java already reads spoofed values from those props. All we
            # need to do is:
            #   1. Set Java Build.* static fields directly (in case Zygote preloaded
            #      them before resetprop ran)
            #   2. Set Settings.Global.device_name so Settings shows the model name
            #   3. Set Settings.Secure/Global adb_enabled
            # We deliberately do NOT load native_hooks.js, gpu_hooks.js, spec_hooks.js,
            # anti_detect.js, or any script that calls Interceptor.attach.
            # Native hooks in Settings cause "keeps stopping" crashes because
            # Settings runs in the same process as the system compositor/SysUI
            # on some Android versions, and any libc interception there is fatal.
            build_sync_js = """// AntiDetect protected-process safe script: Java-only, zero native hooks.
(function () {
  "use strict";

  // Wrap the entire work in setTimeout to defer until the process is fully
  // resumed (spawn-gating holds the process at exec; Java classes are not
  // loaded yet when create_script runs).
  function _run() {
    if (typeof Java === "undefined" || !Java.available) {
      // Retry a bit later
      setTimeout(_run, 500);
      return;
    }
    Java.perform(function () {
      var id = (typeof deviceIdentity !== "undefined" && deviceIdentity) ? deviceIdentity : {};
      var ok = 0;

      // 1. Build.* static field sync
      try {
        var Build = Java.use("android.os.Build");
        var fields = [
          ["MODEL", id.model], ["BRAND", id.brand], ["MANUFACTURER", id.manufacturer],
          ["FINGERPRINT", id.fingerprint], ["DEVICE", id.device], ["BOARD", id.board],
          ["HARDWARE", id.hardware || id.board], ["PRODUCT", id.productName || id.product || id.device],
          ["BOOTLOADER", id.bootloader], ["ID", id.buildId],
          ["DISPLAY", id.buildDisplayId || id.buildId], ["TAGS", id.buildTags || "release-keys"],
          ["TYPE", id.buildType || "user"]
        ];
        for (var i = 0; i < fields.length; i++) {
          try {
            if (fields[i][1] && Build[fields[i][0]]) {
              Build[fields[i][0]].value = fields[i][1];
              ok++;
            }
          } catch (e1) { }
        }
      } catch (e2) { }

      // 2. Settings.Global.device_name = model name
      try {
        var SettingsGlobal = Java.use("android.provider.Settings$Global");
        var ContentResolver = Java.use("android.content.ContentResolver");
        var ActivityThread = Java.use("android.app.ActivityThread");
        var app = ActivityThread.currentApplication();
        if (app && id.model) {
          var resolver = app.getContentResolver();
          try {
            SettingsGlobal.putString(resolver, "device_name", id.model);
          } catch (e3) { }
        }
      } catch (e4) { }

      console.log("[AntiDetect] Protected-process safe hooks applied (" + ok + " Build fields, zero native hooks)");
    });
  }

  // Use setTimeout so the process finishes resuming before we touch Java.
  try { setTimeout(_run, 1500); } catch (e) { _run(); }
})();
"""
            combined += build_sync_js
            return combined

        script_files = [
            "native_hooks.js",
            "safetynet_bypass.js",
            "anti_detect.js",
            "camera_hooks.js",
            # RAM / storage / CPU / ABI / GPU / screen / SIM reported from the
            # selected model instead of the emulator (Settings -> CPU shows
            # "ranchu", "x86_64" and "SwiftShader" without this).
            "spec_hooks.js",
        ]

        for script_name in script_files:
            script_path = self.scripts_dir / script_name
            if script_path.exists():
                with open(script_path, "r", encoding="utf-8") as f:
                    content = f.read()
                    content = self._replace_placeholders(content, identity)
                    # Rename per-file standalone `var config = {` declarations so
                    # concatenation never clobbers the shared `config = deviceIdentity`.
                    stem = script_name.replace(".js", "").replace("-", "_")
                    for decl in ("var config = {", "const config = {", "let config = {"):
                        if decl in content:
                            content = content.replace(decl, f"var config_{stem} = {{", 1)
                            break
                    combined += f"// ========== {script_name} ==========\n"
                    combined += content
                    combined += "\n\n"

        return combined

    def generate_graphics_script(self, identity: Dict) -> str:
        """Config + GL/EGL hooks + prop spoofing for Chromium renderer/GPU processes.

        Renderer processes are where UNMASKED_VENDOR_WEBGL / UNMASKED_RENDERER_WEBGL
        are read from native glGetString, so they need the full native GL/EGL hooks
        AND native prop replacement to hide the host GPU. Renderer processes are
        NOT protected system processes (they belong to apps), so native Interceptor
        hooks are safe there.
        """
        combined = "// AntiDetect graphics-only script (renderer/GPU process)\n\n"
        combined += self._generate_config_js(identity)
        combined += "\n\n"

        # native_hooks.js provides __system_property_get spoofing (ro.hardware,
        # ro.kernel.qemu, ABI list) which renderers may read during GL init.
        # Renderers are NOT protected system processes, so the full native
        # hooks (GL-safe subset - no file hiding needed here) apply.
        for script_name in ("native_hooks.js", "gpu_hooks.js"):
            script_path = self.scripts_dir / script_name
            if script_path.exists():
                content = self._replace_placeholders(
                    script_path.read_text(encoding="utf-8"), identity
                )
                combined += f"// ========== {script_name} ==========\n" + content + "\n"
        return combined

    def _generate_config_js(self, identity: Dict) -> str:
        """Generate JavaScript config object from identity (JSON-escaped)."""
        from spoof.hardware_spoof import parse_fingerprint

        fp = identity.get("fingerprint", "") or ""
        parsed = parse_fingerprint(fp) or {}
        fp_device = parsed.get("device") or identity.get("device", "")
        build_type = parsed.get("build_type") or "user"
        build_tags = parsed.get("build_tags") or "release-keys"
        build_id = parsed.get("build_id") or identity.get("build_id", "")
        incremental = parsed.get("incremental") or ""
        release = parsed.get("release") or str(identity.get("android_version", "") or "")
        product_name = parsed.get("product") or identity.get("product") or fp_device
        flavor = f"{fp_device}-{build_type}" if fp_device else ""
        description = (
            f"{fp_device}-{build_type} {release} {build_id} {incremental} {build_tags}".strip()
            if (fp_device and build_id)
            else ""
        )

        # GPU identity: always generated, and never randomised for a device
        # that already has one (re-applying hooks must not change the GPU).
        try:
            from spoof.gpu_spoof import build_gpu_identity

            gpu = build_gpu_identity(identity)
        except Exception:  # never let a GPU profile break hook generation
            gpu = {
                "gl_renderer": str(identity.get("gl_renderer", "Adreno (TM) 740")),
                "gl_vendor": str(identity.get("gl_vendor", "Qualcomm")),
                "gl_version": str(identity.get("gl_version", "OpenGL ES 3.2 V@0738.0")),
                "egl_vendor": str(identity.get("egl_vendor", identity.get("gl_vendor", "Qualcomm"))),
                "gl_extensions": str(identity.get("gl_extensions", "")),
                "gl_limits": identity.get("gl_limits") or {},
            }

        payload = {
            "imei": str(identity.get("imei", "") or ""),
            "serial": str(identity.get("serial", "") or ""),
            "androidId": str(identity.get("android_id", "") or ""),
            "model": str(identity.get("model", "") or ""),
            "brand": str(identity.get("brand", "") or ""),
            "manufacturer": str(identity.get("manufacturer", "") or ""),
            "fingerprint": str(fp),
            "device": str(identity.get("device", "") or ""),
            "board": str(identity.get("board", "") or ""),
            "product": str(identity.get("product") or identity.get("device", "") or ""),
            "productName": str(product_name or ""),
            "hardware": str(identity.get("board", "") or ""),
            "bootloader": str(identity.get("bootloader", "unknown") or "unknown"),
            "baseband": str(identity.get("baseband", "unknown") or "unknown"),
            "wifiMac": str(identity.get("wifi_mac", "") or ""),
            "bluetoothMac": str(identity.get("bluetooth_mac", "") or ""),
            # Full GPU identity, derived from one chip so the strings can't
            # contradict each other (profiles/gpu_profiles.py).
            **gpu,
            "glRenderer": gpu.get("gl_renderer"),
            "glVendor": gpu.get("gl_vendor"),
            "cpuModel": str(identity.get("cpu_model", "Qualcomm Kryo 585") or "Qualcomm Kryo 585"),
            "cpuHardware": str(identity.get("cpu_hardware", identity.get("board", "qcom")) or "qcom"),
            "buildId": str(build_id),
            "buildDisplayId": str(build_id),
            "buildIncremental": str(incremental),
            "buildRelease": str(release),
            "buildType": str(build_type),
            "buildTags": str(build_tags),
            "buildFlavor": str(flavor),
            "buildDescription": str(description),
            # Real hardware specs of the selected model (profiles/hardware_specs.py):
            # RAM, storage, SoC, cores, ABIs, GPU and screen. spec_hooks.js
            # answers ActivityManager / StatFs / Build.SUPPORTED_ABIS /
            # GLES20 / DisplayMetrics from this block so Settings stops
            # showing the emulator's own numbers.
            "hardware": _hardware_payload(identity),
            # Polish +48 line and the carrier whose prefix matches it.
            "phoneNumber": str(identity.get("phone_number", "") or ""),
            "carrier": str(identity.get("sim_carrier") or identity.get("operator", "") or ""),
            "operatorNumeric": str(identity.get("sim_operator_numeric", "") or ""),
            # Front & back are independent physical sensors (camera_hooks.js).
            "cameras": identity.get("camera_specs") or {},
            "rearCameraModel": str((identity.get("camera_specs") or {}).get("back", {}).get("model", "")),
            "frontCameraModel": str((identity.get("camera_specs") or {}).get("front", {}).get("model", "")),
        }

        return (
            "// Device identity configuration\n"
            f"var deviceIdentity = {json.dumps(payload, indent=4)};\n\n"
            "// Alias for compatibility\n"
            "var config = deviceIdentity;\n"
        )

    def _replace_placeholders(self, content: str, identity: Dict) -> str:
        """Replace {{PLACEHOLDER}} values in script content (JS-escaped)."""
        replacements = {
            "{{IMEI}}": identity.get("imei", ""),
            "{{SERIAL}}": identity.get("serial", ""),
            "{{ANDROID_ID}}": identity.get("android_id", ""),
            "{{MODEL}}": identity.get("model", ""),
            "{{BRAND}}": identity.get("brand", ""),
            "{{MANUFACTURER}}": identity.get("manufacturer", ""),
            "{{FINGERPRINT}}": identity.get("fingerprint", ""),
            "{{DEVICE}}": identity.get("device", ""),
            "{{BOARD}}": identity.get("board", ""),
            "{{PRODUCT}}": identity.get("product", identity.get("device", "")),
            "{{HARDWARE}}": identity.get("board", ""),
            "{{BOOTLOADER}}": identity.get("bootloader", "unknown"),
            "{{BASEBAND}}": identity.get("baseband", "unknown"),
            "{{CPU_MODEL}}": identity.get("cpu_model", "Qualcomm Kryo 585"),
            "{{CPU_HARDWARE}}": identity.get("cpu_hardware", "qcom"),
            "{{CPU_FEATURES}}": identity.get("cpu_features", ""),
        }

        for placeholder, value in replacements.items():
            safe = str(value).replace("\\", "\\\\").replace('"', '\\"')
            content = content.replace(placeholder, safe)

        return content

    def inject_hooks(
        self,
        package_name: str,
        identity: Dict,
        prefer_attach: Optional[bool] = None,
        launch_if_stopped: bool = False,
        allow_protected: bool = False,
    ) -> bool:
        """Inject anti-detection hooks into *package_name*.

        Critical UID-1000 / System UI packages (``com.android.settings``,
        ``com.android.systemui``, ``com.android.providers.settings``,
        ``system_server``) are skipped by default because OS-level
        ``resetprop-rs`` + ``SystemSpoofer`` already spoof them natively and
        Frida injection destabilizes them on Android 14+. Pass
        ``allow_protected=True`` only when explicitly requested, in which case
        a lightweight read-only script is used (zero ``libc``/UI hooks).
        """
        is_protected = package_name in PROTECTED_SYSTEM_PACKAGES
        if is_protected and not allow_protected:
            self.last_error = (
                f"Skipped Frida injection into protected system app {package_name} "
                "(OS-level spoofing already covers it natively; hooking Settings "
                "causes Android 14 crashes)."
            )
            print(f"[HookManager] {self.last_error}")
            return False

        if not self.start_frida_server():
            print("[HookManager] Failed to start Frida server")
            return False

        script_content = self.generate_combined_script(identity, lightweight=is_protected)
        temp_script = Path(config.DATA_DIR) / "combined_hooks.js"
        temp_script.parent.mkdir(parents=True, exist_ok=True)
        temp_script.write_text(script_content, encoding="utf-8")
        if prefer_attach is None:
            prefer_attach = is_protected or (package_name in _ATTACH_ONLY_PACKAGES)
        elif is_protected:
            prefer_attach = True
        return self._run_script(
            package_name,
            script_content,
            script_file=temp_script,
            prefer_attach=prefer_attach,
            launch_if_stopped=launch_if_stopped,
            identity=identity,
        )

    def _verify_frida_ready(self) -> bool:
        """Confirm ``frida-server`` is alive and answering Frida RPC calls."""
        if not self.start_frida_server():
            return False
        try:
            import frida  # type: ignore

            device = self._frida_device(frida, timeout=10)
            device.enumerate_processes()
            self.last_error = None
            return True
        except ImportError:
            if self.is_frida_server_running():
                self.last_error = None
                return True
            self.last_error = "frida-server is not running"
            return False
        except Exception as exc:
            self.last_error = f"frida-server not reachable via Frida API: {exc}"
            print(f"[HookManager] {self.last_error}")
            return False

    def _frida_device(self, frida_module, timeout=10):
        """Get the Frida device for THIS emulator's adb serial."""
        serial = getattr(self, "device_serial", None)
        if serial:
            try:
                return frida_module.get_device(serial, timeout=timeout)
            except Exception:
                pass
        return frida_module.get_usb_device(timeout=timeout)

    def _find_running_pid(self, frida_device, package_name: str) -> Optional[int]:
        """Locate a running PID for *package_name* via Frida or ``adb pidof``."""
        try:
            for proc in frida_device.enumerate_processes():
                if proc.name == package_name:
                    return int(proc.pid)
        except Exception:
            pass
        out = self._adb(["shell", "pidof", package_name], timeout=10)
        if out:
            for token in out.split():
                if token.isdigit():
                    return int(token)
        return None

    def _launch_and_find_pid(
        self, frida_device, package_name: str, timeout: float = 10.0
    ) -> Optional[int]:
        """Return running PID of *package_name*, starting it cleanly via ``am`` if stopped.

        Launching through Android's ``ActivityManager`` and then calling
        ``device.attach(pid)`` avoids Frida's ``device.spawn()`` zygote64
        injection path (which causes ``SIGSEGV``/``SIGBUS`` crashes in
        Android 14 ``com.android.settings`` - frida#1990 / frida#3798).
        """
        pid = self._find_running_pid(frida_device, package_name)
        if pid is not None:
            return pid

        if package_name == "com.android.settings":
            self._adb(
                ["shell", "am", "start", "-W", "-n", "com.android.settings/.Settings"],
                timeout=15,
            )
        else:
            self._adb(
                [
                    "shell",
                    "monkey",
                    "-p",
                    package_name,
                    "-c",
                    "android.intent.category.LAUNCHER",
                    "1",
                ],
                timeout=15,
            )

        deadline = time.time() + timeout
        while time.time() < deadline:
            pid = self._find_running_pid(frida_device, package_name)
            if pid is not None:
                # Let ART + Activity.onCreate settle before attaching Frida
                time.sleep(0.5)
                return pid
            time.sleep(0.3)
        return None

    def capture_crash_log(self, package_name: Optional[str] = None, lines: int = 40) -> str:
        """Return a concise crash summary from Android's ``logcat -b crash`` buffer."""
        raw = self._adb(["logcat", "-d", "-b", "crash", "-t", str(lines)], timeout=10)
        if not raw:
            return ""
        text = raw.strip()
        if not text:
            return ""
        interesting = []
        for line in text.splitlines():
            low = line.lower()
            if any(
                tok in low
                for tok in (
                    "fatal exception",
                    "fatal signal",
                    "abort message",
                    "caused by:",
                    "exception:",
                    "error:",
                    "process:",
                )
            ) or (package_name and package_name in line):
                interesting.append(line.strip())
        if interesting:
            return " | ".join(interesting[-4:])[:400]
        return " | ".join(line.strip() for line in text.splitlines()[-4:])[:400]

    def _cli_device_flag(self):
        serial = getattr(self, "device_serial", None)
        if serial:
            return ["-D", serial]
        return ["-U"]

    _NO_PAUSE_SUPPORTED = None

    @classmethod
    def _cli_resume_flag(cls, frida_bin):
        """``--no-pause`` only when the installed frida-tools supports it."""
        if cls._NO_PAUSE_SUPPORTED is None:
            try:
                proc = subprocess.run(
                    [frida_bin, "--help"], capture_output=True, text=True, timeout=15
                )
                cls._NO_PAUSE_SUPPORTED = "--no-pause" in (
                    (proc.stdout or "") + (proc.stderr or "")
                )
            except Exception:
                cls._NO_PAUSE_SUPPORTED = False
        return ["--no-pause"] if cls._NO_PAUSE_SUPPORTED else []

    # Seconds to wait before re-checking for renderer/GPU processes that only
    # appear once the app actually renders a page (Chrome starts its GPU
    # process lazily, after the first WebGL/Canvas use).
    RENDERER_RETRY_DELAYS = (4.0, 12.0)

    def _schedule_renderer_followup(self, package_name: str, identity: Dict):
        """Re-scan for renderer/GPU processes shortly after injection.

        Chrome-based apps spawn ``<pkg>:privileged_process0`` lazily, so a
        single scan right after attach usually finds nothing. Each retry hooks
        the new processes (idempotent: already-hooked PIDs are skipped).
        """
        def _worker():
            for delay in self.RENDERER_RETRY_DELAYS:
                time.sleep(delay)
                try:
                    self.inject_renderer_processes(package_name, identity)
                except Exception:
                    pass

        try:
            threading.Thread(
                target=_worker, name=f"renderer-followup-{package_name}", daemon=True
            ).start()
        except Exception:
            pass

    def _register_live_session(self, serial: str, package_name: str, entry: dict):
        """Store *entry* in ``_LIVE_SESSIONS``, closing any previous entry for the same key."""
        key = (serial or "", package_name)
        previous = None
        with _LIVE_LOCK:
            previous = _LIVE_SESSIONS.pop(key, None)
            _LIVE_SESSIONS[key] = entry
        if previous is not None:
            _close_entry(previous)
        self._active_sessions[package_name] = entry.get("pid")

    # ------------------------------------------------------------------
    # Hook already-running user apps (browsers, etc.)
    # ------------------------------------------------------------------
    def _hook_running_apps(self, identity):
        """Attach hooks to already-running third-party apps (Chrome, browsers, etc.).

        System packages are handled separately by inject_system_wide. This
        catches apps the user already opened before clicking Start Frida.
        """
        try:
            import frida
            device = self._frida_device(frida, timeout=10)
            if not device:
                return
            # Only hook actual ANDROID APPS (Java processes with dotted package
            # names like com.android.chrome, com.google.android.apps.maps).
            # Native daemons (prng_seeder, lmkd, keystore, audioserver, netd,
            # mediaserver, vold, surfaceflinger, zygote, installd, etc.) are
            # all skipped - they have simple single-word names with no dots
            # or are native services that crash when Frida injects Java hooks.
            # Also skip ALL com.android.* and com.google.android.* system
            # packages except GMS which is handled separately.
            system_names = frozenset({
                "system_server", "zygote", "zygote64", "sh", "adbd",
                "servicemanager", "logd", "vold", "surfaceflinger",
            })
            skip_prefixes = (
                "android.", "com.android.", "com.google.android.",
            )
            hooked = 0
            for proc in device.enumerate_processes():
                name = (proc.name or "").strip()
                if not name or name in system_names:
                    continue
                # Must contain a dot (Java package name) to be an app
                if "." not in name:
                    continue
                # Skip com.google.android.gms (handled separately in inject_system_wide)
                if name == "com.google.android.gms" or name.startswith("com.google.android.gms:"):
                    continue
                # Skip system packages
                if any(name.startswith(p) for p in skip_prefixes):
                    continue
                # Skip renderer suffixes (handled after main process hooks)
                if (":privileged_process" in name or ":sandboxed_process" in name
                        or name.endswith("_renderer") or name.endswith(":gpu_process")):
                    continue
                # Skip already-hooked processes
                key = (self.device_serial or "", name)
                with _LIVE_LOCK:
                    if key in _LIVE_SESSIONS:
                        continue
                # Try to attach to the main app process (full hooks, not lightweight)
                try:
                    self.inject_hooks(
                        name, identity,
                        prefer_attach=True,
                        launch_if_stopped=False,
                        allow_protected=False,
                    )
                    hooked += 1
                except Exception as e:
                    pass  # Best effort - some processes don't take hooks
            if hooked:
                print(f"[HookManager] Attached to {hooked} already-running app(s)")
        except Exception as e:
            print(f"[HookManager] Could not enumerate running apps: {e}")

    # ------------------------------------------------------------------
    # Spawn gating: auto-hook every app the user opens
    # ------------------------------------------------------------------
    _spawn_gating_enabled = False
    _spawn_gating_identity = None

    def _enable_spawn_gating(self, identity: Dict):
        """Enable Frida spawn gating so every new app process gets hooked automatically.

        When enabled, ANY app the user launches (Chrome, browsers, banking apps,
        games) has the full combined anti-detect script injected at spawn time,
        including native_hooks.js (prop/file hiding), anti_detect.js (Java APIs),
        gpu_hooks.js (GL/EGL) and spec_hooks.js (RAM/GPU/Build/ABI). Chromium
        renderer processes (<pkg>:privileged_process0) are followed up with the
        GPU-only script so WebGL shows the spoofed GPU instead of SwiftShader.
        """
        if self._spawn_gating_enabled:
            # Update identity if gating is already running
            self._spawn_gating_identity = identity
            return
        self._spawn_gating_identity = identity

        try:
            import frida
            device = self._frida_device(frida, timeout=10)
            if not device:
                print("[HookManager] Could not get Frida device for spawn gating")
                return

            serial = self.device_serial

            def _on_spawned(spawn_data):
                try:
                    pid = spawn_data.pid if hasattr(spawn_data, "pid") else spawn_data.get("pid")
                    pkg = (spawn_data.identifier if hasattr(spawn_data, "identifier")
                           else spawn_data.get("identifier", ""))
                    if not pid or not pkg:
                        try:
                            device.resume(pid)
                        except Exception:
                            pass
                        return

                    # Native daemons / core system processes: never hook (no Java VM).
                    # These have simple single-word names (no dots) or are zygote/system_server.
                    native_daemons = frozenset({
                        "system_server", "zygote", "zygote64", "zygote32",
                        "sh", "adbd", "servicemanager", "logd", "vold", "surfaceflinger",
                        "prng_seeder", "lmkd", "keystore", "tombstoned", "dhcpclient",
                        "statsd", "credstore", "gpuservice", "drmserver", "traced_probes",
                        "mdnsd", "traced", "idmap2d", "incidentd", "installd",
                        "netd", "wificond", "audioserver", "cameraserver", "mediaserver",
                        "storaged", "apexd", "logcatd", "mediadrmserver", "mediaextractor",
                        "media.codec", "media.swcodec", "media.metrics", "gatekeeperd",
                        "android.hardware.audio@4.0-service-mediatek",
                        "android.hardware.graphics.composer", "goldfish-logcat",
                        "qemu-props", "libgoldfish-rild", "iptables-restore",
                        "ip6tables-restore", "wpa_supplicant", "hostapd",
                        "webview_zygote", "webview_zygote32", "webview_zygote64",
                        "usap32", "usap64",
                    })
                    # Packages that get LIGHTWEIGHT safe hooks (Build.* only, no native/libc/GL).
                    # These are UI/system packages that need spoofed Build values but crash
                    # if we inject the full script.
                    lightweight_packages = frozenset({
                        "com.android.settings",
                        "com.android.systemui",
                        "com.android.systemui:tuner",
                    })
                    # Packages we DO NOT hook at all (GMS handled separately, providers crash,
                    # Play Store has integrity checks, shell/defcontainer are non-Java).
                    do_not_hook = (
                        "com.android.providers.settings",
                        "com.android.providers.downloads",
                        "com.android.providers.media",
                        "com.android.providers.contacts",
                        "com.android.providers.calendar",
                        "com.android.phone",
                        "com.android.nfc",
                        "com.android.bluetooth",
                        "com.android.se",
                        "com.android.keychain",
                        "com.android.vending",
                        "com.google.android.gms",
                        "com.google.android.gsf",
                        "com.google.android.webview",
                        "com.android.webview",
                        "com.android.shell",
                        "com.android.defcontainer",
                        "com.android.externalstorage",
                        "com.google.android.gms.persistent",
                        "com.google.android.gms.unstable",
                        "com.google.process.gservices",
                        "com.google.process.gapps",
                    )
                    is_native = ("." not in pkg) or (pkg in native_daemons)
                    is_lw = pkg in lightweight_packages
                    is_skip = (pkg in do_not_hook
                               or pkg.startswith("android.")
                               or (pkg.startswith("com.android.") and not is_lw)
                               or (pkg.startswith("com.google.android.") and pkg not in lightweight_packages))
                    # Native daemons and skip packages: resume without hooking.
                    if is_native or is_skip:
                        try:
                            device.resume(pid)
                        except Exception:
                            pass
                        return

                    # Renderer processes (<pkg>:privileged_process0, :sandboxed_processN)
                    # get the GPU-only script; they are re-attached via
                    # inject_renderer_processes after the main process is hooked.
                    is_renderer_suffix = (
                        ":privileged_process" in pkg or
                        ":sandboxed_process" in pkg or
                        pkg.endswith("_renderer") or
                        pkg.endswith(":gpu_process")
                    )

                    thread = threading.Thread(
                        target=self._spawn_inject_worker,
                        args=(device, serial, pid, pkg, identity, is_lw, is_renderer_suffix),
                        daemon=True,
                        name=f"spawn-inject-{pkg}-{pid}",
                    )
                    thread.start()
                except Exception as e:
                    print(f"[HookManager] Spawn gating error for {pkg}: {e}")
                    try:
                        device.resume(pid)
                    except Exception:
                        pass

            device.on("spawn-added", _on_spawned)
            try:
                device.enable_spawn_gating()
                self._spawn_gating_enabled = True
                print("[HookManager] Spawn gating enabled (all new apps will be hooked)")
            except Exception as e:
                print(f"[HookManager] Could not enable spawn gating: {e}")
        except ImportError:
            print("[HookManager] Python frida module not available for spawn gating; "
                  "apps opened manually will not be auto-hooked.")
        except Exception as e:
            print(f"[HookManager] Spawn gating setup failed: {e}")

    def _spawn_inject_worker(self, device, serial, pid, pkg, identity, is_protected, is_renderer):
        """Inject the appropriate script into a spawned process, then resume it."""
        try:
            # Skip if already hooked
            key = (serial or "", pkg)
            with _LIVE_LOCK:
                if key in _LIVE_SESSIONS:
                    try:
                        device.resume(pid)
                    except Exception:
                        pass
                    return

            session = device.attach(pid)
            use_identity = identity or self._spawn_gating_identity or {}

            if is_renderer:
                # Chromium renderer/GPU process: GL/EGL hooks only
                script_content = self._graphics_only_script(use_identity)
                mode_label = "GPU/renderer"
            elif is_protected:
                # Protected system process: lightweight safe script (props only)
                script_content = self.generate_combined_script(use_identity, lightweight=True)
                mode_label = "lightweight-safe"
            else:
                # Regular app: full anti-detect script
                script_content = self.generate_combined_script(use_identity, lightweight=False)
                mode_label = "full"

            from core.hook_manager import wrap_with_java_bridge
            runtime_source = wrap_with_java_bridge(script_content)
            script = session.create_script(runtime_source)

            script_errors = []
            def _on_msg(message, _data):
                if message.get("type") == "error":
                    desc = message.get("description") or str(message)
                    script_errors.append(desc)

            script.on("message", _on_msg)
            script.load()

            if script_errors:
                print(f"[HookManager:{pkg}] Spawn inject error: {script_errors[0][:120]}")
                _close_entry({"session": session, "script": script})
                device.resume(pid)
                return

            self._register_live_session(serial or "", pkg, {
                "device": device,
                "session": session,
                "script": script,
                "pid": pid,
                "mode": f"spawn-gating-{mode_label}",
            })
            device.resume(pid)
            print(f"[HookManager:{pkg}] Auto-hooked on spawn ({mode_label})")

            # For main app processes (not renderers), schedule renderer follow-up
            if not is_renderer and not is_protected:
                self._schedule_renderer_followup(pkg, use_identity)

        except Exception as e:
            print(f"[HookManager:{pkg}] Spawn inject failed: {e}")
            try:
                device.resume(pid)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Chromium renderer / GPU processes
    # ------------------------------------------------------------------
    # Chrome-based apps (browsers, WebView-based apps, Instagram/TikTok
    # in-app browsers) render WebGL in a SEPARATE sandboxed process named
    # ``<package>:privileged_processN`` (older: ``:sandboxed_processN``). That
    # process is where UNMASKED_VENDOR_WEBGL / UNMASKED_RENDERER_WEBGL come
    # from - i.e. the "gpu:" row of a WebGL fingerprint - so spoofing only the
    # app's main process left the host GPU visible
    # ("Android Emulator OpenGL ES Translator (AMD Radeon RX 6700 XT)").
    RENDERER_PROCESS_LIMIT = 8

    def find_renderer_processes(self, package_name: str, frida_device=None) -> List[dict]:
        """Return ``[{name, pid}]`` for the Chromium renderer/GPU processes of *package_name*.

        Matches on the app's package prefix AND a renderer-ish suffix, so other
        apps' sandboxed processes (or an unrelated ``:remote`` service) are
        never touched.
        """
        prefix = f"{package_name}:"
        suffix_hints = ("privileged_process", "sandboxed_process", "renderer", "gpu", "gpu_process")
        found: List[dict] = []
        procs = []
        if frida_device is not None:
            try:
                procs = [{"name": p.name, "pid": int(p.pid)}
                         for p in frida_device.enumerate_processes()]
            except Exception:
                procs = []
        if not procs:
            # Fallback: ps -A (works on every Android version we support).
            out = self._adb(["shell", "ps", "-A", "-o", "PID,NAME"], timeout=10) or ""
            if not out.strip():
                out = self._adb(["shell", "ps"], timeout=10) or ""
            for line in out.splitlines():
                parts = line.split()
                if len(parts) < 2 or not parts[0].strip().isdigit():
                    continue
                procs.append({"name": parts[-1].strip(), "pid": int(parts[0])})
        for proc in procs:
            name = str(proc.get("name") or "")
            if not name.startswith(prefix):
                continue
            if any(hint in name for hint in suffix_hints):
                found.append({"name": name, "pid": int(proc["pid"])})
        return found[: self.RENDERER_PROCESS_LIMIT]

    def inject_renderer_processes(self, package_name: str, identity: Dict) -> int:
        """Inject the combined script into *package_name*'s renderer/GPU processes.

        Returns the number of processes successfully hooked. Best effort: a
        browser that has not created its GPU process yet simply returns 0.
        """
        try:
            import frida  # type: ignore

            device = self._frida_device(frida, timeout=10)
        except Exception:
            return 0

        targets = self.find_renderer_processes(package_name, frida_device=device)
        if not targets:
            return 0

        # The renderer process gets only the GL/EGL hooks: the full combined
        # script's native layer bails out early in sandboxed processes (by
        # design, to protect system UIs), and libc/Java identity hooks are
        # pointless there - WebGL only needs the GPU strings.
        script_content = wrap_with_java_bridge(self.generate_graphics_script(identity))
        hooked = 0
        serial_key = self.resolve_serial() or self.device_serial or ""
        for target in targets:
            with _LIVE_LOCK:
                if (serial_key, target["name"]) in _LIVE_SESSIONS:
                    continue  # already hooked by an earlier pass
            try:
                session = device.attach(target["pid"])
                script = session.create_script(script_content)
                script.on("message", lambda *_a: None)
                script.load()
                self._register_live_session(
                    serial_key,
                    target["name"],
                    {
                        "device": device,
                        "session": session,
                        "script": script,
                        "pid": target["pid"],
                        "mode": "python",
                        "renderer": True,
                    },
                )
                hooked += 1
                print(
                    f"[HookManager] Hooks active in {target['name']} "
                    f"(pid={target['pid']}, GPU/WebGL process)"
                )
            except Exception as exc:
                print(
                    f"[HookManager] Could not hook {target['name']} "
                    f"(pid={target['pid']}): {exc}"
                )
        return hooked

    def _run_script(
        self,
        package_name: str,
        script_content: str,
        script_file=None,
        prefer_attach: bool = False,
        launch_if_stopped: bool = False,
        identity: Optional[Dict] = None,
    ) -> bool:
        """Attach/spawn *package_name* with *script_content*, Python API first then CLI.

        *identity* is only needed to build the graphics-only script for the
        Chromium renderer/GPU processes once the main process is hooked.
        """
        serial = self.resolve_serial() or self.device_serial or ""

        # Prefer the Python Frida API (no PATH dependency, retains live session
        # references in _LIVE_SESSIONS, injects Frida 17 Java bridge).
        try:
            import frida  # type: ignore

            device = self._frida_device(frida, timeout=10)
            spawned = False
            pid = None

            if prefer_attach:
                if launch_if_stopped:
                    pid = self._launch_and_find_pid(device, package_name, timeout=10.0)
                else:
                    pid = self._find_running_pid(device, package_name)
                if pid is None:
                    self.last_error = f"{package_name} is not currently running"
                    print(f"[HookManager] {self.last_error} (skipping attach-only service)")
                    return False
                session = device.attach(pid)
            else:
                try:
                    pid = device.spawn([package_name])
                    spawned = True
                    session = device.attach(pid)
                except Exception as spawn_exc:
                    # Fallback: if spawn failed (e.g. non-launchable package), try attach
                    pid = self._find_running_pid(device, package_name)
                    if pid is None:
                        raise spawn_exc
                    session = device.attach(pid)

            runtime_source = wrap_with_java_bridge(script_content)
            script = session.create_script(runtime_source)
            script_errors: List[str] = []
            detached_reasons: List[str] = []

            def _on_message(message, _data):
                mtype = message.get("type")
                if mtype == "error":
                    desc = message.get("description") or str(message)
                    script_errors.append(desc)
                    print(f"[HookManager:{package_name}] JS error: {desc}")

            script.on("message", _on_message)

            def _on_detached(reason, *_args):
                detached_reasons.append(str(reason))
                key = (serial, package_name)
                with _LIVE_LOCK:
                    cur = _LIVE_SESSIONS.get(key)
                    if cur and cur.get("session") is session:
                        _LIVE_SESSIONS.pop(key, None)
                print(f"[HookManager:{package_name}] Session detached ({reason})")

            try:
                session.on("detached", _on_detached)
            except Exception:
                pass

            script.load()
            if spawned:
                device.resume(pid)

            # Yield briefly (~0.6s) in 0.1s slices so synchronous top-level JS
            # errors or async setImmediate(Java.perform) crashes surface before
            # we claim success.
            for _ in range(6):
                if script_errors or detached_reasons:
                    break
                time.sleep(0.1)

            if script_errors:
                self.last_error = f"Frida script error in {package_name}: {script_errors[0]}"
                _close_entry({"session": session, "script": script})
                return False
            if detached_reasons:
                crash_hint = self.capture_crash_log(package_name, lines=40)
                detail = f"{package_name} detached ({detached_reasons[0]})"
                if crash_hint:
                    detail += f" — {crash_hint}"
                self.last_error = detail
                return False

            self._register_live_session(
                serial,
                package_name,
                {
                    "device": device,
                    "session": session,
                    "script": script,
                    "pid": pid,
                    "mode": "python",
                },
            )
            mode_label = "spawned" if spawned else f"attached pid={pid}"
            print(
                f"[HookManager] Hooks active in {package_name} ({mode_label}) "
                f"on {getattr(device, 'id', serial or 'usb device')} (python API)"
            )
            # WebGL/GPU strings are read in the Chromium renderer process, not
            # the app's main process - hook those too (best effort).
            renderer_count = self.inject_renderer_processes(package_name, identity or {})
            if renderer_count:
                print(f"[HookManager] {renderer_count} renderer/GPU process(es) hooked "
                      f"for {package_name} (WebGL vendor/renderer)")
            if identity:
                self._schedule_renderer_followup(package_name, identity)
            return True
        except ImportError:
            pass
        except Exception as e:
            self.last_error = f"Frida python API: {e}"
            print(f"[HookManager] Python Frida API failed ({e}); trying CLI fallback")

        # CLI fallback (when Python `frida` is unavailable or failed to connect)
        if prefer_attach and not launch_if_stopped:
            # Don't try `-f` spawn on attach-only services
            return False

        log_path = Path(config.DATA_DIR) / "frida_inject.log"
        if script_file is None:
            script_file = Path(config.DATA_DIR) / "combined_hooks.js"
            script_file.parent.mkdir(parents=True, exist_ok=True)
            script_file.write_text(script_content, encoding="utf-8")

        try:
            frida_bin = "frida.exe" if os.name == "nt" else "frida"
            if shutil_which(frida_bin) is None:
                print("[HookManager] Frida not found. Install with: pip install frida-tools")
                return False
            if prefer_attach and launch_if_stopped:
                pid = self._launch_and_find_pid(None, package_name, timeout=10.0)
                if pid is None:
                    self.last_error = f"Could not start {package_name} for attach"
                    return False
                cmd = [
                    frida_bin,
                    *self._cli_device_flag(),
                    "-l",
                    str(script_file),
                    "-p",
                    str(pid),
                ]
            else:
                cmd = [
                    frida_bin,
                    *self._cli_device_flag(),
                    "-l",
                    str(script_file),
                    "-f",
                    package_name,
                    *self._cli_resume_flag(frida_bin),
                ]
            log_file = open(log_path, "wb", buffering=0)
            process = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT)
            time.sleep(2)
            if process.poll() is not None:
                tail = ""
                try:
                    log_file.flush()
                    tail = log_path.read_text(encoding="utf-8", errors="replace")[-400:]
                except Exception:
                    pass
                print(
                    f"[HookManager] Frida CLI exited with code "
                    f"{process.returncode}: {tail.strip()}"
                )
                self.last_error = (
                    f"Frida CLI exited with code {process.returncode}: "
                    f"{tail.strip()[-200:]}"
                )
                return False
            self._register_live_session(
                serial,
                package_name,
                {"process": process, "pid": process.pid, "mode": "cli"},
            )
            print(
                f"[HookManager] Hooks injected into {package_name} "
                f"(CLI {' '.join(self._cli_device_flag())}, log: {log_path})"
            )
            return True
        except FileNotFoundError:
            print("[HookManager] Frida not found. Install with: pip install frida-tools")
            return False
        except Exception as e:
            self.last_error = f"Injection error: {e}"
            print(f"[HookManager] Injection error: {e}")
            return False

    def inject_system_wide(self, identity: Dict) -> bool:
        """Start & verify Frida server and hook targets including Settings (safe mode).

        Protected system packages (Settings, SystemUI) get a SAFE lightweight
        script that includes GPU vendor/renderer spoofing, RAM/storage/ABI/spec
        hooks, and Build.* sync - but NOT the crash-prone File.exists,
        PackageManager, Runtime.exec, or access/fopen file-hiding hooks.
        native_hooks.js auto-detects protected processes and skips dangerous
        libc interceptors there while still spoofing /proc/cpuinfo and
        /proc/meminfo safely.
        """
        if not self._verify_frida_ready():
            detail = self.last_error or "frida-server could not be reached"
            self.last_error = f"Frida server not ready ({detail})"
            print(f"[HookManager] {self.last_error}")
            return False

        # Detach any stale Frida sessions on protected packages before
        # re-attaching with the current safe script.
        for protected_pkg in PROTECTED_SYSTEM_PACKAGES:
            self.stop_session(protected_pkg)

        # Best-effort attach to GMS when it is already running (headless service;
        # never spawn it and never fail if GMS is absent or refuses attach).
        gms_ok = self.inject_hooks(
            "com.google.android.gms",
            identity,
            prefer_attach=True,
            launch_if_stopped=False,
        )

        # Hook Settings and SystemUI with the SAFE lightweight script so they
        # show spoofed GPU renderer/vendor, RAM, storage, ABIs, CPU info, and
        # phone number/carrier instead of "Android Emulator OpenGL ES Translator",
        # "ranchu", "x86_64", etc.
        settings_results = []
        for protected_pkg in ("com.android.settings", "com.android.systemui"):
            try:
                ok = self.inject_hooks(
                    protected_pkg,
                    identity,
                    prefer_attach=True,
                    launch_if_stopped=False,
                    allow_protected=True,  # use lightweight safe script
                )
                settings_results.append((protected_pkg, ok))
            except Exception as exc:
                print(f"[HookManager] Could not attach safe hooks to {protected_pkg}: {exc}")
                settings_results.append((protected_pkg, False))

        settings_hooked = [pkg for pkg, ok in settings_results if ok]
        if not gms_ok:
            print(
                "[HookManager] Note: com.google.android.gms was not attached "
                f"({self.last_error or 'not running'}); frida-server is ready."
            )
            self.last_error = None
        # --- Hook already-running user apps (browser, etc.) ---
        # If the user opened Chrome or another app before clicking Start Frida,
        # those processes are already running and won't be caught by spawn
        # gating. Enumerate them and attach now.
        self._hook_running_apps(identity)

        # --- Spawn gating: automatically hook ALL apps the user opens ---
        # Without this, only Settings/SystemUI/GMS get spoofed. Any browser,
        # banking app, or game the user launches still sees "Android Emulator
        # OpenGL ES Translator" / SwiftShader / x86_64 because no Frida hooks
        # are attached to it. enable_spawn_gating() + on('spawn-add') hooks
        # every new app process with the full script, including Chromium
        # renderer processes (WebGL runs in <pkg>:privileged_process0).
        self._enable_spawn_gating(identity)
        if settings_hooked:
            print(
                f"[HookManager] frida-server ready; safe hooks applied to "
                f"{', '.join(settings_hooked)} (Settings/SystemUI show spoofed GPU/specs)."
            )
        else:
            print(
                "[HookManager] frida-server ready (Settings/SystemUI not running yet; "
                "they will be hooked when opened, or safe hooks can be applied manually)."
            )
        print("[HookManager] Spawn gating enabled - apps will be hooked automatically when opened.")
        return True

    def inject_package(
        self,
        package_name: str,
        identity: Dict,
        spawn: bool = False,
        allow_protected: bool = False,
    ) -> bool:
        """Inject anti-detection hooks into any target package (safe launch+attach by default)."""
        if spawn:
            return self.inject_hooks(
                package_name,
                identity,
                prefer_attach=False,
                allow_protected=allow_protected,
            )
        return self.inject_hooks(
            package_name,
            identity,
            prefer_attach=True,
            launch_if_stopped=True,
            allow_protected=allow_protected,
        )

    def spawn_with_hooks(self, package_name: str, identity: Dict) -> bool:
        """Spawn *package_name* with Frida hooks attached from start."""
        return self.inject_hooks(package_name, identity, prefer_attach=False)

    def stop_session(self, package_name: str):
        """Stop active Frida session for *package_name*."""
        serial = self.device_serial or ""
        entry = None
        with _LIVE_LOCK:
            entry = _LIVE_SESSIONS.pop((serial, package_name), None)
        if entry is not None:
            _close_entry(entry)
        elif package_name in self._active_sessions:
            _terminate_pid(self._active_sessions[package_name])
        self._active_sessions.pop(package_name, None)

    def stop_all_sessions(self):
        """Stop all active Frida sessions for this manager's device."""
        if self.device_serial:
            detach_device_sessions(self.device_serial)
        for package in list(self._active_sessions.keys()):
            self.stop_session(package)

    def get_available_scripts(self) -> List[str]:
        """Get list of available Frida scripts."""
        scripts = []
        if self.scripts_dir.exists():
            for script in self.scripts_dir.glob("*.js"):
                scripts.append(script.name)
        return scripts

    def apply_all_protections(self, identity: Dict) -> bool:
        """Apply all anti-detection protections."""
        print("[HookManager] Applying all anti-detection protections...")

        if not self.wait_for_device(timeout=60):
            print("[HookManager] Device not ready")
            return False

        if not self.start_frida_server():
            print("[HookManager] Could not start Frida server")
            return False

        if not self.inject_system_wide(identity):
            print("[HookManager] Could not inject hooks - see last_error")
            return False

        print("[HookManager] All protections applied!")
        return True


def shutil_which(name):
    return shutil.which(name)


def _terminate_pid(pid):
    """Terminate a local process, portably."""
    if not pid:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/F"],
                capture_output=True,
                timeout=10,
            )
        else:
            os.kill(int(pid), 9)
    except Exception:
        pass


def apply_antidetect_hooks(device_serial: str, identity: Dict, async_mode: bool = True):
    """Convenience function to apply all antidetect hooks."""
    manager = HookManager(device_serial)
    if async_mode:
        thread = threading.Thread(
            target=manager.apply_all_protections,
            args=(identity,),
            daemon=True,
        )
        thread.start()
        return thread
    return manager.apply_all_protections(identity)
