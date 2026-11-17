"""
Device Manager - Create, launch, and manage emulator devices.

Launch-path fixes (previously the emulator could never start reliably):
- SDK paths are resolved cross-platform via ``config`` (no hardcoded
  ``C:\\Users\\admin\\...``), with clear errors when the SDK is missing.
- ``avdmanager``/``sdkmanager`` ``.bat`` launchers are invoked correctly
  on Windows (``cmd /c``), system images are installed on demand, and the
  device profile is picked from what the SDK actually provides.
- Each launch allocates a free console port (5554, 5556, ...) and records
  the real ADB serial (``emulator-5554`` etc.) instead of hardcoding
  ``emulator-5554`` for every device.
- Emulator output goes to ``data/logs/<name>.log`` (was ``DEVNULL``, so
  every failure was invisible) and launch waits for the process to stay
  alive and for ``sys.boot_completed`` before applying spoofing.
- Spoofed ``ro.*`` props are passed via ``emulator -prop`` (the only way
  the emulator honours them); ``config.ini`` only gets valid ``hw.*`` keys.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import config
from core import adb
from core import emulator_console
from profiles.device_profiles import get_model_profile
from profiles.profile_generator import generate_device_identity
from spoof.system_props import generate_props_config


# Camera sources accepted by the emulator for -camera-front/-camera-back and
# hw.camera.front/hw.camera.back:
#   emulated       - synthetic camera (default front)
#   virtualscene   - 3D virtual scene (default back)
#   webcam0..N     - a webcam on the *host* machine
#   none           - no camera
CAMERA_EMULATED_MODES = ("emulated", "virtualscene", "none")
CAMERA_DEFAULT_FRONT = "emulated"
CAMERA_DEFAULT_BACK = "virtualscene"
CAMERA_HELP = ("emulated, virtualscene, webcam0, webcam1, ..., usb0, ..., none")


def validate_camera_mode(value, default, role="camera"):
    """Validate a camera source string (``webcam0``, ``emulated``, ...).

    ``usb0``/``usb1``/... pass an external USB camera through to *both*
    facings (the emulator itself decides which one a facing sees), so they are
    accepted here for compatibility. Use ``webcamN`` for a per-facing host
    webcam: ``webcam0`` on the back and ``webcam1`` on the front gives you two
    genuinely independent real cameras.
    """
    if value is None or str(value).strip() == "":
        return default
    mode = str(value).strip().lower()
    if mode in CAMERA_EMULATED_MODES:
        return mode
    for prefix in ("webcam", "usb"):
        if mode.startswith(prefix):
            suffix = mode[len(prefix):].strip()
            if suffix == "":
                return f"{prefix}0"
            if suffix.isdigit():
                return f"{prefix}{int(suffix)}"
            raise ValueError(
                f"Invalid {role} '{value}'. {prefix} sources are named "
                f"{prefix}0, {prefix}1, ... (valid options: {CAMERA_HELP})."
            )
    raise ValueError(f"Invalid {role} '{value}'. Valid options: {CAMERA_HELP}.")


def camera_role_conflicts(front, back):
    """Explain sources that make the front look identical to the back.

    ``emulated`` + ``emulated`` (or the same host webcam on both facings)
    hands apps two views of *one* virtual camera, which is exactly the tell we
    want to avoid. Returns a short hint, or ``None`` when the pair is fine.
    """
    front = (front or "").strip().lower()
    back = (back or "").strip().lower()
    if not front or not back:
        return None
    if front in ("none", "virtualscene") or back in ("none", "virtualscene"):
        return None
    if front == back:
        return (
            f"Front and back are both '{front}', so apps see one shared camera. "
            "Use two different host webcams (webcam0 + webcam1) for truly "
            "independent real cameras - the per-camera sensor identity is "
            "spoofed either way."
        )
    return None


# ---------------------------------------------------------------------------
# GPU renderer selection (host-GPU leak guard)
# ---------------------------------------------------------------------------
# Passed to `emulator -gpu` / `hw.gpu.mode`. The emulator's host-passthrough
# modes ("auto", "host", "angle") render on the *host* GPU, and the guest then
# reports that desktop card as its own phone GPU:
#
#   Android Emulator OpenGL ES Translator (AMD Radeon RX 6700 XT)
#
# Frida can patch GL strings in hooked processes, but the same driver identity
# leaks through anything unhooked (Vulkan, /proc, EGL init logs, WebView, games
# with their own GL loader). The renderer itself therefore has to change: a
# spoofed device always launches with a guest/software stack.
GUEST_GPU_MODE = "swiftshader_indirect"


def resolve_gpu_mode(device=None, requested=None):
    """``-gpu`` mode to launch with, never a host-GPU passthrough for a spoof.

    Precedence: explicit ``requested`` -> the device's saved ``gpu_mode`` ->
    ``config.DEFAULT_GPU_MODE`` -> :data:`GUEST_GPU_MODE`. A host passthrough
    mode ("auto"/"host"/"angle") is replaced by the guest renderer whenever the
    device is (or defaults to) spoofed, because that is exactly what leaked the
    host GPU. Returns ``(mode, note)`` where *note* explains a substitution.
    """
    device = device or {}
    wanted = (requested or device.get("gpu_mode") or config.DEFAULT_GPU_MODE or "").strip()
    wanted = wanted or GUEST_GPU_MODE
    spoofed = device.get("auto_spoof") is not False
    lowered = wanted.lower()
    if lowered in config.HOST_GPU_MODES and spoofed:
        return GUEST_GPU_MODE, (
            f"GPU mode '{wanted}' renders on the host GPU, so apps would see "
            "the desktop graphics card (e.g. 'Android Emulator OpenGL ES "
            f"Translator (AMD Radeon RX 6700 XT)') instead of the spoofed "
            f"chip - using '{GUEST_GPU_MODE}' instead."
        )
    return wanted, None


class SdkNotFoundError(RuntimeError):
    pass


class AvdNotFoundError(RuntimeError):
    pass


class EmulatorLaunchError(RuntimeError):
    pass


class BootTimeoutError(RuntimeError):
    pass


class LaunchCancelledError(EmulatorLaunchError):
    """The launch chain was cancelled because the user stopped the device."""


def _run_tool(cmd, input_text=None, timeout=120):
    """Run an SDK tool, handling Windows ``.bat`` launchers correctly.

    ``subprocess.run([r"C:\\...\\avdmanager.bat", ...])`` fails on Windows
    without a shell; route ``.bat``/``.cmd`` through ``cmd /c``.
    """
    use_shell = False
    if cmd and str(cmd[0]).lower().endswith((".bat", ".cmd")):
        cmd = ["cmd", "/c"] + [str(c) for c in cmd]
    return subprocess.run(
        cmd, capture_output=True, text=True, input=input_text, timeout=timeout
    )


def stop_process(process, name, manager):
    """Shut down a half-launched emulator process when its launch is aborted.

    Killing the tree matters (the VM runs as a qemu child), but the manager
    must also forget its bookkeeping entry, or the next launch finds a pid it
    believes is alive and refuses with "already running".
    """
    try:
        manager._kill_process_tree(process.pid)
    except Exception:
        pass
    try:
        manager._cleanup_process_entry(name)
    except Exception:
        pass

class DeviceManager:
    """Manages emulator device lifecycle"""

    def __init__(self):
        self.devices_dir = config.SAVED_DEVICES_DIR
        self.devices_dir.mkdir(parents=True, exist_ok=True)
        config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        # name -> {"pid": int, "serial": str, "console_port": int, "process": Popen}
        self._running_processes = {}
        # Device names whose post-boot hook chain (_apply_frida_hooks) is
        # still running - the dashboard refuses a second Hooks click while
        # one is in flight (double frida-server pushes raced each other).
        self._hooks_in_flight = set()
        self._hooks_lock = threading.Lock()
        # name -> list of simulator objects with .stop() (battery, sensor)
        self._simulators = {}
        # serial -> (boot_id, read_at): short-lived cache for staleness checks
        self._boot_id_cache = {}
        # Launch epoch per device. Each launch claims a token; a Stop bumps the
        # epoch, which cancels whatever chain holds the older token. A launch
        # still waiting for the process / boot must notice and bail out,
        # otherwise it keeps "booting" long after the user closed the device -
        # and the next Launch click is rejected as "already launching" while
        # nothing runs any more.
        self._launch_epoch = {}
        self._launch_abort_lock = threading.Lock()
        # Per-thread binding of the token of the launch it is executing, so the
        # helpers below can ask "am I still the current launch?" without
        # threading a token through every call.
        self._launch_token_local = threading.local()
        # name -> outcome of the most recent launch chain. The GUI keeps its
        # own per-request state, but this survives a restart of that state and
        # makes "is a chain actually running?" answerable from the manager.
        self._launch_events = {}
        # name -> structured boot progress (phase, label, detail, elapsed).
        # A boot takes minutes and used to be one silent spinner saying
        # "Starting…": the dashboard now says which step is running.
        self._launch_progress = {}
        self._launch_progress_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Listing / lookup
    # ------------------------------------------------------------------
    def list_devices(self):
        """List all saved devices"""
        devices = []
        for device_file in self.devices_dir.glob("*.json"):
            try:
                with open(device_file, "r", encoding="utf-8") as f:
                    device = json.load(f)
                    name = device["name"]
                    device["running"] = self._is_running(name)
                    device["hooks_in_flight"] = self.hooks_in_flight(name)
                    self._decorate_runtime(device)
                    devices.append(device)
            except Exception:
                continue
        return sorted(devices, key=lambda x: x.get("created", ""), reverse=True)

    def _load_device(self, name):
        """Read a saved device without computing runtime state.

        ``get_device`` adds a ``running`` flag, which itself needs the serial,
        which needs the device - so internal helpers must use this raw loader
        to avoid infinite recursion.
        """
        device_file = self.devices_dir / f"{name}.json"
        if not device_file.exists():
            return None
        try:
            with open(device_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def get_device(self, name):
        """Get device by name (including transient runtime flags)."""
        device = self._load_device(name)
        if device is None:
            return None
        device["running"] = self._is_running(name)
        device["hooks_in_flight"] = self.hooks_in_flight(name)
        return self._decorate_runtime(device)

    def _detect_avd_image(self, avd_name):
        """Read the system image package an existing AVD was created with.

        Devices created before image variants were recorded have no ``image``
        key in their JSON. The AVD's ``config.ini`` still knows the truth, so
        use that instead of assuming the current default image.
        """
        if not avd_name:
            return None
        ini = config.AVD_HOME / f"{avd_name}.avd" / "config.ini"
        try:
            lines = ini.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        for line in lines:
            key, _, value = line.partition("=")
            if key.strip() == "image.sysdir.1" and value.strip():
                rel = value.strip().strip("/\\")
                if rel.startswith("system-images;") and rel.count(";") >= 3:
                    return rel  # already a package name
                parts = [p for p in rel.replace("\\", "/").split("/") if p]
                if parts and parts[0] == "system-images" and len(parts) >= 4:
                    return ";".join(parts)
        return None

    def set_launch_error(self, name, message, error_key="launch_error"):
        """Persist (or clear with ``None``) the last launch/stop failure.

        Without this a failed launch was only visible for ~3 seconds as a
        toast; users then saw a Stopped card and concluded "nothing works".
        ``error_key`` lets Stop record its own failure (``stop_error``)
        without overwriting - or being overwritten by - a launch failure.
        """
        try:
            device = self._load_device(name)
            if device is None:
                return
            if message:
                device[error_key] = str(message)
            else:
                device.pop(error_key, None)
            self._save_device(device)
        except Exception:
            pass

    _TRANSIENT_KEYS = frozenset({
        "running",
        "hooks_in_flight",
        "active_hook_sessions",
        # Derived on every read (see list_devices/get_device): whether the
        # live boot still matches the boot the spoof/hooks were applied to.
        "runtime_stale",
        "runtime_stale_reason",
        "needs_restore",
        # Derived from the live spoof state on every read.
        "airplane_required",
        "airplane_gate",
    })

    # ------------------------------------------------------------------
    # Persisted intent ("keep my spoofing and hooks applied")
    # ------------------------------------------------------------------
    _BOOT_ID_TTL = 15.0  # seconds; the dashboard polls device state every 10s

    def _runtime_boot_id(self, serial, fresh=False):
        """Kernel boot id for *serial* (None when it cannot be read).

        Cached briefly because this is called for every running device on
        every dashboard refresh - one ``adb shell`` per device per poll is
        cheap, one per device per widget is not.
        """
        if not serial:
            return None
        now = time.time()
        cached = self._boot_id_cache.get(serial)
        if cached and not fresh and now - cached[1] < self._BOOT_ID_TTL:
            return cached[0]
        try:
            value = adb.boot_id(serial)
        except Exception:
            value = None
        self._boot_id_cache[serial] = (value, now)
        return value

    @staticmethod
    def wants_spoof(device):
        """Should this device be spoofed (and re-spoofed after a restart)?

        Default ON - that is how launching has always behaved - so a device
        with no explicit choice is spoofed on every boot. ``auto_spoof:
        false`` (Details -> Keep Applied After Restart) opts out for good.
        """
        return (device or {}).get("auto_spoof") is not False

    @staticmethod
    def wants_hooks(device):
        """Should Frida hooks be (re-)attached? (default ON, same rules)"""
        return (device or {}).get("auto_hooks") is not False

    @classmethod
    def _pending_restore(cls, device, stale=False):
        """``(spoof_pending, hooks_pending)`` for a device.

        "Pending" means *the user asked for this and it is not live right
        now*: either it was applied before (so there is something to restore)
        or it was explicitly switched on, and it is either missing from this
        boot or ``stale`` (applied on an earlier boot). Devices that were
        never protected are left alone, and an explicit ``auto_*=false``
        opts out for good.
        """
        device = device or {}

        def one(intent_key, applied_key, applied_at_key):
            if device.get(intent_key) is False:
                return False
            ever_protected = bool(device.get(applied_at_key)) or device.get(intent_key) is True
            if not ever_protected:
                return False
            if device.get(applied_key) is not True:
                return True
            return bool(stale)

        return (
            one("auto_spoof", "spoof_applied", "spoof_applied_at"),
            one("auto_hooks", "hooks_applied", "hooks_applied_at"),
        )

    def mark_runtime_applied(self, name, serial=None, spoof=False, hooks=False,
                             boot_id=None):
        """Record that spoofing/hooks are currently live, *and remember the
        intent* so they can be re-applied after a reboot or an app restart.

        ``spoof_applied`` / ``hooks_applied`` only describe the current boot
        (an emulator reboot wipes RAM-only ``resetprop`` values and kills
        frida-server). ``auto_spoof`` / ``auto_hooks`` are the durable
        "keep this device protected" request the dashboard and the launcher
        honour: clearing them (Details → Stop restoring) stops the restore.
        """
        device = self._load_device(name)
        if device is None:
            return None
        now = datetime.now().isoformat()
        if spoof:
            device["spoof_applied"] = True
            device["spoof_applied_at"] = now
            device["auto_spoof"] = True
        if hooks:
            device["hooks_applied"] = True
            device["hooks_applied_at"] = now
            device.pop("hooks_error", None)
            device["auto_hooks"] = True
        current_boot = boot_id if boot_id is not None else self._runtime_boot_id(serial)
        if current_boot:
            device["runtime_boot_id"] = current_boot
        try:
            self._save_device(device)
        except Exception:
            return None
        return device

    def set_restore_intent(self, name, spoof=None, hooks=None):
        """Turn automatic re-application of spoofing/hooks on or off.

        Turning an intent off also clears the matching live flag, so the UI
        stops advertising protection that will not be restored.
        """
        device = self._load_device(name)
        if device is None:
            raise ValueError(f"Device not found: {name}")
        if spoof is not None:
            device["auto_spoof"] = bool(spoof)
            if not spoof:
                device["spoof_applied"] = False
                device.pop("spoof_applied_at", None)
        if hooks is not None:
            device["auto_hooks"] = bool(hooks)
            if not hooks:
                device["hooks_applied"] = False
                device.pop("hooks_applied_at", None)
        self._save_device(device)
        return device

    def _runtime_status(self, device, serial):
        """``(stale, reason)`` for the live spoof/hook state of *device*.

        The saved ``spoof_applied``/``hooks_applied`` flags describe the boot
        they were applied on. Once the guest reboots (or the emulator is
        restarted) the RAM-only props and frida-server are gone, so the flags
        are stale - reporting them as "active" was the bug behind "spoofing
        and hooks don't survive a restart".
        """
        if not serial:
            return False, None
        restored = (self.wants_spoof(device) and device.get("spoof_applied")) or (
            self.wants_hooks(device) and device.get("hooks_applied")
        )
        if not restored:
            return False, None
        recorded = device.get("runtime_boot_id")
        live = self._runtime_boot_id(serial)
        if not recorded:
            # Spoofed before boot ids were tracked (older device JSON):
            # adopt the current boot instead of declaring everything stale.
            if live:
                try:
                    device["runtime_boot_id"] = live
                    self._save_device(device)
                except Exception:
                    pass
            return False, None
        if live and live != recorded:
            return True, (
                "The emulator rebooted since spoofing/hooks were applied "
                f"(boot …{recorded[-8:]} → …{live[-8:]}), so they need "
                "re-applying."
            )
        return False, None

    @staticmethod
    def _drifted_keys(report, identity):
        """Props the device itself reported as *different* from the identity.

        The spoof report is the device's own ``getprop`` verification, so it is
        the honest answer to "is this device still showing the phone we said it
        is?" - a prop can drift without the boot id changing (a service
        resetting a value, a partial re-apply, a prop pinned by the image).

        ``verified`` holds *property* names (``ro.product.model``) while the
        identity holds the values they came from, so the expected map is
        rebuilt from the identity instead of comparing key names to it.
        """
        if not report or not identity:
            return []
        verified = report.get("verified")
        if not isinstance(verified, dict) or not verified:
            return []
        try:
            from spoof.hardware_spoof import build_identity_prop_maps

            expected, extended = build_identity_prop_maps(identity)
            expected = {**extended, **expected}
        except Exception:
            expected = {}
        return sorted(
            key for key, want in verified.items()
            if expected.get(key) is not None and str(want) != str(expected[key])
        )

    def spoof_drift(self, device):
        """``(drifted, reason)`` when the live spoof no longer matches the identity.

        Complements the boot-id check in ``_runtime_status``: that one catches a
        reboot wiping the RAM-only props, this one catches a *partial* spoof on
        the same boot (one prop silently reverted), which used to look
        "applied" forever.
        """
        device = device or {}
        if not self.wants_spoof(device) or device.get("spoof_applied") is not True:
            return False, None
        drifted = self._drifted_keys(device.get("spoof_report"), device.get("identity"))
        if not drifted:
            return False, None
        return True, (
            f"The device reports {len(drifted)} value(s) that no longer match the "
            f"spoofed identity ({', '.join(drifted[:4])}"
            f"{'...' if len(drifted) > 4 else ''}), so the spoof needs re-applying."
        )

    def _needs_auto_recovery(self, device, serial=None):
        """``(spoof, hooks, reason)``: what an unattended pass must re-apply.

        Combines three signals - the durable intent (``auto_spoof``), the live
        boot id (reboot wiped everything) and the device's own prop report
        (the spoof drifted) - so ``autorecover`` can fix any of them without a
        human clicking Spoof / Hooks / Restore.
        """
        device = device or {}
        serial = serial or device.get("serial")
        stale, stale_reason = self._runtime_status(device, serial)
        drift, drift_reason = self.spoof_drift(device)
        pending_spoof, pending_hooks = self._pending_restore(device, stale=stale or drift)
        reason = stale_reason or drift_reason or (
            "spoofing/hooks are not applied on this boot" if (pending_spoof or pending_hooks) else None
        )
        return pending_spoof, pending_hooks, reason

    def autorecover(self, name, serial=None, spoof=None, hooks=None):
        """Make the device match its saved identity again - unattended.

        This is the self-healing entry point: it re-roots, re-applies the
        verified spoof + system settings, re-attaches the Frida hooks when they
        were asked for, and finally re-evaluates the airplane-mode gate so the
        radios are on exactly when the spoof is verified. It never touches a
        device whose intent says "don't restore" (``auto_spoof``/``auto_hooks``
        false) and never claims success it did not verify.
        """
        device = self._load_device(name)
        if device is None:
            return {"success": False, "device": name,
                    "message": f"Device not found: {name}"}
        serial = serial or device.get("serial") or self.get_device_serial(name, heal=False)
        if not serial:
            return {"success": False, "device": name,
                    "message": (f"'{name}' is not running - launch it and the "
                                "spoofing/hooks are applied automatically.")}

        want_spoof, want_hooks, reason = self._needs_auto_recovery(device, serial)
        if spoof is not None:
            want_spoof = bool(spoof)
        if hooks is not None:
            want_hooks = bool(hooks)
        if not want_spoof and not want_hooks:
            return {
                "success": True, "device": name, "serial": serial,
                "spoofed": device.get("spoof_applied") is True, "hooks": False,
                "changed": False,
                "message": f"{name} already matches its identity - nothing to re-apply.",
            }

        print(f"[AntiDetect] Auto-recovering {name}"
              + (f": {reason}" if reason else "") + " ...")
        result = self.restore_device(name, serial=serial,
                                     spoof=want_spoof, hooks=want_hooks)
        result["changed"] = True
        result["reason"] = reason
        # The gate is the honest consequence of the result, not a guess.
        if result.get("spoofed"):
            self.enforce_airplane_gate(name, serial, force_off=True)
        else:
            self.enforce_airplane_gate(name, serial)
        return result

    def autorecover_all(self, only_running=True):
        """Self-heal every device that drifted, rebooted or lost its hooks.

        Run on app startup and after a launch, so "the spoof always comes back
        by itself" does not depend on the dashboard being open or on the user
        clicking anything.
        """
        results = []
        for device in self.list_devices():
            name = device.get("name")
            if not name:
                continue
            if only_running and not device.get("running"):
                continue
            try:
                serial = device.get("serial") or self.get_device_serial(name, heal=False)
                want_spoof, want_hooks, _reason = self._needs_auto_recovery(device, serial)
            except Exception as exc:  # never let one device break the sweep
                results.append({"success": False, "device": name,
                                "message": f"Could not inspect the device: {exc}"})
                continue
            if not (want_spoof or want_hooks):
                continue
            try:
                results.append(self.autorecover(name, serial=serial))
            except Exception as exc:
                results.append({"success": False, "device": name,
                                "message": f"Auto-recovery failed: {exc}"})
        return results

    def _decorate_runtime(self, device):
        """Attach live ``runtime_stale`` / ``needs_restore`` flags for the UI."""
        try:
            serial = device.get("serial")
            if serial and not self._adb_serial_present(serial):
                serial = self.get_device_serial(device["name"], heal=False)
            if not device.get("running"):
                serial = None
            stale, reason = self._runtime_status(device, serial)
            drift, drift_reason = self.spoof_drift(device)
            if drift and not stale:
                stale, reason = True, drift_reason
        except Exception:
            stale, reason = False, None
        device["runtime_stale"] = bool(stale)
        device["runtime_stale_reason"] = reason
        spoof_pending, hooks_pending = self._pending_restore(device, stale=stale)
        device["needs_restore"] = bool(
            device.get("running") and (spoof_pending or hooks_pending)
        )
        # Airplane gate: is this device holding its radios off, and why?
        # ``airplane_gate`` is derived (never saved) so the card can explain
        # the state instead of the dashboard having to infer it.
        device["airplane_mode"] = device.get("airplane_mode")
        device["airplane_required"] = bool(
            device.get("running") and self._airplane_required(device)
        )
        #   "airplane"  radios off *because* the spoof is not applied (the gate)
        #   "opening"   the gate wants them off but the device does not confirm
        #   "held-off"  radios off with no gate reason (manual airplane mode)
        #   "off"       radios on
        if device["airplane_required"]:
            device["airplane_gate"] = (
                "airplane" if device["airplane_mode"] is True else "opening"
            )
        elif device["airplane_mode"] is True:
            device["airplane_gate"] = "held-off"
        else:
            device["airplane_gate"] = "off"
        return device

    def _save_device(self, device):
        device_file = self.devices_dir / f"{device['name']}.json"
        # Don't persist transient runtime keys
        to_save = {
            k: v
            for k, v in device.items()
            if not k.startswith("_") and k not in self._TRANSIENT_KEYS
        }
        tmp_file = device_file.with_suffix(".json.tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(to_save, f, indent=2)
        os.replace(tmp_file, device_file)

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    def create_device(self, model, android_version="14", name=None, proxy=None,
                      camera_front=None, camera_back=None, image_variant=None,
                      auto_root=None, custom_image=None):
        """Create a new device with anti-detection profile.

        ``camera_front``/``camera_back`` accept the emulator camera sources:
        ``emulated``, ``virtualscene``, ``webcam0``/``webcam1``/..., ``none``.
        Use ``webcam0`` to pass the host webcam through to the emulator.

        ``image_variant`` selects the system image: ``google_apis`` (default,
        userdebug - supports ``adb root`` so Frida and property spoofing work)
        or ``google_apis_playstore`` (Play Store, no root).

        ``auto_root`` (default: the Settings toggle, on) runs ``adb root``
        automatically once the device has booted, so Frida and property
        spoofing always have root without any manual step.
        """
        from profiles.device_profiles import model_conflict

        # "Xiaomi 14" and "23116PN5BC" are different phones (14 vs 14 Pro); when
        # the request contains both a name and a code that disagree, the wrong
        # one would be spoofed silently. Check this BEFORE the unknown-model
        # error, which such a mixed selection would otherwise hit first.
        conflict = model_conflict(model)
        if conflict:
            raise ValueError(
                f"{model!r} mixes two different devices: the name matches "
                f"{conflict['by_name']['name']!r} (model {conflict['by_name']['model']}) "
                f"but the model code matches {conflict['by_code']['name']!r}. "
                "Pick one, e.g. --model "
                f"\"{conflict['by_name']['name']}\" or --model "
                f"\"{conflict['by_code']['name']}\"."
            )

        model_profile = get_model_profile(model)
        if not model_profile:
            raise ValueError(
                f"Unknown model: {model}. Use 'models' command to see available models."
            )

        if not name:
            name = f"{model_profile['brand'].lower()}_{uuid.uuid4().hex[:6]}"
        # AVD names must be filesystem-safe
        name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)

        if android_version not in config.ANDROID_VERSIONS:
            raise ValueError(
                f"Unsupported Android version: {android_version}. "
                f"Supported: {', '.join(sorted(config.ANDROID_VERSIONS))}"
            )

        android_info = dict(config.ANDROID_VERSIONS[android_version])
        if custom_image:
            # Explicit package from the SDK's system-images directory (or one
            # imported from a custom folder). The Android version is derived
            # from the package so the two can never disagree.
            parsed = config.parse_image_package(custom_image)
            android_version = config.android_version_for_api(parsed["api"])
            android_info = dict(config.ANDROID_VERSIONS[android_version])
            image = custom_image
            variant = f"custom:{parsed['tag']}"
            if not config.is_system_image_installed(image):
                raise ValueError(
                    f"Custom image not installed: {image}. Import it via the "
                    "System Image -> Import from folder flow first."
                )
        else:
            variant = image_variant or config.DEFAULT_IMAGE_VARIANT
            try:
                image = config.image_package(android_version, variant)
            except ValueError as exc:
                raise ValueError(str(exc)) from None
        android_info = {
            **android_info,
            "image": image,
            "image_variant": variant,
            "rootable": config.image_is_rootable(image),
        }

        identity = generate_device_identity(model_profile, android_version=android_version)

        # Validate optional proxy early so malformed URLs fail cleanly at create time
        cleaned_proxy = (proxy or "").strip() or None
        if cleaned_proxy:
            from anonymity.proxy_manager import ProxyManager
            ProxyManager()._parse_proxy_url(cleaned_proxy)
        proxy = cleaned_proxy

        front = validate_camera_mode(camera_front, CAMERA_DEFAULT_FRONT, "front camera")
        back = validate_camera_mode(camera_back, CAMERA_DEFAULT_BACK, "back camera")

        if auto_root is None:
            # Device-level default comes from the Settings toggle.
            auto_root = bool(config.load_settings().get("auto_root", True))

        device = {
            "name": name,
            # The label is the *resolved profile's* name. Storing the raw
            # request used to let the card/dashboard say "Google Pixel 8" while
            # the generated identity was a Pixel 8 Pro (the fuzzy matcher
            # drifted to the bigger sibling).
            "model": model_profile["name"],
            "requested_model": model,
            "brand": model_profile["brand"],
            "android_version": android_version,
            "api_level": android_info["api"],
            "codename": android_info["codename"],
            "image": android_info["image"],
            "image_variant": android_info["image_variant"],
            "rootable": android_info["rootable"],
            "auto_root": bool(auto_root),
            "proxy": proxy,
            "identity": identity,
            "camera": {"front": front, "back": back},
            # Front/back are independent sensors; keep the generated specs at
            # the top level too so the UI/API can show them without digging
            # through the identity.
            "camera_specs": identity.get("camera_specs") or {},
            # The GPU is one coherent chip: renderer, vendor, EGL vendor, driver
            # version, extension list and GL limits all come from the same model
            # entry (spoof/gpu_spoof.py applies them via Frida GLES/EGL hooks).
            "gpu": {
                key: identity.get(key)
                for key in ("gl_renderer", "gl_vendor", "gl_version", "egl_vendor",
                            "gl_extensions", "gl_limits", "gpu_name", "gpu_hw",
                            "gpu_score", "gpu_year")
            },
            "created": datetime.now().isoformat(),
            "avd_name": f"antidetect_{name}",
        }

        # The identity describes the image that actually runs: the release it
        # reports is ``android_version`` (not the profile fingerprint's own
        # release), so a device created on Android 15 never advertises
        # "Android 14.0.0" while the OS says 15.
        assert str(identity.get("android_version")) == str(android_version)

        # Create AVD first so a failed creation doesn't leave a phantom device.
        # If the SDK is missing entirely we still save the JSON (so the user
        # can fix the SDK and retry `create` without losing the identity),
        # but we surface a clear error.
        try:
            self._create_avd(device, android_info)
        except (SdkNotFoundError, AvdNotFoundError, EmulatorLaunchError):
            self._save_device(device)
            raise

        self._save_device(device)
        hint = camera_role_conflicts(front, back)
        if hint:
            device["camera_warning"] = hint
        return device

    def _create_avd(self, device, android_info):
        """Create Android Virtual Device via avdmanager (or manual fallback)."""
        avd_name = device["avd_name"]
        ini_file = config.AVD_HOME / f"{avd_name}.ini"
        if ini_file.exists():
            print(f"[DeviceManager] AVD {avd_name} already exists, reusing it.")
            return

        if not Path(config.AVDMANAGER_PATH).exists():
            print(
                "[DeviceManager] avdmanager not found at "
                f"{config.AVDMANAGER_PATH} — creating AVD config manually. "
                "Install the Android SDK cmdline-tools for full AVD creation."
            )
            self._create_avd_config_manually(device, android_info)
            return

        # Ensure the system image is installed; install it if missing.
        image = device.get("image") or android_info["image"]
        if not config.is_system_image_installed(image):
            self._install_system_image({**android_info, "image": image})

        device_profile = self._pick_device_profile()
        cmd = [
            config.AVDMANAGER_PATH,
            "create", "avd",
            "--name", avd_name,
            "--package", device.get("image") or android_info["image"],
            "--device", device_profile,
            "--force",
        ]
        try:
            result = _run_tool(cmd, input_text="no\n", timeout=180)
        except FileNotFoundError as exc:
            raise SdkNotFoundError(
                f"Could not run avdmanager at {config.AVDMANAGER_PATH}: {exc}. "
                "Check your Android SDK installation (run `python main.py check_sdk`)."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise EmulatorLaunchError(
                f"avdmanager timed out creating AVD {avd_name}: {exc}"
            ) from exc

        if result.returncode == 0 and ini_file.exists():
            # avdmanager writes default cameras; apply the user's choice.
            camera = device.get("camera") or {}
            self._write_camera_config(
                config.AVD_HOME / f"{avd_name}.avd" / "config.ini",
                camera.get("front", CAMERA_DEFAULT_FRONT),
                camera.get("back", CAMERA_DEFAULT_BACK),
            )
        if result.returncode != 0 or not ini_file.exists():
            detail = (result.stderr or result.stdout or "").strip()[-2000:]
            raise EmulatorLaunchError(
                f"Failed to create AVD {avd_name} (device profile {device_profile!r}).\n"
                f"Command: {' '.join(map(str, cmd))}\n"
                f"avdmanager output:\n{detail}\n"
                "Tip: run `python main.py check_sdk` and install the system image with:\n"
                f"  sdkmanager --install \"{android_info['image']}\""
            )

    def _install_system_image(self, android_info):
        """Install a missing system image via sdkmanager."""
        if not Path(config.SDKMANAGER_PATH).exists():
            raise SdkNotFoundError(
                f"System image {android_info['image']} is not installed and "
                f"sdkmanager was not found at {config.SDKMANAGER_PATH}. "
                "Install cmdline-tools from the Android SDK, then re-run create."
            )
        print(f"[DeviceManager] Installing system image {android_info['image']} ...")
        try:
            result = _run_tool(
                [config.SDKMANAGER_PATH, "--install", android_info["image"]],
                timeout=900,
            )
        except subprocess.TimeoutExpired as exc:
            raise EmulatorLaunchError(
                f"sdkmanager timed out installing {android_info['image']}: {exc}"
            ) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-2000:]
            raise EmulatorLaunchError(
                f"Failed to install system image {android_info['image']}:\n{detail}\n"
                "Check your network connection and SDK licenses "
                "(`sdkmanager --licenses`)."
            )
        if not config.is_system_image_installed(android_info):
            raise EmulatorLaunchError(
                f"sdkmanager reported success but system image "
                f"{android_info['image']} is still missing."
            )

    def _pick_device_profile(self):
        """Pick a device profile that the installed SDK actually provides."""
        try:
            result = _run_tool(
                [config.AVDMANAGER_PATH, "list", "device"], timeout=60
            )
            output = result.stdout or ""
            available = []
            for line in output.splitlines():
                line = line.strip()
                if line.startswith("id:"):
                    # e.g. "id: 3 or \"pixel_6\""
                    if '"' in line:
                        available.append(line.split('"')[1])
            for preferred in ("pixel_6", "pixel_5", "pixel", "nexus_5", "nexus_6p"):
                if preferred in available:
                    return preferred
            if available:
                return available[0]
        except Exception:
            pass
        return "pixel_6"

    @staticmethod
    def _write_camera_config(config_ini, front, back):
        """Set/replace hw.camera.front and hw.camera.back in an AVD config.ini."""
        config_ini = Path(config_ini)
        if not config_ini.exists():
            return False
        try:
            lines = config_ini.read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
        kept = [
            line for line in lines
            if not line.strip().lower().startswith(("hw.camera.front", "hw.camera.back"))
        ]
        kept.append(f"hw.camera.front={front}")
        kept.append(f"hw.camera.back={back}")
        try:
            config_ini.write_text("\n".join(kept) + "\n", encoding="utf-8")
            return True
        except OSError:
            return False

    def set_device_cameras(self, name, camera_front=None, camera_back=None):
        """Change camera sources for an existing device.

        Updates the saved device and its AVD ``config.ini``. A running
        emulator keeps its current cameras until it is restarted.
        """
        device = self.get_device(name)
        if not device:
            raise ValueError(f"Device not found: {name}")

        camera = dict(device.get("camera") or {})
        if camera_front is not None:
            camera["front"] = validate_camera_mode(camera_front, CAMERA_DEFAULT_FRONT, "front camera")
        if camera_back is not None:
            camera["back"] = validate_camera_mode(camera_back, CAMERA_DEFAULT_BACK, "back camera")
        camera.setdefault("front", CAMERA_DEFAULT_FRONT)
        camera.setdefault("back", CAMERA_DEFAULT_BACK)

        device["camera"] = camera
        updated_config = self._write_camera_config(
            config.AVD_HOME / f"{device['avd_name']}.avd" / "config.ini",
            camera["front"], camera["back"],
        )
        if not device.get("camera_specs"):
            # Devices created before per-camera identity existed: generate
            # specs now so front and back stop looking identical.
            from profiles.profile_generator import generate_camera_specs

            specs = generate_camera_specs(
                device.get("brand", "Samsung"),
                (device.get("identity") or {}).get("board", "unknown"),
                device.get("model", "Unknown"),
            )
            device["camera_specs"] = specs
            identity = dict(device.get("identity") or {})
            identity["camera_specs"] = specs
            device["identity"] = identity
            device.pop("boot_props", None)
        if not device.get("gpu"):
            from spoof.gpu_spoof import build_gpu_identity

            device["gpu"] = build_gpu_identity(device.get("identity") or device)

        self._save_device(device)
        return {
            "device": name,
            "camera": camera,
            "camera_specs": device.get("camera_specs"),
            "camera_warning": camera_role_conflicts(camera["front"], camera["back"]),
            "config_updated": updated_config,
            "needs_restart": self._is_running(name),
        }

    def _create_avd_config_manually(self, device, android_info):
        """Create AVD config files manually when avdmanager is unavailable.

        Only valid ``hw.*``/``avd.*`` keys go into ``config.ini`` — the
        emulator ignores ``ro.*`` lines there. Spoofed ``ro.*`` props are
        passed at launch time via ``emulator -prop`` (see launch_device).
        """
        avd_path = config.AVD_HOME / f"{device['avd_name']}.avd"
        avd_path.mkdir(parents=True, exist_ok=True)

        identity = device["identity"]
        image_package = device.get("image") or android_info["image"]
        image_path = image_package.replace(";", "/")
        is_playstore = "playstore" in image_package
        tag_display = "Google Play" if is_playstore else "Google APIs"
        tag_id = "google_apis_playstore" if is_playstore else "google_apis"
        safe_model = "".join(
            c if c.isalnum() or c in "-_" else "_" for c in identity.get("model", "generic")
        )

        config_content = f"""avd.ini.encoding=UTF-8
abi.type=x86_64
avd.id={device['avd_name']}
avd.name={safe_model}
disk.dataPartition.size=6G
hw.cpu.arch=x86_64
hw.cpu.ncore={config.DEFAULT_CORES}
hw.ramSize={config.DEFAULT_RAM}
hw.gpu.enabled=yes
hw.gpu.mode={resolve_gpu_mode(device)[0]}
hw.keyboard=yes
hw.lcd.width=1080
hw.lcd.height=2400
hw.lcd.density=420
hw.camera.back={device.get("camera", {}).get("back", CAMERA_DEFAULT_BACK)}
hw.camera.front={device.get("camera", {}).get("front", CAMERA_DEFAULT_FRONT)}
hw.sensors.orientation=yes
hw.sensors.acceleration=yes
hw.battery=yes
hw.gps=yes
hw.gsmModem=yes
hw.mainKeys=no
hw.initialOrientation=portrait
hw.device.name={identity.get('device', 'generic')}
hw.device.manufacturer={identity.get('manufacturer', 'Samsung')}
image.sysdir.1={image_path}
kernel.newDeviceNaming=yes
kernel.supportsGPUDirectAccess=yes
showDeviceFrame=no
tag.display={tag_display}
tag.id={tag_id}
"""
        with open(avd_path / "config.ini", "w", encoding="utf-8") as f:
            f.write(config_content)

        ini_file = config.AVD_HOME / f"{device['avd_name']}.ini"
        with open(ini_file, "w", encoding="utf-8") as f:
            f.write(
                "avd.ini.encoding=UTF-8\n"
                f"path={avd_path}\n"
                f"path.rel=avd/{device['avd_name']}.avd\n"
                f"target=android-{android_info['api']}\n"
            )

    # ------------------------------------------------------------------
    # Launch / stop
    # ------------------------------------------------------------------
    def launch_device(
        self,
        name,
        enable_frida=False,
        wait_for_boot=True,
        boot_timeout=None,
        wipe=False,
        on_hooks_complete=None,
        launch_token=None,
    ):
        """Launch a device by name.

        Waits for the emulator process to stay alive and (by default) for
        ``sys.boot_completed`` before applying spoofing, so failures are
        reported instead of silently returning success.
        """
        device = self.get_device(name)
        if not device:
            raise ValueError(f"Device not found: {name}")

        # Claim this launch's epoch: only a Stop issued *after* this point
        # cancels it (a Stop meant for the previous attempt cannot leak in).
        # The GUI may pass a token it claimed before starting the thread.
        self._launch_token_local.check = launch_token or self.begin_launch_token(name)

        if self._launch_blocker(name):
            raise ValueError(f"Device already running: {name}")

        # The dashboard watches this record: it turns the old silent
        # "Starting…" spinner into a step-by-step list with a bar.
        if self.launch_progress(name) is None:
            self.begin_launch_progress(name)
        self._progress_detail(name, "preparing", "Validating the Android SDK and the AVD")

        # --- Validate SDK -------------------------------------------------
        self._progress_detail(
            name, "preparing", "Checking the emulator binary "
            f"({config.EMULATOR_PATH})")
        if not Path(config.EMULATOR_PATH).exists():
            raise SdkNotFoundError(
                f"Android emulator not found at {config.EMULATOR_PATH}.\n"
                f"SDK root detected: {config.ANDROID_SDK_ROOT_STR or '(none)'}\n"
                "Install the Android SDK (emulator + platform-tools) and set "
                "ANDROID_SDK_ROOT / ANDROID_HOME, then run "
                "`python main.py check_sdk`."
            )
        self._progress_detail(name, "preparing", f"Checking adb ({config.ADB_PATH})")
        if not Path(config.ADB_PATH).exists():
            raise SdkNotFoundError(
                f"ADB not found at {config.ADB_PATH}. Install platform-tools "
                "in the Android SDK."
            )

        # --- Validate AVD -------------------------------------------------
        self._progress_detail(
            name, "preparing", f"Checking the AVD {device['avd_name']} "
            f"in {config.AVD_HOME}")
        avd_ini = config.AVD_HOME / f"{device['avd_name']}.ini"
        if not avd_ini.exists():
            raise AvdNotFoundError(
                f"AVD {device['avd_name']!r} not found at {avd_ini}. "
                "The device was created without a valid AVD (SDK was missing "
                "at create time?). Re-run `python main.py create ...` with "
                "the SDK installed, or create the AVD manually with avdmanager."
            )

        android_info = config.ANDROID_VERSIONS.get(device.get("android_version", "14"))
        image = device.get("image") or self._detect_avd_image(device.get("avd_name")) \
            or (android_info or {}).get("image")
        if image and not device.get("image"):
            # Legacy device: remember what it actually runs so the dashboard can
            # show the right root/no-root tag.
            device["image"] = image
            device["image_variant"] = "google_apis_playstore" if "playstore" in image else "google_apis"
            device["rootable"] = config.image_is_rootable(image)
            self._save_device(device)
        self._progress_detail(name, "preparing", f"Checking the system image ({image or 'from AVD'})")
        if image and not config.is_system_image_installed(image):
            raise EmulatorLaunchError(
                f"System image {image} is not installed.\n"
                f"Install it with: sdkmanager --install \"{image}\""
            )
        if image and not config.image_is_rootable(image):
            print(
                "[DeviceManager] Note: this device uses a Play Store image, which "
                "refuses `adb root`. Frida injection and protected property spoofing "
                "need a rootable image: create the device with "
                "--image google_apis (or pick 'Google APIs' in the dashboard)."
            )

        # --- Allocate a free console port ---------------------------------
        console_port = self._allocate_console_port(name)
        serial = f"emulator-{console_port}"
        adb_port = console_port + 1

        # --- Pick the GPU backend (never the host GPU for a spoofed device) --
        self._progress_detail(name, "preparing", "Choosing the renderer (never the host GPU)")
        gpu_mode, gpu_note = resolve_gpu_mode(device)
        if gpu_note:
            print(f"[DeviceManager] {name}: {gpu_note}")
        if device.get("gpu_mode") != gpu_mode:
            # Persist what this device actually renders with, so the dashboard
            # and config.ini agree with the launch flags.
            device["gpu_mode"] = gpu_mode
            self._save_device(device)

        # --- Build emulator command ---
        self._progress_detail(name, "preparing", "Checking hardware acceleration")
        cmd = [
            config.EMULATOR_PATH,
            "-avd", device["avd_name"],
            "-port", str(console_port),
            # Quickboot snapshots are a common cause of a "won't start"
            # emulator after an image/driver change; start clean.
            "-no-snapshot",
            "-no-boot-anim",
            "-gpu", gpu_mode,
            "-memory", str(config.DEFAULT_RAM),
            "-cores", str(config.DEFAULT_CORES),
            "-no-metrics",
        ]
        # Enable a per-AVD copy-on-write system overlay for rootable images so
        # `adb disable-verity` + `adb remount` + `build.prop` fallback edits
        # actually stick without modifying the shared SDK system image.
        if device.get("rootable", True):
            cmd.append("-writable-system")
        if wipe:
            cmd.append("-wipe-data")

        # Acceleration: let the emulator auto-detect instead of forcing
        # `-accel on` (which aborts with "x86_64 emulation currently requires
        # hardware acceleration" on machines without KVM/HAXM/WHPX).
        accel_ok, accel_msg = self._acceleration_status()
        self._progress_detail(
            name, "preparing",
            "Acceleration: usable" if accel_ok is True
            else ("Acceleration: NOT usable - the guest may be slow" if accel_ok is False
                  else f"Acceleration: unknown ({str(accel_msg).splitlines()[0][:80] if accel_msg else 'no answer'})"),
        )
        if accel_ok is False:
            print(
                "[DeviceManager] Warning: hardware acceleration unavailable "
                f"({accel_msg}). The x86_64 system image may fail to boot; "
                "enable VT-x/AMD-V (or WHPX/KVM/HAXM) and retry."
            )
        cmd.extend(["-accel", "auto"])

        # Spoofed ro.* props via -prop (config.ini ignores ro.* lines).
        # `boot_props` lets `update_devices.py` pin a prop set without
        # regenerating the whole identity. Always strip toxic early-boot
        # HAL/GPU keys even if an older saved device JSON had them pinned.
        _UNSAFE_BOOT_PROPS = frozenset({
            "ro.kernel.qemu",
            "ro.kernel.qemu.gles",
            "ro.boot.qemu",
            "ro.hardware",
            "ro.boot.hardware",
            "ro.hardware.egl",
            "ro.hardware.vulkan",
        })
        try:
            props_text = "\n".join(device["boot_props"]) if device.get("boot_props") else generate_props_config(
                device["identity"], device.get("brand", ""), device.get("model", "")
            )
            props = {}
            for line in props_text.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip()
                # Empty values and newline-containing values confuse the
                # emulator argument parser; skip them rather than crash.
                if not key or not value or "\n" in value or key in _UNSAFE_BOOT_PROPS:
                    continue
                props[key] = value
            for key, value in props.items():
                cmd.extend(["-prop", f"{key}={value}"])
        except Exception as exc:  # never block launch on prop generation
            print(f"[DeviceManager] Warning: could not build -prop flags: {exc}")

        # Camera sources (webcam passthrough, virtual scene, ...). The flags
        # win over config.ini, so changes take effect on the next launch.
        camera = device.get("camera") or {}
        front = camera.get("front") or CAMERA_DEFAULT_FRONT
        back = camera.get("back") or CAMERA_DEFAULT_BACK
        cmd.extend(["-camera-front", str(front), "-camera-back", str(back)])

        if device.get("proxy"):
            proxy = device["proxy"].strip()
            # emulator -http-proxy only accepts http:// (or socks via newer
            # builds); pass through but normalise bare host:port.
            if "://" not in proxy:
                proxy = f"http://{proxy}"
            cmd.extend(["-http-proxy", proxy])

        # --- Clear a stale AVD lock left by a killed emulator --------------
        # A hard-killed emulator (or one killed from Task Manager) leaves
        # <avd>/hardware-qemu.ini.lock behind, and the next start then fails
        # with "another emulator instance is running". No console answers on
        # our port at this point (checked in _launch_blocker), so such a lock
        # is stale and must not block the launch.
        self._clear_stale_avd_locks(device)

        # --- Spawn emulator with logging ----------------------------------
        self._progress_detail(
            name, "starting",
            f"Starting {device['avd_name']} on emulator-{console_port} "
            f"(-gpu {gpu_mode})",
        )
        log_path = config.LOGS_DIR / f"{name}.log"
        config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "ab", buffering=0)

        env = dict(os.environ)
        if config.ANDROID_SDK_ROOT_STR:
            env.setdefault("ANDROID_SDK_ROOT", config.ANDROID_SDK_ROOT_STR)
            env.setdefault("ANDROID_HOME", config.ANDROID_SDK_ROOT_STR)
        env.setdefault("ANDROID_AVD_HOME", str(config.AVD_HOME))

        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=creationflags,
                start_new_session=(os.name != "nt"),
            )
        except FileNotFoundError as exc:
            log_file.close()
            raise SdkNotFoundError(
                f"Could not execute emulator at {config.EMULATOR_PATH}: {exc}"
            ) from exc
        except OSError as exc:
            log_file.close()
            raise EmulatorLaunchError(f"Failed to start emulator: {exc}") from exc

        self._running_processes[name] = {
            "pid": process.pid,
            "serial": serial,
            "console_port": console_port,
            "adb_port": adb_port,
            "process": process,
            "log_file": log_file,
            "log_path": str(log_path),
        }

        # Persist runtime info so `list`/`stop` work after a restart.
        device["serial"] = serial
        device["console_port"] = console_port
        device["adb_port"] = adb_port
        device["pid"] = process.pid
        device["log_path"] = str(log_path)
        device["launched_at"] = datetime.now().isoformat()
        # Root/spoof/hooks never survive an emulator restart; the auto-root +
        # spoof steps below re-apply them. They are cleared here (not the
        # persisted ``auto_spoof``/``auto_hooks`` *intent*, which lives on so a
        # restart - or a later app restart - always restores them again).
        device["rooted"] = None
        device["root_error"] = None
        device["hooks_applied"] = False
        device["hooks_applied_at"] = None
        device["hooks_error"] = None
        device["spoof_applied"] = False
        device["spoof_applied_at"] = None
        device["spoof_report"] = None
        device["spoof_error"] = None
        device.pop("runtime_boot_id", None)
        self._save_device(device)

        # --- Prove the process stays alive --------------------------------
        self._progress_detail(
            name, "starting",
            f"Emulator process started (pid {process.pid}); waiting for it to stay up",
        )
        deadline = time.time() + config.EMULATOR_START_TIMEOUT
        while time.time() < deadline:
            if self._launch_cancelled():
                # Kill what we just spawned - the user closed the device
                # while the emulator process was still coming up.
                stop_process(process, name, self)
                raise LaunchCancelledError(
                    f"Launch of '{name}' was cancelled by a stop request."
                )
            if process.poll() is not None:
                tail = self.get_log_tail(name, lines=40)
                hint = self._launch_hint(tail)
                self._cleanup_process_entry(name)
                raise EmulatorLaunchError(
                    f"Emulator exited immediately (code {process.returncode}). "
                    f"Full log: {log_path}\n"
                    + (f"Likely cause: {hint}\n" if hint else "")
                    + f"Last log lines:\n{tail}"
                )
            time.sleep(0.5)

        # --- Wait for boot, then spoof ------------------------------------
        if wait_for_boot:
            timeout = boot_timeout or config.EMULATOR_BOOT_TIMEOUT
            self._progress_detail(
                name, "booting",
                f"Waiting for Android to finish booting on {serial} "
                f"(up to {int(timeout)}s)",
            )
            try:
                self.wait_for_boot(serial, timeout=timeout, process=process,
                                   cancel=lambda: self._launch_cancelled(),
                                   on_progress=lambda elapsed, state, left: (
                                       self._progress_detail(
                                           name, "booting",
                                           f"Booting Android ({int(elapsed)}s elapsed, "
                                           f"{int(left)}s left - adb: {state or 'no contact yet'})",
                                       )
                                   ))
            except LaunchCancelledError:
                stop_process(process, name, self)
                raise
            except BootTimeoutError:
                tail = self.get_log_tail(name, lines=40)
                raise BootTimeoutError(
                    f"Emulator {serial} did not finish booting within "
                    f"{timeout}s. It may still be starting — check the log "
                    f"at {log_path}.\nLast log lines:\n{tail}"
                ) from None

            # Stop pressed while booting: do not start rooting/spoofing a
            # device the user just closed.
            if self._launch_cancelled():
                stop_process(process, name, self)
                raise LaunchCancelledError(
                    f"Launch of '{name}' was cancelled by a stop request."
                )

            # Root the freshly booted device automatically (default on), so
            # spoofing and frida-server never run without root. Real status
            # is persisted on the device (rooted / root_error) for the UI.
            self._progress_detail(name, "rooting", "Android booted - enabling root (adb root)")
            self._maybe_auto_root(device, serial)

            # Boot is confirmed: apply hardware spoofing FIRST (it may soft-
            # restart zygote via resetprop or reboot for build.prop fallback),
            # then apply runtime system props & settings so nothing gets wiped.
            # ``auto_spoof=False`` (Details -> stop restoring) is honoured here
            # too, so a device the user unprotected stays unprotected.
            if self.wants_spoof(device):
                # Radios OFF until the spoof is verified: an unspoofed guest
                # announces the emulator's IMEI/baseband/network.
                self._progress_detail(
                    name, "spoofing",
                    "Radios off for now (airplane gate) - applying the hardware spoof",
                )
                self.enforce_airplane_gate(name, serial)
                device = self._load_device(name) or device
                self._apply_hardware_spoof(device, serial)
                self._progress_detail(
                    name, "spoofing",
                    "Hardware props set - applying system props and settings",
                )
                self._apply_system_spoof(device, serial)
                device = self._load_device(name) or device
                if device.get("spoof_applied") is True:
                    # Verified on this boot: the spoofed identity is live, so
                    # the phone is allowed back on the air.
                    self.enforce_airplane_gate(name, serial, force_off=True)
                else:
                    print(
                        f"[DeviceManager] {name}: spoof not verified - keeping "
                        "the device in airplane mode so it does not broadcast "
                        "the emulator identity."
                    )
            else:
                # Spoofing switched off for this device: it is the user's
                # choice, and leaving it in airplane mode would be a surprise.
                self.enforce_airplane_gate(name, serial, force_off=True)

        # --- Auto-recover the rest of the chain -----------------------------
        # Hooks are re-attached when the launch asked for them, or when this
        # device was hooked before (the durable intent) - a reboot killed
        # frida-server and the user should not have to click anything.
        live = self._load_device(name) or device
        if enable_frida and self.wants_hooks(live):
            self._progress_detail(name, "hooking", "Pushing frida-server and attaching hooks")
            self._apply_frida_hooks(live, serial, on_complete=on_hooks_complete)
        elif wait_for_boot and self.wants_hooks_on_launch(live):
            print(f"[AntiDetect] {name}: re-attaching Frida hooks (saved intent)")
            self._progress_detail(name, "hooking", "Re-attaching the saved Frida hooks")
            self._apply_frida_hooks(live, serial, on_complete=on_hooks_complete)

        # Verify the result honestly: if the spoof did not take, the radios
        # must stay off (the gate above put them there) and the card must say
        # why - never a green "Launching" over an unspoofed device.
        if wait_for_boot:
            final = self._load_device(name) or device
            if self.wants_spoof(final) and final.get("spoof_applied") is not True:
                print(
                    f"[AntiDetect] {name} is still NOT spoofed after launch: "
                    f"{final.get('spoof_error') or 'verification did not pass'}. "
                    "Airplane mode stays on until it is."
                )
            else:
                self.enforce_airplane_gate(name, serial, force_off=True)

        final_state = self._load_device(name) or device
        self._progress_detail(
            name, "finishing",
            f"Ready on {serial}"
            + (" - spoof verified" if final_state.get("spoof_applied") is True else ""),
        )
        return device

    # -- launch cancellation -------------------------------------------
    def begin_launch_token(self, name):
        """Claim the current launch epoch for a new chain and return a token.

        ``launch_token`` is a closure: it stays True for exactly as long as no
        Stop has been issued for this device since the claim. Unlike a
        one-shot cancel flag, this cannot be lost to a Stop that arrives
        between starting the thread and entering ``launch_device``.
        """
        with self._launch_abort_lock:
            epoch = int(self._launch_epoch.get(name, 0))

        def _still_current():
            with self._launch_abort_lock:
                return int(self._launch_epoch.get(name, 0)) == epoch

        return _still_current

    def cancel_launch(self, name):
        """Cancel this device's in-flight launch chain, if any.

        Called by ``stop_device`` (the card's Stop button doubles as
        "Abort launch"). Without this, closing the device while it was still
        starting left the launch thread waiting out its boot timeout, and the
        Launch button answered "Launch already in progress" for minutes.
        """
        with self._launch_abort_lock:
            self._launch_epoch[name] = int(self._launch_epoch.get(name, 0)) + 1

    def clear_launch_cancel(self, name):
        """Kept for compatibility: epoch tokens need no explicit clearing."""
        return None

    def launch_token(self):
        """Current cancel-check for this thread's device (set by launch_device)."""
        return getattr(self._launch_token_local, "check", None)

    def _launch_cancelled(self, token=None):
        """True when the Stop that owns *token* (or this thread's launch) fired."""
        check = token if token is not None else self.launch_token()
        return bool(check and not check())

    # -- boot progress -------------------------------------------------
    # The chain the user watches, in order. ``weights`` are how much of the
    # bar each phase owns; they are rough on purpose (a first boot is slower
    # than the numbers suggest) but the *order* is exact, so the panel never
    # shows "spoofing" before "booted".
    LAUNCH_PHASES = (
        ("preparing", "Checking SDK, AVD and ports", 5),
        ("starting", "Starting the emulator process", 10),
        ("booting", "Booting Android", 45),
        ("rooting", "Enabling root", 10),
        ("spoofing", "Applying & verifying the spoof", 20),
        ("hooking", "Attaching Frida hooks", 8),
        ("finishing", "Finishing up", 2),
    )

    def launch_progress(self, name):
        """Structured progress of the launch chain for *name* (or None).

        Shape: ``{"phase", "label", "detail", "index", "total", "percent",
        "elapsed", "started_at", "failed", "error", "stalled", "history",
        "phases": [...]}`` where ``history`` is what the chain has actually
        done (each step with the second it happened), so the dashboard can
        show movement instead of one unchanging sentence.
        Returns None when no chain has reported anything for this device.
        """
        with self._launch_progress_lock:
            info = self._launch_progress.get(name)
            if not info:
                return None
            info = dict(info)
        info["elapsed"] = round(time.time() - info.get("started_ts", time.time()), 1)
        phases = [{"key": key, "label": label, "weight": weight}
                  for key, label, weight in self.LAUNCH_PHASES]
        index = info.get("index", 0)
        total_weight = float(sum(w for _k, _l, w in self.LAUNCH_PHASES))
        done = sum(w for _k, _l, w in self.LAUNCH_PHASES[:index])
        # Partial credit inside the current phase so a long "Booting Android"
        # is visible as movement rather than a frozen bar.
        detail = (info.get("detail") or "")
        if index < len(self.LAUNCH_PHASES):
            weight = self.LAUNCH_PHASES[index][2]
            frac = 0.0
            marker = None
            if "(" in detail and detail.rstrip().endswith(")"):
                marker = detail.rstrip()[detail.rfind("(") + 1:-1]
            if marker and "/" in marker:
                try:
                    done_part, whole = marker.split("/", 1)
                    frac = max(0.0, min(1.0, float(done_part) / float(whole)))
                except (ValueError, ZeroDivisionError):
                    frac = 0.0
            elif marker:
                try:
                    frac = max(0.0, min(1.0, float(marker)))
                except ValueError:
                    frac = 0.35 if detail else 0.0
            else:
                frac = 0.35 if detail else 0.0
            if index == len(self.LAUNCH_PHASES) - 1 and detail:
                frac = 1.0  # "Finishing up" with a detail means it is done
            done += weight * frac
        since_change = round(time.time() - info.get("last_change_ts",
                                                    info.get("started_ts", time.time())), 1)
        if info.get("stalled") and not info.get("failed"):
            doing = (info.get("real_detail") or info.get("label") or "starting").rstrip(".")
            doing = doing[0].lower() + doing[1:] if doing else doing
            detail = f"Still {doing} - {int(since_change)}s with no new step."
            tail = info.get("stall_tail")
            detail += (f" Last log line: {tail}" if tail
                       else " The emulator has not written a log line yet.")
            info["detail"] = detail

        history = list(info.get("history") or [])
        info.update({
            "phase": info.get("phase"),
            "label": info.get("label"),
            "detail": detail,
            # Seconds since the chain last had anything new to say. A boot
            # waits silently for minutes, so this is what lets the card say
            # "no new step for 2m" instead of pretending to be busy.
            "since_change": since_change,
            "stalled": bool(info.get("stalled")),
            "history": history[-12:],
            "index": index,
            "total": len(self.LAUNCH_PHASES),
            "percent": int(round(100.0 * done / total_weight)) if total_weight else 0,
            "phases": phases,
        })
        return info

    def begin_launch_progress(self, name):
        """Start (or restart) the progress record for a launch chain."""
        with self._launch_progress_lock:
            now = time.time()
            self._launch_progress[name] = {
                "phase": "preparing",
                "label": self.LAUNCH_PHASES[0][1],
                "detail": "",
                "real_detail": "",
                "index": 0,
                "started_ts": now,
                "last_change_ts": now,
                "failed": False,
                "error": None,
                "stalled": False,
                "history": [],
            }

    def report_launch_progress(self, name, phase, detail="", error=None):
        """Move the progress record to *phase* (no-op for unknown devices).

        ``detail`` is a human sentence; a trailing ``(3/12)`` (or ``(45%)``)
        is understood by :meth:`launch_progress` and animates the bar without
        needing a numeric argument here.
        """
        order = [key for key, _label, _weight in self.LAUNCH_PHASES]
        if phase not in order:
            raise ValueError(f"Unknown launch phase: {phase}")
        with self._launch_progress_lock:
            info = self._launch_progress.setdefault(name, {
                "started_ts": time.time(), "failed": False, "error": None,
                "index": 0, "detail": "",
            })
            changed = False
            if info.get("phase") != phase:
                info["phase"] = phase
                info["label"] = self.LAUNCH_PHASES[order.index(phase)][1]
                info["index"] = order.index(phase)
                changed = True
            if detail and str(detail) != info.get("real_detail"):
                info["detail"] = str(detail)
                info["real_detail"] = str(detail)
                changed = True
            if changed:
                info["last_change_ts"] = time.time()
                info["stalled"] = False
                history = info.setdefault("history", [])
                history.append({
                    "text": str(detail or self.LAUNCH_PHASES[order.index(phase)][1]),
                    "phase": phase,
                    "at": round(info["last_change_ts"] - info.get("started_ts", 0), 1),
                })
                del history[:-40]
            if error:
                info["failed"] = True
                info["error"] = str(error)

    # How long a step may go without news before the card says so out loud
    # ("still checking the SDK - 28s with no new step, last log: ...").
    PROGRESS_QUIET_BUDGET = 20.0

    def start_progress_watchdog(self, name):
        """Say out loud when a step stops making progress.

        A launch step that blocks (a wedged ``emulator -accel-check``, a huge
        AVD, a stalled adb) used to leave the panel frozen on one sentence,
        which is indistinguishable from a hung app. Once a step has had
        nothing new to report for :data:`PROGRESS_QUIET_BUDGET` seconds, the
        record is annotated with the step it is really on, how long it has
        been quiet, and the last line of the emulator log.
        """
        def run():
            last_note = 0.0
            while True:
                time.sleep(2.0)
                with self._launch_progress_lock:
                    cur = self._launch_progress.get(name)
                    info = dict(cur) if cur else None
                if info is None:
                    return
                if not self._launch_thread_alive(name) and not info.get("failed"):
                    return  # chain finished; leave its final record alone
                quiet = time.time() - info.get("last_change_ts", time.time())
                if quiet < self.PROGRESS_QUIET_BUDGET:
                    continue
                if last_note and quiet - last_note < 10:
                    continue  # do not spam: one note per ~10s of silence
                last_note = quiet
                # The last *real* log line, if the emulator has written one.
                # "(no log file yet)" / "(log is empty)" are the tail helper's
                # own placeholders, not progress.
                tail = ""
                try:
                    for line in reversed(self.get_log_tail(name, lines=15).splitlines()):
                        stripped = line.strip()
                        if (stripped and "fake emulator" not in stripped
                                and not stripped.startswith("(no log")
                                and not stripped.startswith("(log is empty")):
                            tail = stripped
                            break
                except Exception:
                    tail = ""
                with self._launch_progress_lock:
                    cur = self._launch_progress.get(name)
                    if cur is None:
                        return
                    # Only the facts go in the record; the "Ns with no new
                    # step" part is recomputed on every read so it counts up.
                    cur["stalled"] = True
                    cur["stall_tail"] = tail[:160]
                    history = cur.setdefault("history", [])
                    doing = (cur.get("real_detail") or cur.get("label") or "starting").rstrip(".")
                    note = "Still " + (doing[:1].lower() + doing[1:] if doing else doing)
                    if not history or history[-1].get("text") != note:
                        history.append({
                            "text": note,
                            "phase": cur.get("phase"),
                            "at": round(time.time() - cur.get("started_ts", 0), 1),
                        })
                        del history[:-40]

        threading.Thread(target=run, daemon=True,
                         name=f"antidetect-progress-{name}").start()

    def fail_launch_progress(self, name, error):
        """Freeze the progress record on the phase that failed."""
        with self._launch_progress_lock:
            info = self._launch_progress.get(name)
            if info is None:
                return
            info["failed"] = True
            info["error"] = str(error)
            info["detail"] = str(error).splitlines()[0][:400]

    def clear_launch_progress(self, name):
        with self._launch_progress_lock:
            self._launch_progress.pop(name, None)

    def _progress_detail(self, name, step, text, current=None, total=None):
        """Helper: report *text*, with an optional ``(current/total)`` suffix."""
        detail = text
        if current is not None and total:
            detail = f"{text} ({current}/{total})"
        self.report_launch_progress(name, step, detail)

    # -- launch chain bookkeeping --------------------------------------
    def set_launch_running(self, name):
        """Record that a launch chain for *name* has been started."""
        self._launch_events[name] = {"running": True, "outcome": None}

    def set_launch_finished(self, name, outcome):
        """Record the outcome of a launch chain (shell-only, not persisted)."""
        self._launch_events[name] = {"running": False, "outcome": outcome}

    def _launch_thread_alive(self, name):
        """True while a launch chain for *name* is still executing."""
        event = self._launch_events.get(name) or {}
        return bool(event.get("running"))

    def stop_device(self, name):
        """Stop a running device (uses its recorded serial, not 5554).

        Kills the whole emulator process *tree* (on Windows the VM runs as a
        ``qemu-system-*`` child of ``emulator.exe``), then **verifies** the
        console stopped answering and the ports were released. Skipping that
        verification is what made the next Launch fail with "Device already
        running" (stale adb entry) or "port in use" (lingering child).
        """
        device = self.get_device(name)
        if not device:
            raise ValueError(f"Device not found: {name}")

        # Stop also aborts a launch that is still booting, so the chain never
        # keeps running behind a card that already says "Stopped".
        self.cancel_launch(name)

        serial = device.get("serial") or self._running_processes.get(name, {}).get("serial")
        entry = self._running_processes.get(name)

        # Stop any active battery/sensor simulator threads for this device
        self._stop_simulators(name)

        # Detach any live Frida sessions attached to this emulator serial
        if serial:
            try:
                from core.hook_manager import detach_device_sessions
                detach_device_sessions(serial)
            except Exception:
                pass

        # 1. Ask the emulator to shut down cleanly (saves its userdata).
        if serial:
            try:
                subprocess.run(
                    [config.ADB_PATH, "-s", serial, "emu", "kill"],
                    capture_output=True,
                    timeout=10,
                )
            except Exception:
                pass

        # 2. Give it a moment to go away on its own (a clean `emu kill` saves
        #    its userdata). The wait is short and gives up early when the
        #    process clearly ignores the request - a wedged VM must not make
        #    the Stop button feel broken for eight seconds.
        deadline = time.time() + 5.0
        started = time.time()
        while time.time() < deadline:
            proc = (entry or {}).get("process")
            if proc is not None and proc.poll() is not None:
                break
            if not self._pid_alive(device.get("pid")):
                break
            if time.time() - started > 1.5 and entry is None:
                # Not our child (app restart / external launch): no reason to
                # wait for a graceful shutdown we cannot observe.
                break
            time.sleep(0.25)

        # 3. Kill the whole tree if it is still around (lingering qemu child).
        #    Only when the pid is still an emulator: a recycled pid must never
        #    be signalled (see _pid_looks_like_emulator).
        pid = (entry or {}).get("pid") or device.get("pid")
        if pid and self._may_kill_for_device(pid, device):
            print(f"[DeviceManager] Emulator for '{name}' (pid {pid}) did not exit "
                  "after 'emu kill' - force-killing its process tree.")
            self._kill_process_tree(pid)

        # Reap the child so it becomes neither a zombie nor reported as
        # "still running" by a later _pid_alive() check.
        proc = (entry or {}).get("process")
        if proc is not None:
            try:
                proc.wait(timeout=5)
            except Exception:
                pass

        self._cleanup_process_entry(name)

        # 4. Sweep for anything still holding this device: on a real machine
        #    the VM is a qemu child that may outlive `emu kill`, ignore
        #    SIGTERM, or come from an earlier launch whose pid we no longer
        #    track. Keep hunting (tree kill + serial-port owner + stray
        #    emulator processes for this AVD) until the console is silent and
        #    the ports are free - "Stopped" must mean stopped.
        leftovers = self._sweep_lingering_emulator(name, timeout=12.0)

        # The sweep already identified something we must not kill: no point in
        # waiting out the timeout, and pretending would be worse.
        blocked = leftovers.startswith("Still held")
        stopped = False if blocked else self._wait_until_stopped(name, timeout=8.0)
        if not stopped and not blocked:
            stopped = self._force_release(name)
        if not stopped:
            raise EmulatorLaunchError(
                f"Could not stop '{name}': port {device.get('console_port')} "
                "is still in use."
                + (f" {leftovers}." if leftovers else "")
                + " That is not an emulator this app may kill - it belongs to "
                "another program or user (Android Studio? a second copy of the "
                "dashboard?). Close it, or run `python main.py doctor` for the "
                "exact holder."
            )

        # 4b. Nothing answers any more: drop the AVD locks the dead VM left
        #     behind, otherwise the next Launch can die with "another emulator
        #     instance is running" even though this device is fully stopped.
        self._clear_stale_avd_locks(self._load_device(name) or device)

        # 5. Make real adb drop the (now dead) serial so the next Launch is
        #    not rejected as "already running". Emulators deregister lazily.
        if serial and self._adb_serial_present(serial):
            for _ in range(6):
                try:
                    subprocess.run(
                        [config.ADB_PATH, "-s", serial, "reconnect", "offline"],
                        capture_output=True, timeout=10,
                    )
                except Exception:
                    pass
                if not self._adb_serial_present(serial):
                    break
                time.sleep(0.5)

        self._boot_id_cache.pop(serial or "", None)

        # Root/hook/runtime state does not survive the stop; clear it so a
        # restarted dashboard doesn't claim a stopped device is still active.
        try:
            stopped_dev = self._load_device(name)
            if stopped_dev is not None:
                stopped_dev["rooted"] = None
                stopped_dev["root_error"] = None
                stopped_dev["hooks_applied"] = False
                stopped_dev["hooks_applied_at"] = None
                stopped_dev["hooks_error"] = None
                stopped_dev["spoof_applied"] = False
                stopped_dev["spoof_applied_at"] = None
                stopped_dev["spoof_report"] = None
                stopped_dev["spoof_error"] = None
                stopped_dev.pop("pid", None)
                stopped_dev["stopped_at"] = datetime.now().isoformat()
                # The intent (auto_spoof/auto_hooks) deliberately survives:
                # launching the device again re-applies spoofing + hooks.
                stopped_dev.pop("runtime_boot_id", None)
                self._save_device(stopped_dev)
        except Exception:
            pass
        return True

    def delete_device(self, name):
        """Delete a device"""
        device = self.get_device(name)
        if not device:
            raise ValueError(f"Device not found: {name}")

        if self._is_running(name):
            self.stop_device(name)

        avd_path = config.AVD_HOME / f"{device['avd_name']}.avd"
        if avd_path.exists():
            shutil.rmtree(avd_path, ignore_errors=True)

        ini_file = config.AVD_HOME / f"{device['avd_name']}.ini"
        if ini_file.exists():
            ini_file.unlink()

        device_file = self.devices_dir / f"{name}.json"
        if device_file.exists():
            device_file.unlink()

        return True

    # ------------------------------------------------------------------
    # Runtime helpers
    # ------------------------------------------------------------------
    def get_device_serial(self, name, persist=True, heal=True):
        """Serial of the *running* emulator for this device (or None).

        Resolution order:
        1. the serial recorded at launch, if that device is still connected,
        2. an emulator whose AVD name matches this device (covers emulators
           started outside the app, or relaunched on a new port),
        3. the only running emulator, but *only* when this device has no
           recorded serial and no AVD name to match against,
        4. SELF-HEAL: when the emulator console proves the device is alive
           but adb lost it, restart the adb server (emulators re-register)
           and match again.

        A resolved serial that differs from the saved one is persisted so
        later calls (spoofing, Frida, stop) hit the same instance.
        """
        device = self._load_device(name)
        if not device:
            return None

        entry = self._running_processes.get(name, {})
        saved = entry.get("serial") or device.get("serial")
        avd_name = device.get("avd_name")

        states = {d["serial"]: d["state"] for d in adb.devices()}
        if saved and states.get(saved) in ("device", "offline", "unauthorized"):
            return saved

        matched = None
        if avd_name:
            for serial, state in states.items():
                if state != "device" or not serial.startswith("emulator-"):
                    continue
                if adb.emu_avd_name(serial) == avd_name:
                    matched = serial
                    break

        if matched is None and not saved and not avd_name:
            # Legacy device files without an AVD name: accept the single
            # running emulator rather than making the device unusable.
            matched = adb.detect_serial()

        if matched is None and heal:
            matched = self._heal_adb_and_rematch(device)

        if matched and matched != saved:
            device["serial"] = matched
            if persist:
                try:
                    self._save_device(device)
                except Exception:
                    pass
            print(f"[DeviceManager] Bound device '{name}' to running emulator "
                  f"{matched} (AVD {avd_name}).")
        # No connected emulator -> None. A saved serial for an emulator that
        # isn't running must not be treated as a usable device.
        return matched

    def _heal_adb_and_rematch(self, device):
        """Recover an emulator whose console is alive but adb can't see it.

        Returns the serial after healing, or None when there is no live
        console for this device (i.e. it really is not running).
        """
        name = device.get("name")
        avd_name = device.get("avd_name")
        entry = self._running_processes.get(name, {})

        # Positive evidence the emulator is alive outside of adb:
        console_port = device.get("console_port")
        alive_port = None
        if console_port:
            avd = emulator_console.probe_console(console_port)
            if avd and (not avd_name or avd == avd_name):
                alive_port = int(console_port)

        if alive_port is None and (
            self._pid_alive(entry.get("pid")) or self._pid_alive(device.get("pid"))
        ):
            # The port we recorded is wrong/stale but the process is alive:
            # scan the console range for this AVD.
            for port, avd in emulator_console.scan_consoles().items():
                if avd_name and avd == avd_name:
                    alive_port = port
                    break

        if alive_port is None:
            return None  # genuinely not running

        print(f"[DeviceManager] Emulator for '{name}' answers on console port "
              f"{alive_port} but adb can't see it - restarting the adb server "
              "so it re-registers.")
        restarted, found = adb.restart_server(wait_timeout=25)
        if not restarted:
            return None

        expected = f"emulator-{alive_port}"
        deadline = time.time() + 20
        while time.time() < deadline:
            states = {d["serial"]: d["state"] for d in adb.devices()}
            for serial, state in states.items():
                if state != "device" or not serial.startswith("emulator-"):
                    continue
                if serial == expected:
                    return serial
                if avd_name and adb.emu_avd_name(serial) == avd_name:
                    return serial
            time.sleep(1)
        return None

    def _console_confirms(self, device):
        """True when the device's recorded console port answers with its AVD.

        The emulator console listens even when adb has lost track of the
        emulator, so this is the ground truth for "is there a live emulator
        for this device". A pid alone is NOT proof: after a crash, Windows
        happily reuses the pid for an unrelated process, which used to pin
        the dashboard on "Running" forever.
        """
        port = (device or {}).get("console_port")
        if not port:
            return False
        avd = emulator_console.probe_console(port)
        if avd is None:
            return False
        expected = (device or {}).get("avd_name")
        return not expected or avd == expected

    @staticmethod
    def _recently_launched(device, within_seconds=600):
        """True for a few minutes after launch (console not listening yet)."""
        launched_at = (device or {}).get("launched_at")
        if not launched_at:
            return False
        try:
            started = datetime.fromisoformat(str(launched_at))
        except ValueError:
            return False
        return (datetime.now() - started).total_seconds() < within_seconds

    def _is_running(self, name):
        """Check if device is running (PID + console probe + ADB serial).

        Survives app restarts and does not confuse a stale/reused pid with a
        live emulator: a persisted pid only counts when the emulator console
        confirms it, or for a short grace window right after launch.
        """
        entry = self._running_processes.get(name)
        if entry:
            proc = entry.get("process")
            if proc is not None and proc.poll() is None:
                return True
            if self._pid_alive(entry.get("pid")):
                return True
            # Process died; fall through to ADB check before giving up.
            self._running_processes.pop(name, None)

        # Check persisted serial via ADB (covers restarts / external launches)
        device = self._load_device(name)
        serial = (device or {}).get("serial")
        if serial and self._adb_serial_present(serial):
            return True
        if device and self._pid_alive(device.get("pid")):
            # A live pid is only trustworthy when the emulator console also
            # answers, or right after launch while the console is still
            # coming up. Otherwise the pid was recycled by another process
            # (very common on Windows) and the emulator is long gone.
            if self._console_confirms(device) or self._recently_launched(device):
                return True
            # Not running. Only now forget the pid - and only after checking
            # nothing of ours actually holds the device: a wedged emulator can
            # have a silent console while its process still owns the ports.
            # (Dropping the record before that check is how Stop lost the only
            # handle it had on a still-running VM.)
            if self._emulator_evidence(device):
                # Something of ours is still up (booting, or wedged with a
                # silent console). Do not claim "stopped" and do not forget
                # the handle: the device card must offer Stop, not Launch.
                return True
            try:
                device.pop("pid", None)
                self._save_device(device)
            except Exception:
                pass
            return False
        # Emulator started outside the app (or before a restart): match it by
        # AVD name so the dashboard shows the device as running. The resolved
        # serial must actually be connected - a stale saved serial is not
        # evidence that the device is up. (No healing here: _is_running runs
        # on every dashboard refresh; the action endpoints heal on demand.)
        try:
            resolved = self.get_device_serial(name, persist=True, heal=False)
        except Exception:
            resolved = None
        if resolved and self._adb_serial_present(resolved):
            return True
        # Last resort for adb-blind situations: the emulator console answers
        # with this device's AVD name even when `adb devices` is empty.
        if device:
            return self._console_confirms(device)
        return False

    def _may_kill_for_device(self, pid, device):
        """May Stop kill *pid* on behalf of *device*? Only with proof.

        Proof is one of:
        * it is a process this manager launched (live Popen record),
        * its executable is an emulator/qemu („emulator", „qemu-system-*"),
        * the emulator console of this device's port answers with its AVD -
          i.e. this device is the one running there.

        A recycled pid (any random process) matches none of these and is never
        signalled; a foreign program on the port is reported instead.
        """
        if not pid or not DeviceManager._pid_alive(pid):
            return False
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        if pid == os.getpid():
            return False
        device = device or {}
        entry = self._running_processes.get(device.get("name"))
        if entry and entry.get("pid") == pid:
            proc = entry.get("process")
            return proc is None or proc.poll() is None
        if DeviceManager._pid_looks_like_emulator(pid):
            return True
        port = device.get("console_port")
        return bool(port and self._console_owner(port, device.get("avd_name")))

    def _emulator_evidence(self, device):
        """Something of ours still runs for *device* though no console answers.

        Used before Stop/`_is_running` discard a pid. A helper child holding
        the console port belongs to the emulator, and an emulator-shaped
        process with no listener yet is still booting - neither may be
        forgotten just because the console is silent.
        """
        device = device or {}
        name = device.get("name")
        for pid in (device.get("pid"),
                    (self._running_processes.get(name) or {}).get("pid")):
            if pid and self._pid_looks_like_emulator(pid):
                return True
        for port in (device.get("console_port"),
                     (int(device["console_port"]) + 1) if device.get("console_port") else None):
            owner = self._port_owner_pid(port) if port else None
            if owner and self._pid_is_ours(owner):
                return True
        return False

    @staticmethod
    def _pid_looks_like_emulator(pid):
        """True when *pid* alive AND really an emulator/qemu process.

        Stop and "is it still running?" both face the same question: a
        persisted pid may have been recycled by an unrelated process (very
        common on Windows). Never signal a process just because its number
        matches - look at what it is first. Unknown (no `ps`/`tasklist`
        detail) counts as False: refusing to kill is always the safer error.

        The fake-SDK tests set ``EMULATOR_PID_TRUST=1`` because their stub
        emulator is a shell script, not an emulator binary.
        """
        if not pid or not DeviceManager._pid_alive(pid):
            return False
        if os.environ.get("EMULATOR_PID_TRUST") == "1":
            return True
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        return DeviceManager._pid_is_emulator(pid)

    @staticmethod
    def _pid_exe_name(pid):
        """Executable name of *pid* ('' when unknown). Not its arguments.

        Matching the *arguments* is how a shell whose command line happens to
        mention "emulator" - this app's own test harness, a terminal, a script
        - could be mistaken for an emulator and killed. Only the executed
        file counts.
        """
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return ""
        if os.name == "nt":
            try:
                out = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                    capture_output=True, text=True, timeout=10,
                ).stdout or ""
            except Exception:
                return ""
            line = out.strip().splitlines()[0] if out.strip() else ""
            cols = [c.strip().strip('"') for c in line.split('","')]
            return cols[0].lower() if cols else ""
        try:
            with open(f"/proc/{pid}/comm", "r", encoding="utf-8",
                      errors="replace") as fh:
                return fh.read().strip().lower()
        except OSError:
            pass
        try:
            out = subprocess.run(
                ["ps", "-p", str(pid), "-o", "comm="],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
        except Exception:
            return ""
        return os.path.basename(out).lower()

    @staticmethod
    def _pid_is_emulator(pid):
        """True when *pid* is an emulator/qemu process (or belongs to one)."""
        name = DeviceManager._pid_exe_name(pid)
        if not name:
            return False
        if "emulator" in name or "qemu" in name:
            return True
        # A child of an emulator (a helper the VM spawned, qemu under
        # emulator.exe on Windows) belongs to that emulator.
        return DeviceManager._pid_has_emulator_ancestor(pid)

    @staticmethod
    def _emulator_root(pid):
        """The process to kill so that *pid* and its tree are really gone.

        That is *pid*'s own session leader (the emulator we started with
        ``start_new_session``), which covers every child - a lone port-holding
        helper would otherwise survive its parent. Strictly bounded: the walk
        never leaves *pid*'s session and never returns anything in ours, so a
        recycled or unrelated pid cannot drag this app (or the shell that
        started it) into the kill.
        """
        try:
            own_sid = os.getsid(0)
        except OSError:
            own_sid = None
        candidates = [pid] + DeviceManager._pid_ancestors(pid)
        for candidate in candidates:
            try:
                sid = os.getsid(candidate)
            except OSError:
                continue
            if own_sid is not None and sid == own_sid:
                # Our own session: never signal anything in it.
                break
            if sid == candidate:  # session leader of a foreign session
                return candidate
        return pid

    @staticmethod
    def _pid_ancestors(pid, limit=6):
        """[ppid, ppid of ppid, ...] for *pid* (POSIX; empty if unknown)."""
        chain = []
        seen = set()
        current = pid
        for _ in range(limit):
            try:
                with open(f"/proc/{current}/stat", "r", encoding="utf-8",
                          errors="replace") as fh:
                    fields = fh.read().rsplit(")", 1)[1].split()
                ppid = int(fields[1])
            except (OSError, IndexError, ValueError):
                try:
                    out = subprocess.run(
                        ["ps", "-p", str(current), "-o", "ppid="],
                        capture_output=True, text=True, timeout=5,
                    ).stdout.strip()
                    ppid = int(out.split()[0]) if out else 0
                except Exception:
                    break
            if ppid <= 1 or ppid in seen:
                break
            seen.add(ppid)
            chain.append(ppid)
            current = ppid
        return chain

    @staticmethod
    def _pid_has_emulator_ancestor(pid):
        """True when any ancestor of *pid* is the emulator that launched it."""
        for ancestor in DeviceManager._pid_ancestors(pid):
            name = DeviceManager._pid_exe_name(ancestor)
            if name and ("emulator" in name or "qemu" in name):
                return True
        return False

    @staticmethod
    def _pid_alive(pid):
        """Non-destructive "is this process alive?" probe.

        NEVER use ``os.kill(pid, 0)`` for this on Windows: ``os.kill`` calls
        ``TerminateProcess()`` for any signal other than CTRL_C_EVENT /
        CTRL_BREAK_EVENT, so a signal-0 "probe" actually KILLS the process.
        That turned a dashboard refresh into an emulator kill switch — the
        device showed "Running" while adb correctly saw nothing. A stale pid
        could even belong to an unrelated process, which would be killed too.
        """
        if not pid:
            return False
        try:
            pid = int(pid)
        except (ValueError, TypeError):
            return False
        if pid <= 0:
            return False
        if os.name == "nt":
            try:
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                    capture_output=True, text=True, timeout=10,
                )
            except Exception:
                return False
            out = (result.stdout or "").strip()
            if not out or out.upper().startswith("INFO"):
                return False
            for line in out.splitlines():
                cols = [c.strip().strip('"') for c in line.split('","')]
                if len(cols) >= 2 and cols[1] == str(pid):
                    return True
            return False
        # POSIX: signal 0 probes existence without delivering a signal.
        try:
            os.kill(pid, 0)
        except (OSError, ValueError, TypeError):
            return False
        # A zombie has already exited: it still answers signal 0 (until its
        # parent reaps it), but it holds no ports and runs nothing. Counting
        # it as alive made Stop wait out its whole timeout and claim the
        # device was still running after it had been killed.
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return True  # no /proc (macOS): pid exists, that is all we can know
        # "pid (comm) STATE ..." - comm may contain spaces/parens.
        try:
            state = stat.rsplit(")", 1)[1].split()[0]
        except IndexError:
            return True
        return state not in ("Z", "X")

    @staticmethod
    def _kill_pid(pid):
        """Force-kill *pid*. Never raises (used on best-effort cleanup paths)."""
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    capture_output=True,
                    timeout=10,
                )
            else:
                os.kill(int(pid), 9)
        except Exception:
            pass

    @staticmethod
    def _kill_process_tree(pid):
        """Force-kill *pid* **and all of its children**.

        This is the important bit for Stop on Windows: ``emulator.exe``
        launches the actual virtual machine as a *separate*
        ``qemu-system-x86_64.exe`` child. Killing only the pid we recorded
        leaves that child running - it keeps the console/adb ports bound and
        the AVD's ``hardware-qemu.ini.lock`` / userdata locks, so the next
        Launch dies with "port in use" / "another emulator instance is
        running" and adb keeps listing a device that no longer has a UI.

        ``taskkill /T`` on Windows, process-group / ``pkill -P`` + retry on
        POSIX. Never raises.
        """
        if not pid:
            return
        try:
            pid = int(pid)
        except (ValueError, TypeError):
            return
        if pid <= 0:
            return

        if os.name == "nt":
            for args in (
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                ["taskkill", "/PID", str(pid), "/F"],
            ):
                try:
                    subprocess.run(args, capture_output=True, timeout=15)
                except Exception:
                    pass
                if not DeviceManager._pid_alive(pid):
                    break
            return

        # POSIX: the emulator was started in its own session (setsid), so its
        # process group can be signalled at once. Guard hard: signalling a
        # group we are part of would kill this app (and everything else in
        # the shell), and a recycled pid could point at anything.
        try:
            own_pgid = os.getpgid(0)
        except OSError:
            own_pgid = None
        try:
            pgid = os.getpgid(pid)
        except OSError:
            pgid = None
        own_group = pgid == pid and pgid != own_pgid  # a session/group leader

        def _signal_tree(sig):
            # Kill the whole tree, deepest first: a surviving qemu child keeps
            # the console/adb ports bound even after its parent is gone.
            try:
                subprocess.run(["pkill", f"-{sig}", "-P", str(pid)],
                               capture_output=True, timeout=10)
            except Exception:
                pass
            if own_group:
                try:
                    os.killpg(pgid, sig)
                except OSError:
                    pass
            try:
                os.kill(pid, sig)
            except OSError:
                pass

        # Escalate: SIGTERM first so the emulator can flush its userdata
        # snapshot, then SIGKILL whatever ignored it. The old single-shot
        # SIGKILL left a half-written snapshot and killed no children that
        # had already been re-parented.
        if DeviceManager._pid_alive(pid):
            _signal_tree(15)
            for _ in range(30):  # up to ~3s
                if not DeviceManager._pid_alive(pid):
                    break
                time.sleep(0.1)
        for _ in range(5):
            if not DeviceManager._pid_alive(pid):
                break
            _signal_tree(9)
            time.sleep(0.2)

    # ------------------------------------------------------------------
    # Lingering-emulator cleanup (the "can't launch after Stop" bug)
    # ------------------------------------------------------------------
    def _console_owner(self, port, expected_avd=None):
        """True when an emulator console answers on *port* (for *expected_avd*)."""
        if not port:
            return False
        try:
            avd = emulator_console.probe_console(int(port))
        except Exception:
            return False
        if avd is None:
            return False
        return (expected_avd is None) or (avd == expected_avd)

    def _ports_busy(self, console_port):
        """True when the console/adb port pair still has a listener."""
        if not console_port:
            return False
        pair = (int(console_port), int(console_port) + 1)
        if any(self._port_in_use(port) for port in pair):
            return True
        listeners = self._listener_ports()
        return bool(listeners and any(port in listeners for port in pair))

    def _wait_until_stopped(self, name, timeout=8.0):
        """Wait for the emulator of *name* to really be gone.

        "Gone" means: the console no longer answers, its ports are free, the
        recorded pid died, no stray emulator process for this AVD/port is
        left, and adb dropped the serial. The old code returned as soon as the
        *pid* disappeared - on Windows the real VM lives in a child process,
        and real adb keeps listing a dead emulator for a few seconds, which is
        exactly what made a following Launch fail with "Device already
        running".
        """
        device = self._load_device(name) or {}
        console_port = device.get("console_port")
        avd_name = device.get("avd_name")
        serial = device.get("serial")
        deadline = time.time() + timeout
        while time.time() < deadline:
            pid_gone = not self._pid_alive(device.get("pid"))
            console_gone = not self._console_owner(console_port, avd_name)
            ports_free = not self._ports_busy(console_port)
            adb_gone = not (serial and self._adb_serial_present(serial))
            strays_gone = not self._stray_emulator_pids(avd_name, console_port)
            if pid_gone and console_gone and ports_free and adb_gone and strays_gone:
                return True
            time.sleep(0.4)
        return False

    def _sweep_lingering_emulator(self, name, timeout=12.0):
        """Kill everything still holding this device's emulator, and say what.

        `emu kill` is a request, not a guarantee: the VM (a qemu child on
        Windows) can survive it, ignore SIGTERM, or belong to an earlier
        launch whose pid we no longer track. The old code did one tree kill
        and then declared the device stopped - which is how you end up with a
        card that says "Stopped" while the process keeps running.

        Returns a human-readable note about anything that had to be killed
        (empty when nothing was left over).
        """
        device = self._load_device(name) or {}
        console_port = device.get("console_port")
        adb_port = device.get("adb_port") or (
            int(console_port) + 1 if console_port else None
        )
        avd_name = device.get("avd_name")
        killed = []
        blockers = []
        deadline = time.time() + timeout
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            pids = []
            # (a) the pid we recorded. Trusted when it is a session leader we
            #     spawned (all launches do), or when it is recognisably an
            #     emulator. A plain recycled pid matches neither, and is never
            #     killed.
            for pid in (
                (self._running_processes.get(name) or {}).get("pid"),
                device.get("pid"),
            ):
                if pid and self._may_kill_for_device(pid, device):
                    pids.append(int(pid))
            # (b) whoever actually owns this device's emulator port pair: an
            #     emulator we launched, or one that outlived our launch record.
            #     A foreign program on those ports is NOT killed - it is
            #     reported by the blocked branch below.
            for port in (console_port, adb_port):
                owner = self._port_owner_pid(port)
                if owner and DeviceManager._pid_looks_like_emulator(owner):
                    pids.append(owner)
            # (c) stray emulator/qemu processes for this AVD from a launch we
            #     no longer track (app restart, crashed launch, ...)
            pids.extend(self._stray_emulator_pids(avd_name, console_port))
            pids = sorted({p for p in pids if p and p != os.getpid()})

            if not pids:
                if not self._ports_busy(console_port):
                    return "; ".join(dict.fromkeys(killed))
                # Something holds the port but it is not an emulator we may
                # kill (another app, another user, Android Studio). Name it
                # instead of waiting out the clock.
                holders = [
                    self._describe_port_holder(port)
                    for port in (console_port, adb_port)
                    if port and self._port_in_use(int(port))
                ]
                blockers.extend(h for h in holders if h)
                if attempt >= 2:
                    # Still busy after a real effort: say exactly what holds
                    # the port. Remaining holders are not emulators, so they
                    # are reported, never killed (the caller raises with this
                    # text, so the card can show it).
                    reasons = [b for b in dict.fromkeys(blockers) if b]
                    if reasons:
                        return "Still held: " + ", ".join(reasons)
                    return "; ".join(dict.fromkeys(killed)) or "port still busy"
                time.sleep(0.4)
                continue

            for pid in pids:
                was = self._pid_alive(pid)
                # Kill the emulator that owns this process, not just a helper.
                self._kill_process_tree(self._emulator_root(pid))
                if was and attempt == 1:
                    print(f"[DeviceManager] Stop swept a lingering emulator "
                          f"process for '{name}' (pid {pid}).")
                    killed.append(f"killed pid {pid}")
                    if avd_name:
                        log_hint = f"process {pid} (AVD {avd_name})"
                    else:
                        log_hint = f"process {pid}"
                    killed[-1] = log_hint
            time.sleep(0.6)
        return "; ".join(dict.fromkeys(killed))

    @staticmethod
    def _describe_port_holder(port):
        """Human description of what is bound to *port* (best effort)."""
        if not port:
            return ""
        pid = DeviceManager._port_owner_pid(port)
        if not pid:
            return f"port {port} is in use by an unidentified process"
        detail = ""
        try:
            if os.name == "nt":
                detail = (subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                    capture_output=True, text=True, timeout=10,
                ).stdout or "").strip()
            else:
                detail = (subprocess.run(
                    ["ps", "-p", str(pid), "-o", "args="],
                    capture_output=True, text=True, timeout=10,
                ).stdout or "").strip()
        except Exception:
            detail = ""
        if detail:
            return f"port {port} is held by pid {pid} ({detail[:80]})"
        return f"port {port} is held by pid {pid}"

    @staticmethod
    def _port_owner_pid(port):
        """PID of the process listening on *port* (None when unknown).

        `ss -lptn` on Linux, `lsof -nP` on macOS, `netstat -ano` on Windows.
        Best effort: a missing tool just means "cannot tell", never an error.
        """
        if not port:
            return None
        try:
            port = int(port)
        except (TypeError, ValueError):
            return None
        if os.name == "nt":
            try:
                out = subprocess.run(
                    ["netstat", "-ano", "-p", "TCP"],
                    capture_output=True, text=True, timeout=10,
                ).stdout or ""
            except Exception:
                return None
            for line in out.splitlines():
                cols = line.split()
                if len(cols) < 5:
                    continue
                local, state, pid = cols[1], cols[3].upper(), cols[4]
                if state != "LISTENING":
                    continue
                if local.rsplit(":", 1)[-1] == str(port) and pid.isdigit():
                    return int(pid)
            return None
        for cmd in (
            ["ss", "-lptnH"],
            ["lsof", "-nP", "-iTCP", f"-sTCP:LISTEN", "-Fp"],
        ):
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            except Exception:
                continue
            if res.returncode != 0:
                continue
            import re as _re

            for line in (res.stdout or "").splitlines():
                if cmd[0] == "ss":
                    # ... 0.0.0.0:5554 ... users:(("qemu-system",pid=1234,fd=9))
                    if f":{port} " not in line + " ":
                        continue
                    m = _re.search(r"pid=(\d+)", line)
                    if m:
                        return int(m.group(1))
                else:
                    # lsof -Fp prints "p1234" blocks after the matching socket
                    m = _re.match(r"^p(\d+)$", line.strip())
                    if m:
                        return int(m.group(1))
            return None
        return None

    def _stray_emulator_pids(self, avd_name, console_port=None):
        """pids of emulator/qemu processes that belong to this AVD or port."""
        if os.name == "nt":
            return []
        wanted = set()
        if avd_name:
            wanted.add(f"avd {avd_name}")
            wanted.add(f"avd {avd_name.lower()}")
        if console_port:
            wanted.add(f"-port {int(console_port)}")
        if not wanted:
            return []
        try:
            res = subprocess.run(
                ["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=10
            )
        except Exception:
            return []
        found = []
        for line in (res.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2 or not parts[0].isdigit():
                continue
            pid, args = int(parts[0]), parts[1]
            if pid == os.getpid():
                continue
            # Only real emulator processes: our own app's argv mentions AVDs
            # too (python main.py launch ... -avd ...), so match the binary.
            exe = args.split()[0] if args.split() else ""
            if not (exe.endswith("/emulator") or exe.endswith("/emulator64-x86")
                    or "qemu-system" in os.path.basename(exe)):
                continue
            if any(token in args for token in wanted):
                found.append(pid)
        return found

    def _force_release(self, name):
        """Last resort: free this device's ports and forget stale runtime state.

        Called when an emulator refuses to die (or leaves children behind).
        Only touches *this* device's console port pair, so other running
        emulators and other processes are safe.
        """
        device = self._load_device(name) or {}
        console_port = device.get("console_port")
        pid = device.get("pid")
        if pid and self._may_kill_for_device(pid, device):
            self._kill_process_tree(pid)
        if console_port and self._console_owner(console_port, device.get("avd_name")):
            # The console is still ours but ignored `emu kill`: nothing left
            # to do beyond the tree kill above, so report it honestly.
            print(
                f"[DeviceManager] Warning: an emulator for '{name}' still "
                f"answers on console port {console_port} after a stop - it may "
                "be a different copy of the emulator holding the port."
            )
        self._boot_id_cache.pop(device.get("serial") or "", None)
        # "Released" means the console is silent *and* the port pair is free.
        # Checking the console alone said "freed" while another process still
        # held the port - the next Launch then failed with "port in use" on a
        # card that had reported a successful stop.
        return (not self._console_owner(console_port, device.get("avd_name"))
                and not self._ports_busy(console_port))

    def _cleanup_process_entry(self, name):
        entry = self._running_processes.pop(name, None)
        if entry:
            proc = entry.get("process")
            if proc is not None:
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
            log_file = entry.get("log_file")
            if log_file:
                try:
                    log_file.close()
                except Exception:
                    pass

    def _adb_serial_present(self, serial):
        if not Path(config.ADB_PATH).exists():
            return False
        try:
            result = subprocess.run(
                [config.ADB_PATH, "devices"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            for line in (result.stdout or "").splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == serial and parts[1] == "device":
                    return True
        except Exception:
            pass
        return False

    def _used_console_ports(self):
        """Console ports currently occupied by *running* emulators only."""
        used = set()
        # 1. Tracked processes still alive in this DeviceManager
        for entry in list(self._running_processes.values()):
            port = entry.get("console_port")
            proc = entry.get("process")
            alive = (proc is not None and proc.poll() is None) or self._pid_alive(entry.get("pid"))
            if port and alive:
                used.add(int(port))
        # 2. Live emulators visible in ADB
        if Path(config.ADB_PATH).exists():
            try:
                result = subprocess.run(
                    [config.ADB_PATH, "devices"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                for line in (result.stdout or "").splitlines()[1:]:
                    parts = line.split()
                    if len(parts) >= 2 and parts[0].startswith("emulator-"):
                        try:
                            used.add(int(parts[0].split("-")[1]))
                        except ValueError:
                            pass
            except Exception:
                pass
        # 3. Saved devices whose recorded emulator process/console is actually alive
        for device_file in self.devices_dir.glob("*.json"):
            try:
                with open(device_file, "r", encoding="utf-8") as f:
                    device = json.load(f)
                port = device.get("console_port")
                if not port:
                    continue
                port_int = int(port)
                if port_int in used:
                    continue
                if self._pid_alive(device.get("pid")) and (
                    self._console_confirms(device) or self._recently_launched(device)
                ):
                    used.add(port_int)
            except Exception:
                continue
        return used

    @staticmethod
    def _port_in_use(port):
        """Is *port* really held by a live process?

        Three probes, most authoritative first:

        1. the listener table (``ss``/``lsof``/``netstat``) - the ground truth;
        2. ``connect()`` - success or a timeout (full accept backlog) means a
           live but possibly wedged process holds it, ``ECONNREFUSED`` means
           nothing is listening;
        3. ``bind()`` - only used when no listener table is available. It can
           fail because of a harmless TIME_WAIT entry, which is why it must
           never be the sole reason to call a port "in use" (that made Stop
           claim a fully stopped device was still running).
        """
        import socket

        if not port:
            return False
        try:
            port = int(port)
        except (TypeError, ValueError):
            return False

        listeners = DeviceManager._listener_ports()
        if listeners:
            if port in listeners:
                return True
            # No listener: only a live-but-wedged process could still answer
            # (its socket may be stuck but it is listening, so this is rare).
            return DeviceManager._connect_says_busy(port)

        if DeviceManager._connect_says_busy(port):
            return True
        if os.name == "nt":  # bind semantics differ; connect is enough
            return False
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True
        finally:
            probe.close()

    @staticmethod
    def _connect_says_busy(port, timeout=0.3):
        """connect() probe: True unless the port clearly refuses connections."""
        import socket

        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
                return True
        except ConnectionRefusedError:
            return False
        except OSError:
            # Timeout / unreachable: a live process with a full accept
            # backlog, or a firewalled port. Assume it is taken.
            return True

    @staticmethod
    def _pid_is_ours(pid):
        """True when *pid* is a session leader this app started.

        Every emulator launch uses ``start_new_session=True``, so its session
        leader is the emulator itself and never this app (or the shell that
        launched it). Refusing to consider anything in our own session "ours"
        also makes it impossible to target this process by mistake.
        """
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        if pid <= 0 or not DeviceManager._pid_alive(pid):
            return False
        try:
            if pid == os.getpid():
                return False
            os_getsid = getattr(os, "getsid", None)
            if os_getsid is None:  # Windows
                return True
            sid = os_getsid(pid)
            return sid == pid and sid != os_getsid(0)
        except OSError:
            return False

    @staticmethod
    def _listener_ports():
        """Set of *our* TCP ports with a live listener (empty if unknown).

        Only listeners owned by a process this app started are counted. The
        emulator binds its console/adb port on every local interface, so a
        stopped device's port can still show a listener in a *sibling network
        namespace* (e.g. the host view inside a container) - that entry does
        not mean the device is running, and treating it as "still in use" made
        Stop refuse a device that was already fully gone. ``ss`` also omits the
        pid for processes it cannot inspect, which we skip for the same reason.
        """
        ports = set()
        if os.name == "nt":
            try:
                out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                     capture_output=True, text=True, timeout=10).stdout or ""
            except Exception:
                return ports
            for line in out.splitlines():
                cols = line.split()
                if len(cols) >= 5 and cols[3].upper() == "LISTENING":
                    tail = cols[1].rsplit(":", 1)[-1]
                    pid = cols[4]
                    if tail.isdigit() and pid.isdigit() and (
                            DeviceManager._pid_looks_like_emulator(int(pid))
                            or DeviceManager._pid_is_ours(int(pid))):
                        ports.add(int(tail))
            return ports

        import re as _re

        try:
            res = subprocess.run(["ss", "-lptnH"], capture_output=True,
                                 text=True, timeout=10)
        except Exception:
            res = None
        if res is not None and res.returncode == 0 and (res.stdout or "").strip():
            for line in res.stdout.splitlines():
                # LISTEN 0 5 169.254.0.21:5554 0.0.0.0:* users:(("qemu",pid=42,fd=9))
                m_pid = _re.search(r"pid=(\d+)", line)
                if not m_pid or not (DeviceManager._pid_looks_like_emulator(int(m_pid.group(1)))
                                     or DeviceManager._pid_is_ours(int(m_pid.group(1)))):
                    continue
                cols = line.split()
                if len(cols) >= 4:
                    tail = cols[3].rsplit(":", 1)[-1]
                    if tail.isdigit():
                        ports.add(int(tail))
            return ports
        # macOS: lsof lists listeners with their pid.
        try:
            res = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpn"],
                                 capture_output=True, text=True, timeout=10)
        except Exception:
            return ports
        current = None
        for line in (res.stdout or "").splitlines():
            if line.startswith("p") and line[1:].isdigit():
                current = int(line[1:])
            elif line.startswith("n") and current and (
                    DeviceManager._pid_looks_like_emulator(current)
                    or DeviceManager._pid_is_ours(current)):
                tail = line.rsplit(":", 1)[-1]
                if tail.isdigit():
                    ports.add(int(tail))
        return ports

    def _clear_stale_avd_locks(self, device):
        """Remove lock files a hard-killed emulator left in the AVD folder.

        The emulator refuses to start with "another emulator instance is
        running" when ``<avd>.avd/hardware-qemu.ini.lock`` (and friends) are
        still there. Only called right before a launch, after the console
        probe proved nothing is running for this device, so removing them
        cannot disturb a live emulator.
        """
        avd_name = (device or {}).get("avd_name")
        if not avd_name:
            return []
        avd_dir = config.AVD_HOME / f"{avd_name}.avd"
        removed = []
        for pattern in ("*.lock", "*.lock.d", "snapshots/*.lock"):
            for lock in list(avd_dir.glob(pattern)):
                try:
                    if lock.is_dir():
                        shutil.rmtree(lock, ignore_errors=True)
                    else:
                        lock.unlink()
                    removed.append(lock.name)
                except OSError:
                    pass
        if removed:
            print(f"[DeviceManager] Removed stale AVD lock(s) for {avd_name}: "
                  + ", ".join(sorted(removed)))
        return removed

    def _launch_blocker(self, name):
        """True when *name* really has a live emulator (not a stale ghost).

        Refusing to launch because of a stale adb entry / recorded pid used to
        leave a stopped device permanently un-launchable right after Stop:
        real adb keeps listing a dead emulator for a few seconds, and on
        Windows a half-dead process can linger on the console port. The
        emulator console - which also works when adb is confused - decides.
        """
        if not self._is_running(name):
            return False

        device = self._load_device(name) or {}
        console_port = device.get("console_port")
        avd_name = device.get("avd_name")

        # A live, answering console is conclusive proof it is running.
        if self._console_owner(console_port, avd_name):
            return True

        # No console yet: a tracked child process is trusted for the first
        # seconds after launch (the console comes up a moment later).
        entry = self._running_processes.get(name)
        proc = (entry or {}).get("process")
        if proc is not None and proc.poll() is None:
            if self._recently_launched(device, within_seconds=90):
                return True
            # Alive long after launch and still no console: stuck emulator.
            print(f"[DeviceManager] Process for '{name}' is alive but no emulator "
                  "console answers - it is stuck. Stopping it before relaunching.")
            try:
                self.stop_device(name)
            except Exception as exc:
                print(f"[DeviceManager] Could not clean up '{name}': {exc}")
            return self._console_owner(console_port, avd_name)

        # Not a child of this process any more (app restart, or it was started
        # elsewhere). A recorded pid that is still alive right after launch
        # belongs to a booting emulator - respect it.
        if self._pid_alive(device.get("pid")) and self._recently_launched(
            device, within_seconds=90
        ):
            return True

        # Everything else is a ghost: adb still lists the dead emulator, or a
        # stop just happened and adbd has not deregistered yet. Clear it and
        # let the launch proceed instead of blocking the device forever.
        print(f"[DeviceManager] '{name}' looked running but no emulator console "
              "answers - clearing the stale runtime state and launching.")
        self._boot_id_cache.pop(device.get("serial") or "", None)
        try:
            self._clear_stale_runtime(name)
        except Exception:
            pass
        return False

    def _clear_stale_runtime(self, name):
        """Forget runtime keys that belong to a previous (dead) emulator."""
        device = self._load_device(name)
        if device is None:
            return
        for key in ("pid", "runtime_boot_id"):
            device.pop(key, None)
        device["hooks_applied"] = False
        device["spoof_applied"] = False
        device["rooted"] = None
        self._save_device(device)

    def _allocate_console_port(self, name=None):
        used = self._used_console_ports()
        port = config.EMULATOR_PORT_START
        # A zombie emulator that adb no longer lists can still own the port;
        # launching onto it makes the new instance exit with "port in use".
        # The scan is bounded by both a port count and a wall-clock budget:
        # this runs before the first log line exists, so an unbounded loop
        # here looked exactly like "checking the SDK forever".
        deadline = time.time() + 20.0
        skipped = []
        for _attempt in range(64):
            if not (port in used or self._port_in_use(port) or self._port_in_use(port + 1)):
                if skipped and name:
                    self._progress_detail(
                        name, "preparing",
                        f"Console port {port} free (skipped busy: "
                        f"{', '.join(str(p) for p in skipped[-4:])})",
                    )
                return port
            skipped.append(port)
            if name:
                self._progress_detail(
                    name, "preparing",
                    f"Console port {port} is in use - trying {port + 2}",
                )
            port += 2
            if port > 65534:
                break
            if time.time() > deadline:
                if name:
                    self._progress_detail(
                        name, "preparing",
                        f"Port scan took longer than 20s (still checking {port})",
                    )
                break
        raise EmulatorLaunchError(
            "No free emulator console port available"
            + (f" (tried {len(skipped)} ports starting at "
               f"{config.EMULATOR_PORT_START})" if skipped else "")
            + ". Stop any leftover emulator processes and try again."
        )

    @staticmethod
    def _launch_hint(log_text):
        """Map common emulator startup errors onto a concrete fix."""
        text = (log_text or "").lower()
        hints = [
            ("requires hardware acceleration",
             "hardware acceleration is disabled — enable VT-x/AMD-V in BIOS and "
             "WHPX (Windows) / KVM (Linux), or install HAXM on older machines."),
            ("kvm is required",
             "KVM is not available — enable virtualization in BIOS and load kvm "
             "(sudo modprobe kvm_intel / kvm_amd), or add your user to the kvm group."),
            ("haxm",
             "Intel HAXM is missing/not running — install HAXM or switch to WHPX."),
            ("whpx",
             "Windows Hypervisor Platform is not enabled — turn on 'Windows "
             "Hypervisor Platform' and 'Virtual Machine Platform' in Windows features."),
            ("permission denied",
             "permission denied — check ownership/permissions of the AVD directory "
             f"({config.AVD_HOME}) and the SDK directory."),
            ("cannot access",
             "the AVD directory is not accessible — recreate the device or fix "
             f"permissions on {config.AVD_HOME}."),
            ("unknown avd",
             "the AVD definition is missing/corrupt — delete the device and create it again."),
            ("failed to allocate memory",
             "not enough RAM for the requested memory size — lower it with "
             "EMULATOR_RAM (default 4096 MB)."),
            ("gpu",
             "the GPU backend failed — retry with a software renderer, e.g. "
             "set EMULATOR_GPU_MODE=swiftshader_indirect."),
            ("another emulator instance",
             "another emulator already uses this port — stop it or relaunch."),
            ("webcam",
             "the emulator could not open the selected webcam - pick a different "
             "camera (webcam1, ...) or use 'emulated'/'virtualscene' in the device "
             "camera settings."),
            ("camera",
             "a camera source failed to initialise - check the camera mode "
             "(webcam0 needs a webcam connected to this machine)."),
            ("snapshot",
             "the quickboot snapshot is unusable — the device was started with "
             "-no-snapshot already, so try a wipe: `python main.py launch <name> --wipe`."),
        ]
        for needle, hint in hints:
            if needle in text:
                return hint
        return None

    def _acceleration_status(self):
        """Ask the emulator whether hardware acceleration is usable.

        Returns ``(True|False|None, message)``. ``None`` means "unknown"
        (e.g. the probe timed out), in which case we still try to launch.
        """
        if not Path(config.EMULATOR_PATH).exists():
            return None, "emulator binary not found"
        # `emulator -accel-check` spins up its own probe and on Windows/WHPX
        # can sit there for a minute. This is only ever a *warning*, so it
        # must never be what holds a launch in "checking..." - give it a
        # short budget and carry on when it does not answer in time.
        try:
            result = subprocess.run(
                [config.EMULATOR_PATH, "-accel-check"],
                capture_output=True, text=True, timeout=8,
            )
        except subprocess.TimeoutExpired:
            return None, "the acceleration probe did not answer within 8s (continuing anyway)"
        except Exception as exc:
            return None, str(exc)
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        lowered = output.lower()
        if result.returncode == 0:
            return True, output
        if "not" in lowered and ("install" in lowered or "usable" in lowered or "found" in lowered):
            return False, output
        # Non-zero but inconclusive output: don't block the launch.
        return None, output

    def wait_for_boot(self, serial, timeout=180, process=None, cancel=None,
                      on_progress=None):
        """Poll ``sys.boot_completed`` until the device is ready.

        A freshly started emulator shows up as ``offline`` in ``adb devices``
        for a while (and again after ``adb root``); that is normal, so the loop
        heals the connection instead of burning the whole boot timeout on it.

        ``cancel`` is an optional predicate; when it returns True the wait
        stops immediately with ``LaunchCancelledError`` (a Stop arrived).
        ``on_progress(elapsed, adb_state, seconds_left)`` is called about once
        a second so the dashboard can show how far the boot has come instead
        of an unchanging "Starting…".
        """
        if not Path(config.ADB_PATH).exists():
            raise SdkNotFoundError(f"ADB not found at {config.ADB_PATH}")
        start = time.time()
        offline_hits = 0
        server_restarted = False
        state = None
        last_report = 0.0
        while time.time() - start < timeout:
            if callable(on_progress):
                elapsed = time.time() - start
                if elapsed - last_report >= 1.0:
                    last_report = elapsed
                    try:
                        on_progress(elapsed, state, max(0, timeout - elapsed))
                    except Exception:
                        pass  # progress reporting must never break a boot
            if callable(cancel) and cancel():
                raise LaunchCancelledError(
                    f"Waiting for {serial} to boot was cancelled by a stop request."
                )
            if process is not None and process.poll() is not None:
                raise EmulatorLaunchError(
                    f"Emulator exited (code {process.returncode}) while waiting for boot."
                )
            try:
                state = adb.get_state(serial)
                if state == "device":
                    offline_hits = 0
                    if adb.boot_completed(serial, timeout=10):
                        return True
                elif state in ("offline", "unknown", None):
                    offline_hits += 1
                    if offline_hits == 4:
                        # A fresh emulator is briefly "offline" - but when its
                        # CONSOLE already answers while adb shows nothing at
                        # all, the adb server lost the registration. Restart
                        # it once to make every running emulator re-register.
                        try:
                            console_port = int(str(serial).rsplit("-", 1)[1])
                        except (ValueError, IndexError):
                            console_port = None
                        console_alive = (
                            console_port is not None
                            and emulator_console.probe_console(console_port) is not None
                        )
                        if console_alive and state is None and not server_restarted:
                            server_restarted = True
                            print(f"[DeviceManager] {serial} answers on its console "
                                  "port but adb can't see it - restarting adb server")
                            adb.restart_server(wait_timeout=20)
                        else:
                            ok, detail = adb.recover(serial, timeout=30)
                            print(f"[DeviceManager] adb reported '{state}' for {serial}; {detail}")
            except Exception:
                pass
            time.sleep(2)
        raise BootTimeoutError(
            f"Timed out waiting for {serial} to boot (last adb state: "
            f"{state or 'not connected'}). {adb.device_listing()}"
        )

    def get_log_path(self, name):
        return config.LOGS_DIR / f"{name}.log"

    def get_log_tail(self, name, lines=60):
        log_path = self.get_log_path(name)
        if not log_path.exists():
            return "(no log file yet)"
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
            tail = text.splitlines()[-lines:]
            return "\n".join(tail) if tail else "(log is empty)"
        except OSError as exc:
            return f"(could not read log: {exc})"

    def diagnose_device(self, name):
        """Full diagnostic snapshot for 'adb does not see my emulator' errors.

        Answers, per device: is the saved process alive? does the emulator
        console answer (and with which AVD)? what can adb see? what does the
        emulator log say? Used by the Spoof/Hooks endpoints so their error
        message names the real problem instead of just "adb is empty".
        """
        device = self._load_device(name) or {}
        serial = device.get("console_port")
        console_avd = emulator_console.probe_console(serial) if serial else None
        consoles = {} if console_avd else emulator_console.scan_consoles()
        return {
            "device": name,
            "avd_name": device.get("avd_name"),
            "pid": device.get("pid"),
            "pid_alive": self._pid_alive(device.get("pid")),
            "console_port": device.get("console_port"),
            "console_avd": console_avd,
            "consoles_found": consoles,
            "adb_describe": adb.describe_devices(include_avd=True),
            "adb_available": adb.adb_available(),
            "log_path": str(self.get_log_path(name)),
            "log_tail": self.get_log_tail(name, lines=12),
        }

    # ------------------------------------------------------------------
    # Spoofing (serial-aware, called after boot is confirmed)
    # ------------------------------------------------------------------
    def _stop_simulators(self, name):
        """Stop and clear any background battery/sensor simulators for *name*."""
        with self._hooks_lock:
            sims = self._simulators.pop(name, [])
        for sim in sims:
            try:
                sim.stop()
            except Exception:
                pass

    def start_simulators(self, name, serial, model="Samsung Galaxy S24"):
        """Start (or restart) battery and sensor simulators for *name*."""
        from spoof.battery_spoof import BatterySimulator
        from spoof.sensor_simulator import SensorSimulator

        self._stop_simulators(name)
        started_sims = []
        try:
            battery_sim = BatterySimulator(device_serial=serial)
            battery_sim.start_simulation(model)
            started_sims.append(battery_sim)
            print(f"[AntiDetect] Battery simulation started for {name}")
        except Exception as exc:
            print(f"[AntiDetect] Battery simulation warning: {exc}")

        try:
            sensor_sim = SensorSimulator(device_serial=serial)
            sensor_sim.start_simulation(mode="idle")
            started_sims.append(sensor_sim)
            print(f"[AntiDetect] Sensor simulation started for {name}")
        except Exception as exc:
            print(f"[AntiDetect] Sensor simulation warning: {exc}")

        if started_sims:
            with self._hooks_lock:
                self._simulators[name] = started_sims
        return len(started_sims)

    @classmethod
    def wants_hooks_on_launch(cls, device):
        """Should the launch chain re-attach Frida without being asked?

        Yes when the user asked for hooks before (``auto_hooks``) and they are
        not live on this boot: a reboot kills frida-server, so "hooks on" used
        to survive only if the launch flag happened to be set. The durable
        intent now drives it, which is what makes the whole chain unattended.
        """
        device = device or {}
        if not cls.wants_hooks(device):
            return False
        if device.get("hooks_applied") is True:
            return False
        return device.get("auto_hooks") is True or bool(device.get("hooks_applied_at"))

    def _apply_frida_hooks(self, device, serial="emulator-5554", on_complete=None):
        """Apply Frida hooks for anti-detection (background thread)."""
        from core.hook_manager import HookManager

        def run_hooks():
            name = device["name"]
            with self._hooks_lock:
                if name in self._hooks_in_flight:
                    return
                self._hooks_in_flight.add(name)
            applied = False
            err_text = None
            try:
                hooks = HookManager(device_serial=serial)
                if not hooks.wait_for_device(timeout=60):
                    err_text = f"Device {serial} did not become ready for Frida within 60s"
                    print(f"[AntiDetect] {err_text}")
                    try:
                        live = self._load_device(name)
                        if live is not None:
                            live["hooks_applied"] = False
                            live["hooks_error"] = err_text
                            self._save_device(live)
                    except Exception:
                        pass
                    return

                if hooks.apply_all_protections(device["identity"]):
                    applied = True
                    print(f"[AntiDetect] Frida hooks applied to {name}")
                    try:
                        live = self._load_device(name)
                        if live is not None:
                            live["hooks_applied"] = True
                            live["hooks_applied_at"] = datetime.now().isoformat()
                            live.pop("hooks_error", None)
                            # Remember the request so a reboot/app restart
                            # puts the hooks back automatically.
                            if live.get("auto_hooks") is not False:
                                live["auto_hooks"] = True
                            self._remember_boot_id(live, serial)
                            self._save_device(live)
                    except Exception:
                        pass
                else:
                    err_text = hooks.last_error or "Frida hooks did not attach"
                    print(f"[AntiDetect] Failed to apply Frida hooks for {name}: {err_text}")
                    try:
                        live = self._load_device(name)
                        if live is not None:
                            live["hooks_applied"] = False
                            live["hooks_error"] = err_text
                            self._save_device(live)
                    except Exception:
                        pass

                self.start_simulators(name, serial, device.get("model", "Samsung Galaxy S24"))
            finally:
                with self._hooks_lock:
                    self._hooks_in_flight.discard(name)
                if callable(on_complete):
                    try:
                        on_complete(name, applied, err_text)
                    except Exception:
                        pass

        thread = threading.Thread(target=run_hooks, daemon=True)
        thread.start()

    def set_device_proxy(self, name, proxy_url):
        """Validate, persist, and (when running) live-apply proxy config for *name*."""
        from anonymity.proxy_manager import ProxyManager

        device = self._load_device(name)
        if not device:
            raise ValueError(f"Device not found: {name}")

        cleaned = (proxy_url or "").strip() or None
        parsed_scheme = None
        if cleaned:
            pm_validator = ProxyManager()
            parsed_scheme, _, _ = pm_validator._parse_proxy_url(cleaned)

        device["proxy"] = cleaned
        self._save_device(device)

        live_applied = False
        note = "Saved to device profile."
        serial = self.get_device_serial(name, heal=False)
        if serial:
            pm = ProxyManager(device_serial=serial)
            if cleaned is None:
                live_applied = pm.clear_proxy()
                note = "Proxy cleared on running device." if live_applied else (pm.last_error or "Cleared in profile.")
            elif parsed_scheme in ("http", "https"):
                live_applied = pm.set_proxy(cleaned)
                note = (
                    f"HTTP proxy applied live on {serial}."
                    if live_applied
                    else (pm.last_error or "Saved in profile; live update failed.")
                )
            else:
                note = (
                    "SOCKS proxy saved to device profile — relaunch the device "
                    "to apply it via emulator -http-proxy."
                )
        return {
            "proxy": cleaned,
            "live_applied": live_applied,
            "message": note,
        }

    def regenerate_identity(self, name):
        """Generate a brand-new unique identity (IMEI, Android ID, serial, MAC, etc.) for *name*."""
        device = self._load_device(name)
        if not device:
            raise ValueError(f"Device not found: {name}")

        model_profile = get_model_profile(device["model"])
        if not model_profile:
            raise ValueError(f"Unknown model profile: {device['model']}")

        new_identity = generate_device_identity(
            model_profile,
            android_version=device.get("android_version", "14"),
        )
        device["identity"] = new_identity
        # A brand-new identity means a brand-new GPU too (a new renderer, vendor,
        # driver version and extension list - always one coherent chip).
        device["gpu"] = {
            key: new_identity.get(key)
            for key in ("gl_renderer", "gl_vendor", "gl_version", "egl_vendor",
                        "gl_extensions", "gl_limits", "gpu_name", "gpu_hw",
                        "gpu_score", "gpu_year")
        }
        device.pop("boot_props", None)
        device.pop("spoof_report", None)
        device.pop("spoof_error", None)
        device["identity_regenerated_at"] = datetime.now().isoformat()
        self._save_device(device)

        running = self._is_running(name)
        return {
            "device": self.get_device(name),
            "running": running,
            "message": (
                "Identity regenerated. Click 'Spoof' / 'Re-hook' or relaunch the device to apply."
                if running
                else "New identity generated and saved. Launch the device to use it."
            ),
        }

    def get_crash_log(self, name, lines=80):
        """Return Android ``logcat -d -b crash`` output from the running emulator."""
        device = self._load_device(name)
        if not device:
            raise ValueError(f"Device not found: {name}")
        serial = self.get_device_serial(name, heal=False)
        if not serial:
            return "(device is not running — start it to inspect Android crash logs)"
        try:
            res = subprocess.run(
                [config.ADB_PATH, "-s", serial, "logcat", "-d", "-b", "crash", "-t", str(lines)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            out = (res.stdout or "").strip()
            return out if out else "(no Android crash entries recorded in logcat -b crash)"
        except Exception as exc:
            return f"(could not read crash log: {exc})"

    def hooks_in_flight(self, name):
        """True while this device's post-boot hook chain is still running."""
        with self._hooks_lock:
            return name in self._hooks_in_flight

    # ------------------------------------------------------------------
    # Radio gate (airplane mode until the spoof is verified)
    # ------------------------------------------------------------------
    @staticmethod
    def _airplane_required(device):
        """Should the radios be off right now?

        Yes while this device is *not* verified spoofed on the current boot:
        before the spoof is applied the guest still announces the emulator
        (its IMEI/baseband/network), so the safest state is airplane mode.
        Devices with the spoof switched off keep their radios on - that is the
        user's explicit choice (Details -> Keep spoofing after restart off).
        """
        device = device or {}
        if not DeviceManager.wants_spoof(device):
            return False
        return not DeviceManager._spoof_current(device)

    @staticmethod
    def _spoof_current(device):
        """True when the saved spoof provably belongs to the *live* boot.

        ``spoof_applied`` alone is not enough: it describes the boot it was
        applied on (an emulator reboot wipes the RAM-only props). A boot id
        mismatch means the device is unspoofed again, so the gate closes.
        """
        device = device or {}
        if device.get("spoof_applied") is not True:
            return False
        if device.get("runtime_stale"):
            return False
        return True

    def set_airplane_mode(self, name, enable):
        """Turn the device's radios on/off (airplane mode), verified.

        Returns ``(ok, message)``. The state is persisted on the device so the
        dashboard can show the gate honestly (and so a failed set never looks
        like a success).
        """
        device = self._load_device(name)
        if device is None:
            raise ValueError(f"Device not found: {name}")
        serial = device.get("serial") or self.get_device_serial(name, heal=False)
        if not serial:
            raise ValueError(
                f"'{name}' is not running - airplane mode can only be changed "
                "on a running emulator."
            )
        from spoof.system_spoof import SystemSpoofer

        ok = SystemSpoofer(device_serial=serial).set_airplane_mode(enable)
        boot = self._runtime_boot_id(serial)
        if ok:
            device["airplane_mode"] = bool(enable)
            device["airplane_mode_serial"] = serial
            device["airplane_mode_boot_id"] = boot
            device["airplane_mode_at"] = datetime.now().isoformat()
            device.pop("airplane_error", None)
            device.pop("airplane_unknown", None)
        else:
            device["airplane_error"] = (
                f"Could not turn airplane mode {'on' if enable else 'off'} on "
                f"{serial} - the device did not confirm the setting."
            )
            # Do not keep a state we could not read back (the next dashboard
            # refresh would then repeat it forever instead of retrying).
            device["airplane_mode"] = None
            device["airplane_unknown"] = True
        self._save_device(device)
        return ok, (
            f"Airplane mode switched {'on' if enable else 'off'} on {serial}"
            if ok else device["airplane_error"]
        )

    def enforce_airplane_gate(self, name, serial=None, force_off=False):
        """Keep the device's radios off until its spoof is verifiably applied.

        Called after every boot/restore pass, so a device that comes up
        unspoofed (fresh boot, failed spoof, app restart) sits in airplane mode
        instead of broadcasting the emulator's real identity - and comes back
        out of it as soon as the spoof is verified on that boot.

        ``force_off`` releases the gate unconditionally (used right after a
        verified spoof). Returns ``(state, message)`` where state is
        ``"on"``, ``"off"``, ``"unchanged"`` or ``"unavailable"``.
        """
        device = self._load_device(name)
        if device is None:
            return "unavailable", f"Device not found: {name}"
        serial = serial or device.get("serial") or self.get_device_serial(name, heal=False)
        if not serial:
            return "unavailable", f"'{name}' is not running - no radios to gate."
        want_on = False if force_off else self._airplane_required(device)
        current = device.get("airplane_mode")
        boot = self._runtime_boot_id(serial)
        # No boot id readable (adb offline): fall back to the cached state for
        # this same serial so a held gate is not toggled twice needlessly.
        same_boot = bool(
            (boot and device.get("airplane_mode_boot_id") == boot)
            or (not boot and device.get("airplane_mode_serial") == serial
                and device.get("airplane_mode_boot_id") is None)
        )
        if not want_on and same_boot and current is False:
            return "unchanged", f"Airplane mode already off on {serial}"
        if want_on and same_boot and current is True:
            return "unchanged", f"Airplane mode already on {serial}"
        ok, message = self.set_airplane_mode(name, want_on)
        if not ok:
            return "unavailable", message
        return ("on" if want_on else "off"), message

    def _apply_hardware_spoof(self, device, serial="emulator-5554"):
        """Apply hardware spoofing (boot already confirmed by caller).

        The spoofer now *verifies* every prop after staging (runtime setprop
        → /data/local.prop → reboot as needed) and returns an honest report,
        which is saved on the device so Details shows exactly which values
        the guest reports (and which could not be changed on this image).
        """
        from spoof.hardware_spoof import HardwareSpoofer

        try:
            spoofer = HardwareSpoofer(device_serial=serial)
            report = spoofer.spoof_all(device["identity"])
            device["spoof_report"] = report or None
            failed = (report or {}).get("failed") or []
            if failed:
                device["spoof_error"] = (
                    f"Could not change props {sorted(failed)} - Settings may "
                    "still show emulator values for these (Frida hooks cover "
                    "hooked apps)."
                )
                print(f"[AntiDetect] {device['name']}: {device['spoof_error']}")
            else:
                device.pop("spoof_error", None)
                print(
                    f"[AntiDetect] Hardware spoofing verified for "
                    f"{device['name']} ({(report or {}).get('method')})"
                )
            if not failed:
                # Verified on this boot: remember that the device *is* spoofed
                # so a reboot/app restart knows exactly what to re-apply.
                device["spoof_applied"] = True
                device["spoof_applied_at"] = datetime.now().isoformat()
                if device.get("auto_spoof") is not False:
                    device["auto_spoof"] = True
            else:
                device["spoof_applied"] = False
            self._save_device(device)
            if not failed:
                self._remember_boot_id(device, serial)
        except Exception as exc:
            device["spoof_error"] = f"Spoofing failed: {exc}"
            device["spoof_applied"] = False
            self._save_device(device)
            print(f"[AntiDetect] Hardware spoofing warning for {device['name']}: {exc}")

    def _remember_boot_id(self, device, serial):
        """Pin the boot id the current live spoof/hook state belongs to."""
        boot = self._runtime_boot_id(serial, fresh=True)
        if not boot:
            return
        device["runtime_boot_id"] = boot
        try:
            self._save_device(device)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Restoring the persisted intent
    # ------------------------------------------------------------------
    def restore_device(self, name, serial=None, spoof=None, hooks=None):
        """Re-apply the spoofing/hooks this device asked to keep.

        With no arguments this restores whatever the device was configured
        to keep (``auto_spoof`` / ``auto_hooks``) and is not applied right
        now - i.e. exactly "make the device match what I had before the
        restart". Pass ``spoof``/``hooks`` explicitly to force a full
        re-apply.
        """
        device = self._load_device(name)
        if device is None:
            raise ValueError(f"Device not found: {name}")

        serial = serial or self.get_device_serial(name, heal=False)
        stale, _reason = self._runtime_status(device, serial)
        spoof_pending, hooks_pending = self._pending_restore(device, stale=stale)
        want_spoof = spoof_pending if spoof is None else bool(spoof)
        want_hooks = hooks_pending if hooks is None else bool(hooks)
        if not want_spoof and not want_hooks:
            return {
                "success": False,
                "device": name,
                "spoofed": False,
                "hooks": False,
                "message": (
                    "Nothing to restore: this device has no saved spoofing/hooks "
                    "to put back (or they were switched off). Use Spoof/Hooks to "
                    "apply them."
                ),
            }

        if not serial:
            return {
                "success": False,
                "device": name,
                "spoofed": False,
                "hooks": False,
                "message": (
                    f"'{name}' is not running - launch it and the spoofing/hooks "
                    "will be applied automatically."
                ),
            }

        ready, state, detail = adb.wait_until_ready(serial, timeout=90)
        if not ready:
            return {
                "success": False,
                "device": name,
                "serial": serial,
                "spoofed": False,
                "hooks": False,
                "message": (
                    f"{serial} is not ready yet (state: {state or 'not connected'}"
                    + (f" - {detail}" if detail else "")
                    + "). Retry once it has fully booted."
                ),
            }

        # Root is required for both steps and never survives a reboot.
        device = self._load_device(name) or device
        self._maybe_auto_root(device, serial)

        spoofed = False
        if want_spoof:
            device = self._load_device(name) or device
            self._apply_hardware_spoof(device, serial)
            try:
                from spoof.system_spoof import SystemSpoofer

                SystemSpoofer(device_serial=serial).apply_all_spoofing(
                    device["identity"]
                )
            except Exception as exc:
                print(f"[DeviceManager] System spoof restore warning: {exc}")
            device = self._load_device(name) or device
            spoofed = device.get("spoof_applied") is True
            if spoofed:
                self.enforce_airplane_gate(name, serial, force_off=True)
            else:
                self.enforce_airplane_gate(name, serial)

        hooks_ok = False
        if want_hooks:
            from core.hook_manager import HookManager

            hook_mgr = HookManager(device_serial=serial)
            hooks_ok = bool(hook_mgr.apply_all_protections(device["identity"]))
            live = self._load_device(name) or device
            if hooks_ok:
                live["hooks_applied"] = True
                live["hooks_applied_at"] = datetime.now().isoformat()
                live.pop("hooks_error", None)
                self._save_device(live)
                try:
                    self.start_simulators(
                        name, serial, live.get("model", "Samsung Galaxy S24")
                    )
                except Exception:
                    pass
            else:
                live["hooks_applied"] = False
                live["hooks_error"] = hook_mgr.last_error or "Frida hooks did not attach"
                self._save_device(live)

        live = self._load_device(name) or device
        if spoofed or hooks_ok:
            self._remember_boot_id(live, serial)

        parts = []
        if want_spoof:
            parts.append("spoofing " + ("restored" if spoofed else "NOT restored"))
        if want_hooks:
            parts.append("Frida hooks " + ("restored" if hooks_ok else "NOT restored"))
        latest = self._load_device(name) or live
        return {
            "success": bool(spoofed or hooks_ok),
            "device": name,
            "serial": serial,
            "spoofed": spoofed,
            "hooks": hooks_ok,
            "spoof_error": latest.get("spoof_error"),
            "hooks_error": latest.get("hooks_error"),
            "message": ", ".join(parts) or "Nothing to restore.",
        }

    def restore_all_pending(self, only_running=True):
        """Restore every running device whose spoofing/hooks were lost.

        Used by the dashboard on startup (an app restart used to forget the
        whole chain) and by ``python main.py restore``.
        """
        results = []
        for device in self.list_devices():
            name = device.get("name")
            if not name:
                continue
            if only_running and not device.get("running"):
                continue
            drifted = self.spoof_drift(device)[0]
            if not (device.get("runtime_stale") or device.get("needs_restore") or drifted):
                continue
            print(f"[DeviceManager] Restoring spoofing/hooks for {name} ...")
            try:
                # autorecover also closes/opens the airplane gate to match the
                # verified result, so an unattended restore can never leave a
                # half-spoofed device broadcasting.
                results.append(self.autorecover(name, serial=device.get("serial")))
            except Exception as exc:  # never let one device break the sweep
                results.append({
                    "success": False,
                    "device": name,
                    "message": f"Restore failed: {exc}",
                })
        return results

    # ------------------------------------------------------------------
    # Automatic rooting
    # ------------------------------------------------------------------
    @staticmethod
    def _auto_root_enabled(device):
        """Per-device setting wins; otherwise the Settings toggle (on)."""
        if device.get("auto_root") is not None:
            return bool(device["auto_root"])
        return bool(config.load_settings().get("auto_root", True))

    def _maybe_auto_root(self, device, serial):
        """Run ``adb root`` right after boot so spoofing/Frida always have root.

        Never raises: root failures land in ``device["root_error"]`` and just
        get logged (spoofing retries root on its own anyway).
        """
        name = device["name"]
        if not self._auto_root_enabled(device):
            print(f"[DeviceManager] auto-root disabled for {name} - skipping adb root")
            return False

        if device.get("rootable") is False:
            device["rooted"] = False
            device["root_error"] = (
                "Play Store images refuse adb root. Recreate the device with the "
                "'Google APIs (root)' image to enable Frida and full spoofing."
            )
            self._save_device(device)
            print(f"[DeviceManager] {name}: {device['root_error']}")
            return False

        print(f"[DeviceManager] auto-rooting {name} ({serial})...")
        try:
            ok, detail = adb.root(serial, timeout=60)
        except Exception as exc:
            ok, detail = False, str(exc)

        device["rooted"] = bool(ok)
        device["rooted_at"] = datetime.now().isoformat() if ok else None
        device["root_error"] = None if ok else (detail or "adb root failed")
        try:
            self._save_device(device)
        except Exception:
            pass

        if ok:
            print(f"[DeviceManager] {name} rooted ({serial} is uid=0)")
        else:
            print(f"[DeviceManager] auto-root failed for {name}: {detail}")
        return bool(ok)

    def _apply_system_spoof(self, device, serial="emulator-5554"):
        """Apply system-level spoofing via ADB (boot already confirmed)."""
        from spoof.system_spoof import SystemSpoofer

        try:
            spoofer = SystemSpoofer(device_serial=serial)
            spoofer.apply_all_spoofing(device["identity"])
            # System settings only take effect with the radios already gated;
            # make sure the identity did not wipe the airplane flag.
            live = self._load_device(device["name"]) or device
            if self._airplane_required(live):
                spoofer.set_airplane_mode(True)
            print(f"[AntiDetect] System spoofing applied for {device['name']}")
        except Exception as exc:
            print(f"[AntiDetect] System spoofing warning for {device['name']}: {exc}")
