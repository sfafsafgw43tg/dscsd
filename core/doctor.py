"""Health checks for the whole stack - `python main.py doctor` / GUI Health panel.

Every failure so far on real machines traced back to something checkable in
advance (SDK layout, stale AVD, zombie emulator holding a port, frida
client/server version drift, empty system images). Doctor turns those into
one readable report: each check has ok/severity/detail and a concrete fix.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import config
from core import adb
from core.emulator_console import probe_console
from core.device_manager import DeviceManager

SEVERITY_ORDER = {"ok": 0, "info": 1, "warn": 2, "error": 3}


def _check(cid, title, ok, severity, detail, fix=""):
    if ok and severity != "info":
        severity = "ok"
    return {"id": cid, "title": title, "ok": bool(ok), "severity": severity,
            "detail": detail, "fix": fix}


def _adb_platform_tools_version():
    """Parse `adb version` -> (android_debug_bridge, platform_tools_major) or None."""
    try:
        result = subprocess.run(
            [config.ADB_PATH, "version"], capture_output=True, text=True, timeout=10)
    except Exception:
        return None, None
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    bridge = None
    platform = None
    # "Android Debug Bridge version 1.0.41" / "Version 35.0.2-12147458"
    m = re.search(r"Debug Bridge version ([\d.]+)", text)
    if m:
        bridge = m.group(1)
    m = re.search(r"Version (\d+)\.", text)
    if m:
        platform = int(m.group(1))
    return bridge, platform


def check_python():
    ok = sys.version_info >= (3, 9)
    return _check(
        "python", "Python version",
        ok, "error",
        f"Python {sys.version.split()[0]}",
        "" if ok else "Install Python 3.9+ from python.org (tick 'Add to PATH').",
    )


def check_sdk_layout():
    info = config.check_sdk()
    ok = all([info["sdk_root_found"], info["emulator_found"],
              info["adb_found"], info["avdmanager_found"], info["sdkmanager_found"]])
    missing = [k for k in ("emulator", "adb", "avdmanager", "sdkmanager")
               if not info[f"{k}_found"]]
    return _check(
        "sdk_layout", "Android SDK tools",
        ok, "error",
        (f"SDK at {info['sdk_root']}" if ok
         else f"Missing {', '.join(missing)} in {info['sdk_root'] or '(no SDK root)'}"),
        "" if ok else "Run: python main.py setup (it installs platform-tools, "
                      "emulator, cmdline-tools).",
    )


def check_cmdline_duplicates():
    """Two cmdline-tools copies (latest + latest-2) confuse avdmanager/sdkmanager."""
    root = config.ANDROID_SDK_ROOT
    if not root:
        return _check("cmdline_dups", "cmdline-tools copies", True, "info",
                      "SDK root not set.")
    ct = Path(root) / "cmdline-tools"
    if not ct.is_dir():
        return _check("cmdline_dups", "cmdline-tools copies", True, "info",
                      "no cmdline-tools directory yet.")
    dirs = sorted(p.name for p in ct.iterdir() if p.is_dir())
    ok = len(dirs) <= 1
    return _check(
        "cmdline_dups", "cmdline-tools copies",
        ok, "warn",
        f"found: {', '.join(dirs) if dirs else '(none)'}",
        "" if ok else (
            "Delete/merge the extra copy: avdmanager prints 'inconsistent "
            "location' warnings and some commands pick the wrong one. Keep the "
            f"one that actually contains bin\\sdkmanager. ({ct})"),
    )


def check_platform_tools_age():
    bridge, platform = _adb_platform_tools_version()
    if platform is None:
        return _check("platform_tools", "platform-tools version", False, "warn",
                      f"could not parse `adb version` output (bridge {bridge or '?'})",
                      "Update: sdkmanager --install platform-tools")
    ok = platform >= 33  # Android 13-era; below this pairing with new images is fragile
    return _check(
        "platform_tools", "platform-tools version",
        ok, "error",
        f"platform-tools {platform}.x (adb bridge {bridge})",
        "" if ok else ("Update platform-tools (sdkmanager --install platform-tools). "
                       "Old builds break adb against Android 13+ system images "
                       "(offline devices that never come back)."),
    )


def check_adb_server():
    try:
        result = subprocess.run(
            [config.ADB_PATH, "devices"], capture_output=True, text=True, timeout=10)
        ok = result.returncode == 0 and "List of devices attached" in (result.stdout or "")
        detail = (result.stdout or "").strip().replace("\n", " | ") or (
            result.stderr or "").strip()
    except Exception as exc:
        ok, detail = False, str(exc)
    return _check(
        "adb_server", "adb server responds",
        ok, "error",
        detail,
        "" if ok else "Restart adb: adb kill-server && adb start-server "
                      "(or press 'Repair ADB' in Settings).",
    )


def check_system_images():
    installed, missing = [], []
    for ver, info in config.ANDROID_VERSIONS.items():
        (installed if config.is_system_image_installed(info) else missing).append(ver)
    return _check(
        "system_images", "System images",
        bool(installed), "warn",
        f"installed: {', '.join(installed) or 'none'} · missing: {', '.join(missing) or 'none'}",
        "" if installed else ("No device can launch without one: "
                              "python main.py setup --android 14"),
    )


def check_settings_file():
    path = config.SETTINGS_FILE
    if not path.exists():
        return _check("settings_file", "Settings file", True, "info",
                      "no settings file yet (defaults in use).")
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return _check("settings_file", "Settings file", True, "ok",
                      f"{path.name} parses.")
    except Exception as exc:
        return _check("settings_file", "Settings file", False, "warn",
                      f"{path}: {exc}",
                      "Delete the file; defaults will be regenerated.")


def check_gpu_identity():
    """Every device's GPU identity must be one coherent chip.

    Checks the parts anti-emulator SDKs cross-reference: renderer name ==
    vendor (Adreno->Qualcomm, Mali->ARM, Xclipse->Samsung), the extension list
    must match that vendor, and the numeric limits must be present. A mismatch
    (or a missing identity on an existing device) is reported with the fix.
    """
    from profiles.gpu_profiles import gpu_profile, vendor_for_renderer
    from spoof.gpu_spoof import build_gpu_identity

    # Read the saved device JSONs directly: list_devices() derives liveness,
    # which (correctly) clears a recycled pid - a side effect a health check
    # must never have, or it would hide the very rows check_devices reports.
    try:
        import json as _json

        from core.device_manager import DeviceManager

        devices = []
        for path in sorted(DeviceManager().devices_dir.glob("*.json")):
            try:
                devices.append(_json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    except Exception as exc:
        return _check("gpu_identity", "GPU identity", False, "warn",
                      f"could not read devices: {exc}")

    if not devices:
        return _check("gpu_identity", "GPU identity", True, "info",
                      "no devices yet; a GPU identity is generated per device.")

    problems = []
    for device in devices:
        name = device.get("name", "?")
        try:
            # Pass the whole device: its top-level ``gpu`` block is what the
            # hooks actually reported, so a contradiction is visible here.
            gpu = build_gpu_identity(device)
        except Exception as exc:
            problems.append(f"{name}: {exc}")
            continue
        renderer = gpu.get("gl_renderer") or ""
        vendor = gpu.get("gl_vendor") or ""
        # The vendor must agree with the chip. For a chip from the table that
        # is the vendor its *name* implies (Adreno->Qualcomm, Mali->ARM,
        # Xclipse->Samsung), so a planted "Mali-G715 / Qualcomm" is caught;
        # for an unknown chip the profile it inherited decides.
        profile = gpu_profile(renderer)
        expected = (vendor_for_renderer(renderer) if profile.get("matched")
                    else profile["vendor"])
        if vendor != expected:
            problems.append(f"{name}: renderer {renderer!r} but vendor {vendor!r} "
                            f"(should be {expected!r})")
            continue
        exts = (gpu.get("gl_extensions") or "").split()
        if not exts:
            problems.append(f"{name}: no GL extension string")
            continue
        foreign = [e for e in exts
                   if ("QCOM" in e and vendor != "Qualcomm")
                   or ("ARM_" in e and vendor != "ARM")]
        if foreign:
            problems.append(f"{name}: {vendor} GPU reports another vendor's "
                            f"extensions {foreign[:2]}")
        if not (gpu.get("gl_limits") or {}).get("max_texture_size"):
            problems.append(f"{name}: GL limits missing")

    if problems:
        return _check("gpu_identity", "GPU identity", False, "warn",
                      "; ".join(problems[:4]),
                      "Run `python main.py create ...` for a fresh device, or run "
                      "`python apply_antidetect.py --device <name>` (Details -> New "
                      "identity) to regenerate and re-apply the GPU identity.")
    sample = build_gpu_identity(devices[0])
    return _check(
        "gpu_identity", "GPU identity", True, "ok",
        f"{len(devices)} device(s) report one coherent chip, e.g. "
        f"{sample.get('gl_vendor')} {sample.get('gl_renderer')} "
        f"({len((sample.get('gl_extensions') or '').split())} GL extensions, "
        f"{sample.get('gl_version')}).",
    )


def check_renderer_and_radios():
    """No device may render on the host GPU.

    ``hw.gpu.mode`` / ``gpu_mode`` on host passthrough (``auto``/``host``/
    ``angle``) means the guest loads the desktop driver and reports the host
    card, whatever the Frida hooks answer.

    Airplane mode is *not* judged here: the app never changes the radios on
    its own, so a device's airplane state is whatever the user chose.
    """
    from core.device_manager import GUEST_GPU_MODE, resolve_gpu_mode

    try:
        import json as _json

        from core.device_manager import DeviceManager

        manager = DeviceManager()
        devices = []
        for path in sorted(manager.devices_dir.glob("*.json")):
            try:
                devices.append(_json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    except Exception as exc:
        return _check("renderer_radios", "GPU renderer / radios", False, "warn",
                      f"could not read devices: {exc}")

    if not devices:
        return _check("renderer_radios", "GPU renderer / radios", True, "info",
                      f"no devices yet; launches use -gpu {GUEST_GPU_MODE} "
                      "(guest renderer, never the host GPU).")

    host_gl = []
    for device in devices:
        name = device.get("name", "?")
        # `resolve_gpu_mode` already protects the launch (it substitutes the
        # guest renderer), so a host mode here means the *configuration* is
        # still wrong - a pinned EMULATOR_GPU_MODE or a legacy device JSON -
        # and would leak the moment anything bypassed the launcher.
        configured = (device.get("gpu_mode") or config.DEFAULT_GPU_MODE or "").strip()
        if configured.lower() in config.HOST_GPU_MODES:
            host_gl.append(f"{name} ({configured})")

    if host_gl:
        return _check(
            "renderer_radios", "GPU renderer / radios", False, "warn",
            "these devices are configured for host-GPU passthrough, so the guest "
            f"would report your desktop card: {', '.join(host_gl)}",
            f"The launcher forces -gpu {GUEST_GPU_MODE} for spoofed devices, but "
            "clear the pinned value (EMULATOR_GPU_MODE) or relaunch the device "
            "once so its saved gpu_mode matches.",
        )
    return _check(
        "renderer_radios", "GPU renderer / radios", True, "ok",
        f"all {len(devices)} device(s) render on the guest stack "
        f"({GUEST_GPU_MODE}). Radios are never forced into airplane mode.",
    )


def check_frida_versions():
    try:
        from frida_toolkit.frida_server_manager import (
            client_version, target_version, FRIDA_SERVER_VERSION_PATH,
            FRIDA_SERVER_PATH, KNOWN_CRASHY_VERSIONS,
        )
    except Exception as exc:
        return _check("frida_versions", "Frida client/server versions", False, "warn",
                      f"could not load frida manager: {exc}")
    client = client_version()
    if client is None:
        return _check("frida_versions", "Frida client/server versions", False, "error",
                      "the `frida` python package is not installed",
                      "pip install frida-tools")
    wanted = target_version()
    if client in KNOWN_CRASHY_VERSIONS:
        return _check(
            "frida_versions", "Frida client/server versions", False, "error",
            f"client {client} has a known Android 14 x86_64 SIGSEGV crash bug (frida-core#1280/#1281)",
            "Upgrade immediately: pip install --upgrade frida frida-tools (17.25.2+ fixes the crash).",
        )
    cached = None
    try:
        cached = FRIDA_SERVER_VERSION_PATH.read_text().strip() or None
    except OSError:
        pass
    downloaded = FRIDA_SERVER_PATH.exists()
    if downloaded and cached and cached != wanted:
        return _check("frida_versions", "Frida client/server versions", False, "warn",
                      f"client {wanted} but a cached server is {cached}",
                      "Hooks auto-redownload on next install - or delete "
                      f"{FRIDA_SERVER_PATH} now.")
    return _check("frida_versions", "Frida client/server versions", True, "ok",
                  f"client {wanted}" + (f" · cached server {cached}" if cached
                                        else " · no cached server yet"))


def check_gpu_accel():
    """Probe the emulator's -accel-check (warn-only information)."""
    try:
        result = subprocess.run(
            [config.EMULATOR_PATH, "-accel-check"], capture_output=True,
            text=True, timeout=15)
        text = ((result.stdout or "") + (result.stderr or "")).strip()
    except Exception as exc:
        return _check("gpu_accel", "Hardware acceleration", False, "warn",
                      f"emulator -accel-check failed: {exc}",
                      "Enable VT-x/AMD-V in BIOS + 'Windows Hypervisor Platform' "
                      "(Windows) or KVM (Linux).")
    ok = bool(re.search(r"\b(KVM|WHPX|HAXM|AEHD|GHVF|Hyper-V)\b", text))
    return _check(
        "gpu_accel", "Hardware acceleration",
        ok, "warn",
        text.replace("\n", " | ")[:200],
        "" if ok else ("x86_64 images without acceleration are impossibly slow. "
                       "Enable VT-x/AMD-V in BIOS + WHPX (Windows) / KVM (Linux)."),
    )


def check_devices():
    """Per saved device: AVD files exist, no zombie port holders, stale runtime."""
    manager = DeviceManager()
    rows = []
    try:
        devices = manager.list_devices()
    except Exception as exc:
        return [_check("devices", "Saved devices", False, "error",
                       f"could not list devices: {exc}")]
    for device in devices:
        name = device["name"]
        problems, fixes = [], []
        avd_ini = config.AVD_HOME / f"{device.get('avd_name', '')}.ini"
        if not avd_ini.exists():
            problems.append("AVD ini missing")
            fixes.append("delete + recreate the device (a fresh AVD is built)")
        port = device.get("console_port")
        if port:
            avd = probe_console(int(port))
            if avd and avd != device.get("avd_name"):
                problems.append(f"port {port} held by different AVD '{avd}'")
                fixes.append("stop that emulator (it may be a leftover - "
                             "taskkill /F /IM emulator.exe), then relaunch")
            elif avd is None and DeviceManager._port_in_use(int(port)):
                problems.append(f"port {port} occupied but no console answers")
                fixes.append("a crashed emulator is holding the port - kill stray "
                             "emulator.exe/qemu-system processes, then relaunch")
        if device.get("serial") and device.get("pid"):
            serial_alive = adb.device_state(device["serial"]) == "device"
            pid_alive = manager._pid_alive(device["pid"])
            if pid_alive and not serial_alive:
                problems.append("process alive but adb can't see it")
                fixes.append("click Stop once (clears the stale state), then Launch")
        # A half-dead emulator (dead pid but ports still bound, or a ghost
        # adb entry) is what makes Launch fail right after Stop.
        if device.get("console_port") and DeviceManager._port_in_use(int(device["console_port"])):
            avd_here = probe_console(int(device["console_port"]))
            if avd_here is None:
                problems.append(f"port {device['console_port']} busy but no console answers")
                fixes.append("`python main.py stop <name> --force` (frees a zombie "
                             "emulator), or kill stray emulator.exe processes")
            elif avd_here != device.get("avd_name") and device.get("running"):
                problems.append(f"port held by AVD '{avd_here}'")
                fixes.append("`python main.py doctor` names the holder; stop that "
                             "emulator first")
        if device.get("runtime_stale"):
            problems.append("spoofing/hooks were lost on reboot")
            fixes.append("click ↻ Restore on the card (or `python main.py restore`)")
        elif device.get("needs_restore"):
            problems.append("saved spoofing/hooks are not applied")
            fixes.append("click ↻ Restore (or `python main.py restore`)")
        rows.append(_check(
            f"device_{name}", f"Device: {name}",
            not problems, "warn",
            "; ".join(problems) if problems else "consistent",
            " · ".join(fixes),
        ))
    if not rows:
        rows.append(_check("devices", "Saved devices", True, "info",
                           "none yet - create one with + New Device."))
    return rows


def orphaned_avds():
    """AVDs on disk that no saved device points at (leftovers from deletes).

    A delete used to remove only the device record when its ``avd_home`` was
    stale, which left ``C:\\Users\\<user>\\.android\\avd\\<avd>.avd`` behind:
    hundreds of MB each, invisible in the app, and still listed by
    ``emulator -list-avds``. Returns ``[(home, avd_name, size_bytes), ...]``.
    """
    manager = DeviceManager()
    try:
        known = {d.get("avd_name") for d in manager.list_devices()}
    except Exception:
        return []
    found = []
    for home in manager._avd_homes():
        try:
            inis = sorted(Path(home).glob("*.ini"))
        except OSError:
            continue
        for ini in inis:
            if ini.stem in known:
                continue
            size = 0
            avd_dir = Path(home) / f"{ini.stem}.avd"
            if avd_dir.exists():
                for item in avd_dir.rglob("*"):
                    try:
                        if item.is_file():
                            size += item.stat().st_size
                    except OSError:
                        pass
            found.append((Path(home), ini.stem, size))
    return found


def check_orphaned_avds():
    """Are there AVD folders nothing in the app knows about?"""
    orphans = orphaned_avds()
    if not orphans:
        return _check("orphaned_avds", "Orphaned AVDs", True, "info",
                      "none - every AVD on disk belongs to a saved device.")
    total = sum(size for _, _, size in orphans)
    names = ", ".join(f"{n} ({size / (1024 ** 2):.0f} MB)" if size else n
                      for _, n, size in orphans[:6])
    more = f" and {len(orphans) - 6} more" if len(orphans) > 6 else ""
    return _check(
        "orphaned_avds", "Orphaned AVDs", False, "warn",
        f"{len(orphans)} AVD(s) no saved device points at "
        f"({total / (1024 ** 3):.1f} GB): {names}{more}.",
        "Run `python main.py clean-avds` to remove them (or delete the folders "
        f"by hand from {orphans[0][0]}).",
    )


def check_emulator_ports():
    """Are the console ports usable, and what is squatting on them?

    A launch answered *"No free emulator console port available"* while
    nothing was running: the port probes called closed ports "in use", so
    there was no way to tell why. This check lists the ports that really have
    a listener, says who owns them and whether that owner is one of ours.
    """
    from core.device_manager import DeviceManager
    import config

    start = int(config.EMULATOR_PORT_START)
    try:
        listeners = DeviceManager._listener_ports()
    except Exception as exc:
        return _check(
            "emulator_ports", "Emulator ports", False, "warn",
            f"Could not read the listener table ({exc}).",
            "Ports are still probed one by one during launch.",
        )

    emulators = []
    for port in sorted(p for p in listeners if p >= 1024):
        owner = DeviceManager._port_owner_pid(port)
        if owner and DeviceManager._pid_looks_like_emulator(owner):
            emulators.append((port, owner))

    if emulators:
        return _check(
            "emulator_ports", "Emulator ports", True, "warn",
            "Leftover emulator listener(s): "
            + ", ".join(f"{p} (pid {pid})" for p, pid in emulators)
            + f". The next launch frees the ones it needs above {start}.",
            "Stop them with `python main.py stop <device>`; a launch also "
            "cleans up leftovers automatically.",
        )
    if listeners:
        return _check(
            "emulator_ports", "Emulator ports", True, "ok",
            f"No emulator is listening; {len(listeners)} other local listener(s) "
            f"do not conflict. Next console port: "
            f"{start if start not in listeners else start + 2}.",
        )
    return _check(
        "emulator_ports", "Emulator ports", True, "ok",
        f"Nothing is listening; a launch will take {start} / {start + 1} "
        "for the console and adb port.",
    )


def run_doctor():
    """Run every check. Returns (checks, worst_severity)."""
    checks = [
        check_python(),
        check_sdk_layout(),
        check_cmdline_duplicates(),
        check_platform_tools_age(),
        check_adb_server(),
        check_system_images(),
        check_settings_file(),
        check_frida_versions(),
        check_gpu_accel(),
        check_gpu_identity(),
        check_renderer_and_radios(),
        check_emulator_ports(),
        *check_devices(),
        check_orphaned_avds(),
    ]
    worst = "ok"
    for c in checks:
        if SEVERITY_ORDER[c["severity"]] > SEVERITY_ORDER[worst]:
            worst = c["severity"]
    return checks, worst
