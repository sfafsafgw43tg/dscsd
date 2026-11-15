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

# Module-level live session registry so Frida sessions/scripts stay alive
# after HookManager methods return (keyed by (serial, package_name)).
_LIVE_SESSIONS: Dict[tuple, dict] = {}
_LIVE_LOCK = threading.Lock()
_JAVA_BRIDGE_CACHE: Optional[str] = None


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

    def generate_combined_script(self, identity: Dict) -> str:
        """Generate a combined Frida script with all hooks and identity values."""
        combined = "// Combined AntiDetect Frida Script\n\n"
        combined += self._generate_config_js(identity)
        combined += "\n\n"

        script_files = [
            "native_hooks.js",
            "safetynet_bypass.js",
            "anti_detect.js",
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
            "glRenderer": str(identity.get("gl_renderer", "Adreno (TM) 740") or "Adreno (TM) 740"),
            "glVendor": str(identity.get("gl_vendor", "Qualcomm") or "Qualcomm"),
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
    ) -> bool:
        """Inject all anti-detection hooks into *package_name*."""
        if not self.start_frida_server():
            print("[HookManager] Failed to start Frida server")
            return False

        script_content = self.generate_combined_script(identity)
        temp_script = Path(config.DATA_DIR) / "combined_hooks.js"
        temp_script.parent.mkdir(parents=True, exist_ok=True)
        temp_script.write_text(script_content, encoding="utf-8")
        if prefer_attach is None:
            prefer_attach = package_name in _ATTACH_ONLY_PACKAGES
        return self._run_script(
            package_name,
            script_content,
            script_file=temp_script,
            prefer_attach=prefer_attach,
            launch_if_stopped=launch_if_stopped,
        )

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

    def _run_script(
        self,
        package_name: str,
        script_content: str,
        script_file=None,
        prefer_attach: bool = False,
        launch_if_stopped: bool = False,
    ) -> bool:
        """Attach/spawn *package_name* with *script_content*, Python API first then CLI."""
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
        """Inject hooks into Settings (launch+attach, zero zygote spawn) and GMS (best-effort attach)."""
        # Launch-and-attach Settings through ActivityManager instead of
        # device.spawn(): keeps zygote64 untouched (combined with frida-server -P)
        # and avoids Android 14 ART spawn-race crashes in com.android.settings.
        settings_ok = self.inject_hooks(
            "com.android.settings",
            identity,
            prefer_attach=True,
            launch_if_stopped=True,
        )
        if not settings_ok:
            detail = self.last_error or "injection failed"
            self.last_error = f"Hooks not applied to com.android.settings ({detail})"
            print(f"[HookManager] {self.last_error}")
            return False

        # Best-effort attach to GMS when it is running (it has no launcher
        # Activity so spawning it always fails, and some images don't run GMS).
        gms_ok = self.inject_hooks(
            "com.google.android.gms",
            identity,
            prefer_attach=True,
            launch_if_stopped=False,
        )
        if not gms_ok:
            print(
                "[HookManager] Note: com.google.android.gms was not attached "
                f"({self.last_error or 'not running'}); com.android.settings is hooked."
            )
            self.last_error = None
        return True

    def inject_package(self, package_name: str, identity: Dict, spawn: bool = False) -> bool:
        """Inject anti-detection hooks into any target package (safe launch+attach by default)."""
        if spawn:
            return self.inject_hooks(package_name, identity, prefer_attach=False)
        return self.inject_hooks(
            package_name,
            identity,
            prefer_attach=True,
            launch_if_stopped=True,
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
