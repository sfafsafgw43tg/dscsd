"""
System Spoofer - Apply system-level identity changes via ADB
This runs after the emulator boots to change properties that can't be set in config.ini
"""
import shlex
import subprocess
import time
import threading
import config


class SystemSpoofer:
    """Apply system-level spoofing after boot"""
    
    def __init__(self, device_serial="emulator-5554"):
        self.device_serial = device_serial
        self.adb = config.ADB_PATH
    
    def _adb(self, command, timeout=30, retry=True):
        """Execute ADB command with one offline-recovery retry."""
        cmd = [self.adb, "-s", self.device_serial] + command
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except Exception:
            return None
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        if result.returncode != 0 and retry:
            combined = f"{out} {err}".lower()
            if (
                "offline" in combined
                or "no devices" in combined
                or ("device '" in combined and "not found" in combined)
                or "closed" in combined
                or "broken pipe" in combined
            ):
                try:
                    from core import adb as adb_util
                    ok, _ = adb_util.recover(self.device_serial, timeout=20)
                    if ok:
                        return self._adb(command, timeout=timeout, retry=False)
                except Exception:
                    pass
        return out

    def _adb_root(self):
        """Ensure adbd runs as root before applying protected properties.

        Delegates to ``core.adb.root``, which is a no-op when the shell is
        already root (the launch-time auto-root ran just before) and which
        waits for the adbd restart + verifies ``uid=0`` otherwise - the old
        fixed 2s sleep raced slow adbd restarts and dropped properties.
        """
        try:
            from core import adb as adb_util
            adb_util.root(self.device_serial, timeout=30)
        except Exception:
            pass
    
    def wait_for_boot(self, timeout=120):
        """Wait for device to fully boot"""
        start = time.time()
        while time.time() - start < timeout:
            result = self._adb(["shell", "getprop", "sys.boot_completed"])
            if result == "1":
                return True
            time.sleep(2)
        return False
    
    def apply_all_spoofing(self, identity):
        """Apply all system-level spoofing"""
        print("[SystemSpoof] Waiting for device boot...")
        if not self.wait_for_boot():
            print("[SystemSpoof] Device boot timeout")
            return False
        
        print("[SystemSpoof] Applying system-level identity...")
        
        # Get root
        self._adb_root()
        
        # Apply properties
        self._apply_build_props(identity)
        self._apply_settings(identity)
        self._hide_emulator_strings()
        
        print("[SystemSpoof] System-level spoofing complete!")
        return True
    
    def _apply_build_props(self, identity):
        """Apply build properties via setprop"""
        props = {
            # Core identity
            "ro.product.model": identity.get("model", "SM-S928B"),
            "ro.product.brand": identity.get("brand", "samsung"),
            "ro.product.manufacturer": identity.get("manufacturer", "samsung"),
            "ro.product.device": identity.get("device", "e2s"),
            "ro.product.name": identity.get("product", identity.get("device", "e2s")),
            "ro.build.fingerprint": identity.get("fingerprint", ""),
            "ro.serialno": identity.get("serial", ""),
            "ro.boot.serialno": identity.get("serial", ""),
            
            # Hide emulator
            # NOTE: NEVER set ro.hardware.egl or ro.hardware.vulkan here!
            # Android's libEGL (Loader.cpp) and libvulkan (driver.cpp) use those
            # properties as exact shared-library filename suffixes
            # (libEGL_<val>.so / vulkan.<val>.so); pointing them at "adreno" or
            # "arm" when the emulator only ships emulation/ranchu crashes
            # RenderThread / Skia in Android 14 apps (e.g. Settings).
            # GPU vendor/renderer spoofing is handled safely in Frida GLES/EGL hooks.
            "ro.kernel.qemu": "0",
            "ro.boot.qemu": "0",
            "ro.hardware": identity.get("board", "qcom"),
            "ro.build.characteristics": "default",
            "ro.build.type": "user",
            "ro.build.tags": "release-keys",

            # Baseband
            "gsm.version.baseband": identity.get("baseband", "unknown"),
            "ro.baseband": identity.get("baseband", "unknown"),

            # Bootloader
            "ro.bootloader": identity.get("bootloader", "unknown"),
            
            # Partition-specific (for newer Android)
            "ro.product.system.model": identity.get("model", "SM-S928B"),
            "ro.product.system.brand": identity.get("brand", "samsung"),
            "ro.product.system.manufacturer": identity.get("manufacturer", "samsung"),
            "ro.product.system.device": identity.get("device", "e2s"),
            "ro.product.vendor.model": identity.get("model", "SM-S928B"),
            "ro.product.vendor.brand": identity.get("brand", "samsung"),
            "ro.product.vendor.device": identity.get("device", "e2s"),
        }
        
        for key, value in props.items():
            if value:
                self._adb(["shell", "setprop", key, value])
        
        print(f"[SystemSpoof] Applied {len(props)} properties")
    
    def _apply_settings(self, identity):
        """Apply settings database changes (shell-quoted for spaces in model names)."""
        device_name = str(identity.get("model", "Galaxy S24 Ultra") or "Galaxy S24 Ultra")
        quoted_name = shlex.quote(device_name)

        self._adb(["shell", f"settings put global device_name {quoted_name}"])
        self._adb(["shell", f"settings put secure bluetooth_name {quoted_name}"])
        self._adb(["shell", f"settings put system device_name {quoted_name}"])

        android_id = str(identity.get("android_id", "") or "")
        if android_id:
            self._adb(["shell", f"settings put secure android_id {shlex.quote(android_id)}"])

        print("[SystemSpoof] Settings applied")

    def _hide_emulator_strings(self):
        """Hide emulator-specific settings entries safely."""
        commands = [
            # Use `delete` instead of `put ... ""`: `adb shell` joins argv
            # with spaces so an empty trailing arg vanished and failed syntax.
            ["shell", "settings delete global emulator_device_name"],
            ["shell", "settings put global development_settings_enabled 0"],
            # NOTE: adb_enabled is intentionally NOT set to 0 here.
            # On an emulator adb is our ONLY management channel: setting
            # adb_enabled=0 stops adbd (the Mainline AdbService observes the
            # setting), the device goes 'offline' for good and even
            # kill-server/reconnect cannot bring it back - the setting also
            # persists across reboots, so the AVD becomes unmanageable until
            # its userdata is wiped. The anti-detection goal (apps checking
            # Settings.Global "adb_enabled") is preserved by the Frida hooks
            # in frida/scripts/anti_detect.js, which spoof the read instead.
        ]
        for cmd in commands:
            self._adb(cmd)


def apply_system_spoofing_async(device_serial, identity, initial_delay=0):
    """Apply system spoofing in background thread.

    ``apply_all_spoofing`` already waits for ``sys.boot_completed``, so no
    arbitrary initial sleep is needed (the old 5s sleep raced slow boots).
    """
    spoofer = SystemSpoofer(device_serial)

    def run():
        if initial_delay:
            time.sleep(initial_delay)
        spoofer.apply_all_spoofing(identity)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread
