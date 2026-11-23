"""
Hardware ID Spoofing - verified build/hardware/IMEI/MAC/Serial spoofing via ADB.

Spoofing pipeline (verified against ``getprop`` — never claims success unless
properties actually changed):

1. Ensure ``adb root`` cleanly via ``core.adb.root()`` (which waits for the
   ``adbd`` restart/offline window and confirms ``uid=0`` instead of racing
   against an offline daemon).
2. Primary path — ``resetprop-rs --stealth`` (``spoof/resetprop_manager.py``):
   mutates Android's ``/dev/__properties__`` shared memory directly in <1s
   without touching ``/system`` or doing full reboots, covering all 22 core
   ``ro.*`` props (including kernel-cmdline ``ro.serialno``,
   ``ro.boot.serialno``, ``ro.hardware``, ``ro.kernel.qemu``,
   ``ro.bootloader``, ``ro.baseband``) plus Android 10-15 partition-scoped
   properties. Restarts ``zygote`` (soft reload, ~6-10s) so Java
   ``android.os.Build.*`` reloads cleanly, then applies ``ro.kernel.qemu=0``
   / ``ro.boot.qemu=0`` after ``system_server`` finishes restarting.
3. Fallback path (when ``resetprop-rs`` is unavailable, e.g. offline without a
   cached binary): ``/data/local.prop`` + reboot -> ``avbctl
   disable-verification`` / ``adb disable-verity`` + reboot + ``adb remount``
   + multi-partition ``build.prop`` rewrite (Android 10-15 paths) + reboot.
4. Runtime non-``ro.*`` settings/props (``android_id``, ``device_name``,
   ``bluetooth_name``, MACs, baseband) applied AFTER any zygote reload or
   reboot so nothing is wiped.
"""
import os
import re
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

import config
from core import adb as core_adb
from spoof.resetprop_manager import ResetpropManager


# Property files that shape what Settings/Build report across Android 10-15.
# Files that don't exist on a given image are skipped.
_BUILD_PROP_FILES = (
    "/system/build.prop",
    "/system/system_ext/etc/build.prop",
    "/system_ext/etc/build.prop",
    "/system_ext/build.prop",
    "/system/product/etc/build.prop",
    "/product/etc/build.prop",
    "/product/build.prop",
    "/vendor/build.prop",
    "/vendor/default.prop",
    "/odm/etc/build.prop",
)

_PARTITION_NAMES = ("system", "system_ext", "product", "vendor", "odm")

# Props applied AFTER zygote restarts so zygote64/system_server OpenGL and HAL
# preloads finish cleanly against ranchu before qemu/hardware flags flip.
_POST_ZYGOTE_KEYS = ("ro.kernel.qemu", "ro.boot.qemu")

# Original ro.hardware values used by Android emulators whose vendor HAL shared
# libraries (e.g. /vendor/lib64/hw/gralloc.ranchu.so, vulkan.ranchu.so) fall
# back to ro.hardware when ro.hardware.<class> is not explicitly pinned.
_EMULATOR_HARDWARE_TAGS = (
    "ranchu",
    "goldfish",
    "cutf_cvm",
    "vsoc_x86_64",
    "vsoc_x86",
    "vsoc_arm64",
)


def parse_fingerprint(fp):
    """Parse an Android build fingerprint into its 8 standard components."""
    m = re.match(
        r"([^/]+)/([^/]+)/([^/:]+):([^/:]+)/([^/:]+)/([^/:]+):([^/:]+)/([\w.-]+)",
        fp or "",
    )
    if not m:
        return None
    brand, product, device, release, build_id, incremental, build_type, build_tags = m.groups()
    return {
        "brand": brand,
        "product": product,
        "device": device,
        "release": release,
        "build_id": build_id,
        "incremental": incremental,
        "build_type": build_type,
        "build_tags": build_tags,
    }


def build_identity_prop_maps(identity):
    """Return ``(core_props, extended_props)`` for *identity*.

    *core_props* is the set of canonical properties verified and reported
    in ``prop_report`` (22 identity props, plus the SIM carrier/country/numeric
    pair when the identity carries a Polish line). *extended_props* adds Android 10-15 partition-scoped
    and verified-boot properties so partition checks match ``ro.product.*``.
    """
    model = identity.get("model", "")
    brand = identity.get("brand", "")
    manufacturer = identity.get("manufacturer", "")
    device = identity.get("device", "")
    board = identity.get("board", "")
    fp = identity.get("fingerprint", "")
    serial = identity.get("serial", "")
    baseband = identity.get("baseband", "")
    bootloader = identity.get("bootloader", "")

    # CPU ABI list is NOT set at the native prop level: telling zygote the
    # system supports only arm64-v8a (pre- or post-zygote restart) causes it
    # to look for ARM native libraries on an x86_64 system, which crashes
    # zygote and bootloops the device. The emulator uses libndk_translation
    # to run ARM binaries on x86_64, and the ACTUAL ABI list must stay at
    # x86_64 for native library loading to work.
    # ABI spoofing is done EXCLUSIVELY at runtime via Frida hooks:
    #   - spec_hooks.js: Build.SUPPORTED_ABIS, Build.CPU_ABI, os.arch (Java)
    #   - native_hooks.js: __system_property_get("ro.product.cpu.abilist") in
    #     hooked app processes only (never system_server/zygote)
    # The Frida-side defaults are arm64-v8a/armeabi-v7a/armeabi.

    core = {
        "ro.product.model": model,
        "ro.product.brand": brand,
        "ro.product.manufacturer": manufacturer,
        "ro.product.device": device,
        "ro.product.board": board,
        "ro.build.fingerprint": fp,
        "ro.serialno": serial,
        "ro.boot.serialno": serial,
        "ro.kernel.qemu": "0",
        # ro.hardware kept at native ranchu (HAL loading); apps get Build.HARDWARE via Java hooks
        "ro.baseband": baseband or "unknown",
        "ro.bootloader": bootloader,
    }

    # Polish SIM: the carrier name and a real Polish MCC/MNC (260xx). Both are
    # verified in the report so "the line is Polish" is not just a claim, and
    # alpha/numeric always describe the same carrier. The line number itself is
    # answered by the Frida telephony hooks (no prop holds it).
    sim_carrier = identity.get("sim_carrier") or identity.get("operator") or ""
    # Accept "26006" (MCC+MNC) or "06" (MNC only) - never double-prefix.
    sim_numeric = str(identity.get("sim_operator_numeric") or "").strip()
    if sim_numeric and not sim_numeric.startswith("260"):
        sim_numeric = "260" + sim_numeric.rjust(2, "0")
    if len(sim_numeric) != 5:
        sim_numeric = ""
    if sim_carrier:
        core.update({
            "gsm.sim.operator.alpha": sim_carrier,
            "gsm.operator.alpha": sim_carrier,
            "gsm.sim.operator.iso-country": "pl",
            "gsm.operator.iso-country": "pl",
        })
    if sim_numeric.startswith("260"):
        core.update({
            "gsm.sim.operator.numeric": sim_numeric,
            "gsm.operator.numeric": sim_numeric,
        })

    # SoC / chipset informational props - Settings "Processor" reads these.
    # NOTE: We do NOT override ro.hardware here: it is already set to `board`
    # above (from the device profile), and the HAL pinning + post-zygote
    # mechanism protects gralloc/EGL/Vulkan/Audio/Camera HAL loading from
    # breakage. Changing ro.hardware away from what the profile's board
    # specifies can cause HAL .so load failures and bootloops.
    # NOTE: ro.board.platform, ro.hardware.chipname, ro.soc.model are
    # informational only and do not affect library loading.
    soc = identity.get("soc", "")
    if soc:
        import re as _re
        _soc_lower = soc.lower()
        # Use the board value from profile (e.g. "zuma", "kalama", "s5e9945")
        # as ro.board.platform — it's already the correct SoC codename.
        platform = board or "qcom"

        if "exynos" in _soc_lower:
            soc_mfr = "Samsung"
        elif "tensor" in _soc_lower:
            soc_mfr = "Google"
        elif "dimensity" in _soc_lower or "mediatek" in _soc_lower:
            soc_mfr = "MediaTek"
        else:
            soc_mfr = "Qualcomm"

        core.update({
            "ro.board.platform": platform,
            "ro.hardware.chipname": soc,
            "ro.soc.model": soc,
            "ro.soc.manufacturer": soc_mfr,
        })

    parsed = parse_fingerprint(fp)
    product_name = identity.get("product", "")
    build_id = ""
    incremental = ""
    # The release the device actually runs wins: reporting a fingerprint's
    # number while the OS is a different version is a contradiction an app sees
    # from two places (Build.VERSION.RELEASE vs Build.FINGERPRINT).
    release = str(identity.get("android_version", "") or "")
    build_type = "user"
    build_tags = "release-keys"

    if parsed:
        product_name = parsed["product"]
        fp_device = parsed["device"]
        release = release or parsed["release"]
        build_id = parsed["build_id"]
        incremental = parsed["incremental"]
        build_type = parsed["build_type"]
        build_tags = parsed["build_tags"]
        core.update({
            "ro.product.name": product_name,
            "ro.build.product": fp_device,
            "ro.build.id": build_id,
            "ro.build.display.id": build_id,
            "ro.build.version.incremental": incremental,
            "ro.build.version.release": release,
            "ro.build.type": build_type,
            "ro.build.tags": build_tags,
            "ro.build.flavor": f"{fp_device}-{build_type}",
            "ro.build.description": (
                f"{fp_device}-{build_type} {release} {build_id} {incremental} {build_tags}"
            ),
        })

    core = {k: str(v) for k, v in core.items() if v is not None and str(v) != ""}

    extended = dict(core)
    # Anti-detect extras (partition consistency + verified boot state)
    extras = {
        "ro.boot.qemu": "0",
        # ro.boot.hardware kept at native value (HAL loading)
        "ro.build.characteristics": "default",
        "ro.boot.verifiedbootstate": "green",
        "ro.boot.flash.locked": "1",
        "ro.boot.veritymode": "enforcing",
        "ro.boot.vbmeta.device_state": "locked",
        "ro.bootimage.build.fingerprint": fp,
    }
    for k, v in extras.items():
        if v:
            extended[k] = str(v)

    for part in _PARTITION_NAMES:
        part_map = {
            f"ro.product.{part}.model": model,
            f"ro.product.{part}.brand": brand,
            f"ro.product.{part}.manufacturer": manufacturer,
            f"ro.product.{part}.device": device,
            f"ro.product.{part}.name": product_name or device,
            f"ro.{part}.build.fingerprint": fp,
            f"ro.{part}.build.id": build_id,
            f"ro.{part}.build.version.incremental": incremental,
            f"ro.{part}.build.version.release": release,
            f"ro.{part}.build.type": build_type,
            f"ro.{part}.build.tags": build_tags,
        }
        for k, v in part_map.items():
            if v:
                extended[k] = str(v)

    return core, extended


class HardwareSpoofer:
    """Spoof hardware identifiers on a running emulator."""

    def __init__(self, device_serial="emulator-5554"):
        self.device_serial = device_serial
        self.adb = config.ADB_PATH
        self._root_done = False
        # Set True whenever a zygote soft-restart or full reboot occurred
        # during spoofing (so callers know live Frida sessions were detached).
        self.reloaded_runtime = False
        # Filled by spoof_build_props: {props, method, verified, failed, ...}
        self.prop_report = None
        # Filled by spoof_gpu: the coherent GPU identity applied to this device.
        self.gpu_report = None

    # ------------------------------------------------------------------ adb IO
    def _adb_full(self, command, timeout=30, retry=True):
        """Execute an ADB command returning ``(ok, stdout, stderr)``.

        Automatically attempts one ``core.adb.recover`` retry if the daemon
        reports ``offline`` / ``not found`` / closed transport.
        """
        cmd = [self.adb, "-s", self.device_serial] + command
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "", f"adb timed out after {timeout}s"
        except Exception as exc:
            print(f"ADB error: {exc}")
            return False, "", str(exc)

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
                recovered, _ = core_adb.recover(self.device_serial, timeout=20)
                if recovered:
                    return self._adb_full(command, timeout=timeout, retry=False)
        return result.returncode == 0, out, err

    def _adb(self, command, timeout=30):
        """Execute an ADB command and return stdout (or None on transport error)."""
        ok, out, err = self._adb_full(command, timeout=timeout)
        if not ok and not out and err:
            return None
        return out

    def _adb_root(self):
        """Ensure adbd runs as root and WAIT for it to come back online.

        Previously this called raw ``adb root`` and returned immediately
        while ``adbd`` was mid-restart, causing every subsequent ``getprop`` /
        ``disable-verity`` / ``push`` / ``remount`` command to fail with
        ``adb: device offline``. Delegating to ``core.adb.root()`` checks
        ``id`` first (no-op when already uid=0) and waits out the offline
        restart window cleanly.
        """
        if self._root_done:
            # Quick liveness check in case adbd dropped or rebooted
            ok, out, _ = self._adb_full(["shell", "id"], timeout=10)
            if ok and "uid=0" in out:
                return True
            self._root_done = False

        rooted, detail = core_adb.root(self.device_serial, timeout=60)
        if rooted:
            self._root_done = True
            return True
        # Even if root was refused (e.g. production/Play Store image or stub),
        # ensure adbd itself is back online before running remaining commands.
        if core_adb.get_state(self.device_serial) != "device":
            core_adb.wait_until_ready(self.device_serial, timeout=20)
        return False

    def _shell_root(self, command, timeout=30):
        """Run a shell command as root, preferring ``adb root`` over ``su``."""
        self._adb_root()
        ok, out, err = self._adb_full(["shell", command], timeout=timeout)
        if not ok and "permission denied" in f"{out} {err}".lower():
            _, out, _ = self._adb_full(["shell", "su", "-c", command], timeout=timeout)
        return out

    def _getprop(self, key):
        ok, out, _ = self._adb_full(["shell", "getprop", key], timeout=15)
        return out if ok else ""

    def _batch_getprop(self, keys):
        """Read *keys* via ``getprop``; uses individual calls for stub compatibility."""
        return {k: self._getprop(k) for k in keys}

    def _pin_emulator_hal_props(self, rp_mgr):
        """Pin ``ro.hardware.<class>`` for emulator HAL ``.so`` modules before mutating ``ro.hardware``.

        Android's ``libhardware`` (``hw_get_module_by_class`` in ``hardware.c``)
        looks up ``ro.hardware.<class>`` first and falls back to ``ro.hardware``
        and ``ro.product.board``. If ``ro.hardware`` changes from ``ranchu`` to
        a spoofed SoC board name (e.g. ``s5e9945`` / ``pineapple``) without
        explicit ``ro.hardware.<class>=ranchu`` entries, any HAL module lacking
        an explicit property fails to load (``-ENOENT``) and crashes Settings
        or system services.
        """
        orig_hw = (self._getprop("ro.hardware") or "").strip()
        if not orig_hw:
            return {}
        low = orig_hw.lower()
        if low not in _EMULATOR_HARDWARE_TAGS and not low.startswith("emulator"):
            return {}
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", orig_hw):
            return {}

        # Single Toybox-safe scan (no awk) across standard HAL directories
        scan_cmd = (
            f"HW={shlex.quote(orig_hw)}; "
            "for d in /vendor/lib64/hw /vendor/lib/hw /system/lib64/hw /system/lib/hw /odm/lib64/hw; do "
            '[ -d "$d" ] || continue; '
            'for f in "$d"/*."$HW".so "$d"/*.goldfish.so; do '
            '[ -f "$f" ] && echo "$f"; '
            "done; done; "
            "for e in /vendor/lib64/egl/libEGL_emulation.so /vendor/lib/egl/libEGL_emulation.so; do "
            '[ -f "$e" ] && echo "EGL:emulation" && break; '
            "done"
        )
        ok, out, _ = self._adb_full(["shell", scan_cmd], timeout=15)
        if not ok or not out:
            return {}

        candidates = {}
        suffixes = [f".{orig_hw}.so"]
        if orig_hw != "goldfish":
            suffixes.append(".goldfish.so")

        for raw_line in out.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line == "EGL:emulation":
                candidates.setdefault("ro.hardware.egl", "emulation")
                continue
            base = line.rsplit("/", 1)[-1]
            for suf in suffixes:
                if base.endswith(suf) and len(base) > len(suf):
                    hal_class = base[: -len(suf)]
                    variant = suf[1:-3]  # strip leading '.' and trailing '.so'
                    if re.fullmatch(r"[A-Za-z0-9_.-]+", hal_class):
                        candidates.setdefault(f"ro.hardware.{hal_class}", variant)
                    break

        if not candidates:
            return {}

        # Only pin keys that are not already explicitly set by init.ranchu.rc
        hal_pins = {}
        for key, val in candidates.items():
            if not (self._getprop(key) or "").strip():
                hal_pins[key] = val

        if hal_pins:
            rp_mgr.apply_props(hal_pins)
            print(
                f"[AntiDetect] Pinned {len(hal_pins)} emulator HAL prop(s) "
                f"before spoofing ro.hardware ({', '.join(sorted(hal_pins)[:6])}"
                f"{'...' if len(hal_pins) > 6 else ''})"
            )
        return hal_pins

    # ------------------------------------------------------------ build props
    def spoof_build_props(self, identity):
        """Spoof ``ro.*`` and build properties and verify every one via ``getprop``."""
        props, extended = build_identity_prop_maps(identity)
        report = {"props": props, "method": None, "verified": {}, "failed": []}

        if not props:
            report["method"] = "none"
            self.prop_report = report
            return

        # Step 0: Wait out `adb root` BEFORE reading/writing anything so we
        # never race adbd's offline restart window.
        has_root = self._adb_root()

        # Step 1: Check current state & try plain runtime setprop (fast no-op
        # when already spoofed, and sets any non-ro.* keys immediately).
        for key, value in props.items():
            if not key.startswith("ro."):
                self._adb(["shell", "setprop", key, value])

        current = self._batch_getprop(props.keys())
        mismatched = {k: v for k, v in props.items() if current.get(k) != v}
        if not mismatched:
            report["method"] = "runtime setprop"
            report["verified"] = dict(props)
            self.prop_report = report
            return

        # Step 2 (PRIMARY): Direct /dev/__properties__ spoof via resetprop-rs
        # + soft zygote restart (zero full reboots, works on Android 10-15
        # including kernel-cmdline props like ro.serialno/ro.hardware/ro.kernel.qemu).
        resetprop_detail = None
        if has_root:
            pre_zygote = {
                k: v for k, v in extended.items() if k not in _POST_ZYGOTE_KEYS
            }
            post_zygote = {
                k: v for k, v in extended.items() if k in _POST_ZYGOTE_KEYS
            }

            rp_mgr = ResetpropManager(self.device_serial)
            # Pin emulator HAL ro.hardware.<class> overrides BEFORE mutating
            # ro.product.board / ro.hardware so libhardware/EGL/Vulkan keep
            # resolving ranchu/emulation .so drivers cleanly.
            self._pin_emulator_hal_props(rp_mgr)
            rp_ok, rp_msg, rp_kind = rp_mgr.apply_props(pre_zygote)
            resetprop_detail = rp_msg

            if rp_ok:
                # Check if resetprop actually changed any mismatched pre-zygote props
                after_pre = self._batch_getprop(
                    [k for k in mismatched if k not in _POST_ZYGOTE_KEYS]
                )
                changed_any = any(
                    after_pre.get(k) == props[k] and current.get(k) != props[k]
                    for k in after_pre
                )

                if changed_any:
                    # Soft-restart zygote so Java Build.* and Settings reload the
                    # new properties cleanly (without rebooting the kernel).
                    self._restart_zygote_and_wait()
                    # Re-apply pre-zygote props if any service reset them, then
                    # flip ro.kernel.qemu / ro.boot.qemu AFTER system_server is up.
                    if post_zygote:
                        rp_mgr.apply_props(post_zygote)
                elif post_zygote and any(k in mismatched for k in post_zygote):
                    rp_mgr.apply_props(post_zygote)

                still = {k: v for k, v in props.items() if self._getprop(k) != v}
                if still and len(still) < len(mismatched):
                    # Retry just the remaining keys once in case a service reset one
                    # during zygote restart; NEVER reboot and wipe working resetprop changes.
                    rp_mgr.apply_props(still)
                    still = {k: v for k, v in props.items() if self._getprop(k) != v}

                if not still or len(still) < len(mismatched):
                    final_vals = self._batch_getprop(props.keys())
                    verified = {k: v for k, v in props.items() if final_vals.get(k) == v}
                    failed = [k for k in props if k not in verified]
                    report["method"] = (
                        f"{rp_kind or 'resetprop'} + zygote reload"
                        if changed_any else (rp_kind or "resetprop")
                    )
                    report["verified"] = verified
                    report["failed"] = failed
                    report["resetprop_detail"] = resetprop_detail
                    self.prop_report = report
                    if failed:
                        print(
                            "[AntiDetect] WARNING: resetprop verified "
                            f"{len(verified)}/{len(props)} props on {self.device_serial}; "
                            f"unchanged: {sorted(failed)}."
                        )
                    return
                # resetprop changed 0 props — fall back to local.prop / build.prop
                mismatched = still

        # Step 3 (FALLBACK): /data/local.prop + reboot -> disable-verity + remount
        # + multi-partition build.prop rewrite + reboot.
        # Runs when resetprop-rs is unavailable (e.g. offline machine without
        # cached binary, or fake_sdk tests exercising the build.prop path).
        still = dict(mismatched)
        method = "runtime setprop"

        self._install_local_prop(still)
        self._reboot_and_ready()
        still = {k: v for k, v in still.items() if self._getprop(k) != v}
        if still:
            writable_ok = self._rewrite_build_props(still, extended_props=extended)
            self._reboot_and_ready()
            still = {k: v for k, v in still.items() if self._getprop(k) != v}
            if still:
                method = "runtime setprop + build.prop (ro.* unchanged)"
                if not writable_ok:
                    report["hint"] = (
                        "System partitions stayed read-only. Stop and relaunch the "
                        "device (which now passes -writable-system) or ensure internet "
                        "access for resetprop-rs auto-download."
                    )
            else:
                method = "runtime setprop + disable-verity + build.prop reboot"
        else:
            method = "runtime setprop + /data/local.prop + reboot"

        final_vals = self._batch_getprop(props.keys())
        verified = {k: v for k, v in props.items() if final_vals.get(k) == v}
        failed = [k for k in props if k not in verified]

        report["verified"] = verified
        report["failed"] = failed
        report["method"] = method
        if resetprop_detail:
            report["resetprop_detail"] = resetprop_detail
        self.prop_report = report

        if failed:
            hint = f" ({report['hint']})" if report.get("hint") else ""
            print(
                "[AntiDetect] WARNING: could not change props "
                f"{sorted(failed)} on {self.device_serial}{hint}. "
                "Runtime Frida hooks still cover Build.* for hooked apps."
            )

    def _restart_zygote_and_wait(self, timeout=60):
        """Soft-restart zygote so ``android.os.Build.*`` reloads from memory.

        Unlike a full ``adb reboot``, ``ctl.restart zygote`` preserves
        ``/dev/__properties__`` (keeping all ``resetprop`` changes intact),
        takes ~6-10s instead of ~45-60s, and does not tear down ``adbd``.

        Waits for BOTH a new ``zygote`` PID AND a new ``system_server`` PID
        (plus ``activity`` and ``settings`` services registered) so follow-up
        ``settings put`` calls never race a dying ``system_server``.
        """
        _, z64_raw, _ = self._adb_full(["shell", "pidof zygote64 2>/dev/null"], timeout=10)
        _, z32_raw, _ = self._adb_full(["shell", "pidof zygote 2>/dev/null"], timeout=10)
        z64_pids = {p for p in (z64_raw or "").split() if p.isdigit()}
        z32_pids = {p for p in (z32_raw or "").split() if p.isdigit()}
        old_zygote_pids = z64_pids | z32_pids

        _, sec_svc, _ = self._adb_full(
            ["shell", "getprop init.svc.zygote_secondary"], timeout=10
        )
        has_secondary = bool(
            (z32_pids and z64_pids and z32_pids != z64_pids)
            or (sec_svc or "").strip() == "running"
        )

        _, sys_raw, _ = self._adb_full(["shell", "pidof system_server 2>/dev/null"], timeout=10)
        old_sys_pids = {p for p in (sys_raw or "").split() if p.isdigit()}

        self._shell_root("setprop ctl.restart zygote")
        if has_secondary:
            self._shell_root("setprop ctl.restart zygote_secondary")
        self.reloaded_runtime = True

        # Detach any live Frida sessions on this serial because their target
        # app processes are restarting with zygote.
        try:
            from core.hook_manager import detach_device_sessions
            detach_device_sessions(self.device_serial)
        except Exception:
            pass

        deadline = time.time() + timeout
        zygote_changed = not old_zygote_pids
        sys_changed = not old_sys_pids
        while time.time() < deadline:
            if not zygote_changed:
                _, cur_z_raw, _ = self._adb_full(
                    ["shell", "pidof zygote64 zygote 2>/dev/null || pidof zygote64 2>/dev/null"],
                    timeout=10,
                )
                cur_z = {p for p in (cur_z_raw or "").split() if p.isdigit()}
                if cur_z and cur_z != old_zygote_pids:
                    zygote_changed = True

            if zygote_changed and not sys_changed:
                _, cur_s_raw, _ = self._adb_full(
                    ["shell", "pidof system_server 2>/dev/null"], timeout=10
                )
                cur_s = {p for p in (cur_s_raw or "").split() if p.isdigit()}
                if cur_s and cur_s != old_sys_pids:
                    sys_changed = True

            if zygote_changed and sys_changed:
                ok_act, act_out, _ = self._adb_full(
                    ["shell", "service check activity 2>/dev/null"], timeout=10
                )
                ok_set, set_out, _ = self._adb_full(
                    ["shell", "service check settings 2>/dev/null"], timeout=10
                )
                act_ok = ok_act and (
                    "found" in (act_out or "").lower()
                    or (not act_out and not old_zygote_pids)
                )
                set_ok = ok_set and (
                    "found" in (set_out or "").lower()
                    or (not set_out and not old_sys_pids)
                )
                if act_ok and set_ok:
                    # Brief settle beat + stop Settings and clear ONLY its stale
                    # pre-fingerprint cache (preserves all user data/preferences)
                    # so com.android.settings boots fresh with the new Build.*.
                    time.sleep(0.4)
                    self._adb_full(["shell", "am", "force-stop", "com.android.settings"], timeout=10)
                    self._shell_root(
                        "rm -rf /data/user/0/com.android.settings/cache "
                        "/data/user_de/0/com.android.settings/cache 2>/dev/null"
                    )
                    return True

            time.sleep(1.0)

        return False

    def _install_local_prop(self, props):
        """Merge props into ``/data/local.prop`` (root via ``adb root``)."""
        self._adb_root()
        existing = self._shell_root("cat /data/local.prop 2>/dev/null") or ""
        merged = {}
        for line in existing.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                merged[k.strip()] = v.strip()
        merged.update(props)
        content = "\n".join(f"{k}={v}" for k, v in sorted(merged.items())) + "\n"

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n",
                prefix="local-prop-", suffix=".txt", delete=False,
            ) as fh:
                fh.write(content)
                tmp_path = Path(fh.name)
            self._adb(["push", str(tmp_path), "/data/local.prop"])
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
        self._shell_root("chmod 644 /data/local.prop; chown root:root /data/local.prop")

    def _read_boot_id(self):
        ok, out, _ = self._adb_full(
            ["shell", "cat /proc/sys/kernel/random/boot_id 2>/dev/null"], timeout=10
        )
        val = (out or "").strip()
        # Real Linux boot_id is a 36-char UUID; ignore error strings
        if ok and len(val) >= 16 and " " not in val and "/" not in val:
            return val
        return ""

    def _reboot_and_ready(self, timeout=300):
        """Reboot and wait until the NEW boot is fully completed + rooted.

        Uses ``/proc/sys/kernel/random/boot_id`` (when available on a real
        kernel) plus the ``adbd`` offline transition so we never race ahead
        before the reboot actually starts — fixing the 8-second race on slow
        disks while staying fast on stub SDKs.
        """
        pre_boot_id = self._read_boot_id()
        self._adb(["reboot"])
        self.reloaded_runtime = True
        try:
            from core.hook_manager import detach_device_sessions
            detach_device_sessions(self.device_serial)
        except Exception:
            pass

        # Wait up to 25s for adbd to drop OR boot_id to change (instant on fast/stub devices)
        drop_deadline = time.time() + (25 if pre_boot_id else 8)
        dropped = False
        while time.time() < drop_deadline:
            state = core_adb.get_state(self.device_serial)
            if state != "device":
                dropped = True
                break
            if pre_boot_id:
                cur_boot_id = self._read_boot_id()
                if cur_boot_id and cur_boot_id != pre_boot_id:
                    dropped = True
                    break
            time.sleep(0.5)

        if dropped:
            time.sleep(1.0)
        else:
            time.sleep(1.0)

        try:
            core_adb.wait_until_ready(self.device_serial, timeout=timeout)
        except Exception:
            pass

        # If we recorded a real boot_id and the device still has the old one,
        # wait briefly for the reboot to finish cycling.
        if pre_boot_id:
            wait_end = time.time() + 45
            while time.time() < wait_end:
                cur_boot_id = self._read_boot_id()
                if not cur_boot_id or cur_boot_id != pre_boot_id:
                    break
                time.sleep(1.0)
            core_adb.wait_until_ready(self.device_serial, timeout=timeout)

        self._root_done = False
        self._adb_root()

    def _rewrite_build_props(self, props, extended_props=None):
        """disable-verity + remount + rewrite Android 10-15 build.prop files in place.

        Returns ``True`` if at least one ``build.prop`` file was verifiably
        rewritten, ``False`` if all partitions remained read-only.
        """
        self._adb_root()

        # 1) Disable AVB + dm-verity (covers Android 10-15 google_apis images;
        #    avbctl handles Android 14+ vbmeta verification alongside adb disable-verity).
        self._adb_full(["shell", "avbctl disable-verification 2>/dev/null"], timeout=30)
        self._adb_full(["disable-verity"], timeout=30)
        self._reboot_and_ready()

        # 2) Remount partitions writable (requires -writable-system on real AVDs).
        self._adb_full(["remount"], timeout=60)
        self._shell_root(
            "mount -o remount,rw / 2>/dev/null; "
            "mount -o remount,rw /system 2>/dev/null; "
            "mount -o remount,rw /system_ext 2>/dev/null; "
            "mount -o remount,rw /product 2>/dev/null; "
            "mount -o remount,rw /vendor 2>/dev/null; "
            "mount -o remount,rw /odm 2>/dev/null"
        )

        # Ensure backup directory exists (previously omitted -> backup silently failed)
        self._shell_root("mkdir -p /data/local/antidetect-buildprop-backup")

        # 3) Rewrite every build.prop file that exists on this image.
        #    Existing partition-scoped keys (ro.product.<part>.*, ro.<part>.build.*)
        #    are updated in place; canonical props are ensured in /system/build.prop.
        all_updates = dict(extended_props or props)
        all_updates.update(props)

        wrote_any = False
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n",
                prefix="adbp-", suffix=".txt", delete=False,
            ) as fh:
                tmp_path = Path(fh.name)

            seen_realpaths = set()
            for path in _BUILD_PROP_FILES:
                original = self._shell_root(f"cat {path} 2>/dev/null") or ""
                if not original.strip():
                    continue  # file does not exist on this image

                # Deduplicate symlinked paths (e.g. /product -> /system/product)
                _, rp_out, _ = self._adb_full(
                    ["shell", f"realpath {path} 2>/dev/null || echo {path}"], timeout=10
                )
                canonical_path = (rp_out or path).splitlines()[0].strip() or path
                if canonical_path in seen_realpaths:
                    continue
                seen_realpaths.add(canonical_path)

                backup = (
                    f"/data/local/antidetect-buildprop-backup/{path.lstrip('/').replace('/', '_')}"
                )
                self._shell_root(f"test -f {backup} || cp {path} {backup} 2>/dev/null")

                lines = []
                # Update any matching key already present in this file;
                # append missing core props only to /system/build.prop (or first file).
                pending_core = dict(props) if path == "/system/build.prop" else {}
                changed_file = False

                for line in original.splitlines():
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#") and "=" in stripped:
                        key = stripped.split("=", 1)[0].strip()
                        if key in all_updates:
                            new_val = all_updates[key]
                            lines.append(f"{key}={new_val}")
                            pending_core.pop(key, None)
                            changed_file = True
                            continue
                    lines.append(line)

                for key, value in pending_core.items():
                    lines.append(f"{key}={value}")
                    changed_file = True

                if not changed_file:
                    continue

                content = "\n".join(lines) + "\n"
                with open(tmp_path, "w", encoding="utf-8", newline="\n") as out_fh:
                    out_fh.write(content)
                self._adb(["push", str(tmp_path), "/data/local/tmp/.adbp"])

                # Prefer in-place redirection (`cat >`) to preserve the target
                # inode's permissions and SELinux file context; fall back to `cp`.
                self._shell_root(
                    f"cat /data/local/tmp/.adbp > {path} 2>/dev/null "
                    f"|| (cp /data/local/tmp/.adbp {path} && chmod 644 {path})"
                )

                # Verify the write actually landed (read-only fs -> log clearly).
                rewritten = self._shell_root(f"cat {path} 2>/dev/null") or ""
                if any(f"{k}={v}" in rewritten for k, v in all_updates.items()):
                    wrote_any = True
                else:
                    print(f"[AntiDetect] {path} is read-only even after remount")
        finally:
            self._shell_root("rm -f /data/local/tmp/.adbp")
            if tmp_path is not None:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

        return wrote_any

    # ------------------------------------------------------------ other IDs
    def spoof_imei(self, imei):
        """IMEI spoofing is handled via Frida ``TelephonyManager`` hooks.

        Historically this called ``service call iphonesubinfo 1 s16 <imei>``
        (which is actually the ``getDeviceId(callingPackage)`` getter, not a
        setter) and wrote ``imei[:6]`` (an IMEI TAC, not a valid MCC/MNC) into
        ``gsm.sim.operator.numeric``, corrupting CarrierConfig / Settings SIM
        status. Both harmful calls have been removed.
        """
        return

    def spoof_serial(self, serial):
        """Best-effort runtime serial spoof (verified path lives in spoof_build_props)."""
        if not serial:
            return
        self._adb(["shell", "setprop", "ro.serialno", serial])
        self._adb(["shell", "setprop", "ro.boot.serialno", serial])

    def spoof_android_id(self, android_id):
        """Spoof Settings.Secure.ANDROID_ID."""
        if not android_id:
            return
        self._adb(["shell", f"settings put secure android_id {shlex.quote(str(android_id))}"])

    def spoof_device_name(self, name):
        """Settings -> About 'Device name' row + Bluetooth name (shell-quoted for spaces)."""
        if not name:
            return
        quoted = shlex.quote(str(name))
        self._adb(["shell", f"settings put global device_name {quoted}"])
        self._adb(["shell", f"settings put secure bluetooth_name {quoted}"])

    def spoof_sim_identity(self, identity):
        """Polish +48 line + the carrier whose prefix matches the number.

        Only props that the telephony stack actually reads are written, and the
        number/carrier pair comes from profiles/phone_numbers.py so the SIM
        status screen cannot show "Play" for a number issued by Plus.

        Deliberately NOT written: ``gsm.sim.operator.numeric`` with a bogus
        value (it used to be set from the IMEI TAC, which corrupted
        CarrierConfig), and anything on the SIM itself - an AVD has no SIM, so
        only the "operator" props the framework caches are touched.
        """
        if not isinstance(identity, dict):
            return
        number = identity.get("phone_number")
        carrier = identity.get("sim_carrier") or identity.get("operator")
        numeric = identity.get("sim_operator_numeric")
        if carrier:
            quoted = shlex.quote(str(carrier))
            for key in ("gsm.sim.operator.alpha", "gsm.operator.alpha",
                        "gsm.sim.operator.iso-country", "gsm.operator.iso-country"):
                value = "pl" if "iso-country" in key else quoted
                self._adb(["shell", f"setprop {key} {value}"])
        if numeric and len(str(numeric)) in (5, 6):
            numeric = str(numeric)
            full = numeric if len(numeric) == 6 else "260" + numeric
            # alpha and numeric must describe the same carrier, and numeric must
            # be a real MCC/MNC for Poland (260) or CarrierConfig breaks.
            if full.startswith("260"):
                self._adb(["shell", f"setprop gsm.sim.operator.numeric {full}"])
                self._adb(["shell", f"setprop gsm.operator.numeric {full}"])
        # The line number itself is not a prop: the framework reads it from the
        # subscription DB, so spec_hooks.js answers getLine1Number / getMsisdn
        # with the same +48 number (kept in sync by construction).

    def spoof_mac_address(self, mac_address):
        """Spoof WiFi MAC address (requires root; best-effort)."""
        if not mac_address:
            return
        for cmd in (
            "ip link set wlan0 down",
            f"ip link set wlan0 address {mac_address}",
            "ip link set wlan0 up",
        ):
            self._shell_root(cmd)

    def hide_emulator_files(self):
        """Hide common emulator indicator files (best-effort, needs root).

        Deliberately NOT renamed: ``/dev/qemu_pipe`` and
        ``/dev/socket/qemud``. The emulator's camera HAL (virtual scene /
        webcam pass-through), sensor stack and GPS all open the qemu pipe
        lazily on first use - renaming the node breaks exactly those
        features while their Java-side presence checks are already spoofed
        by the ``File.exists()`` hook in frida_toolkit/scripts/anti_detect.js.
        """
        files_to_hide = [
            "/system/bin/qemu-props",
            "/system/lib/libc_malloc_debug_qemu.so",
        ]
        for file_path in files_to_hide:
            self._shell_root(f"mv {file_path} {file_path}.bak 2>/dev/null")

    def spoof_cpu_info(self):
        """Placeholder — /proc/cpuinfo spoofing is handled by native Frida hooks."""
        pass

    def get_current_props(self):
        """Get current device properties for debugging."""
        important_props = [
            "ro.product.model",
            "ro.product.brand",
            "ro.product.manufacturer",
            "ro.build.fingerprint",
            "ro.serialno",
            "ro.kernel.qemu",
            "ro.hardware",
        ]
        return {prop: self._getprop(prop) for prop in important_props}

    def spoof_bluetooth_mac(self, mac_address):
        """Spoof Bluetooth MAC address (best-effort; never corrupts binary NV files)."""
        if not mac_address:
            return
        self._adb(["shell", f"settings put secure bluetooth_address {shlex.quote(str(mac_address))}"])

    def spoof_baseband(self, baseband_version):
        """Spoof baseband/modem version."""
        if not baseband_version:
            return
        self._adb(["shell", "setprop", "gsm.version.baseband", baseband_version])
        self._adb(["shell", "setprop", "ro.baseband", baseband_version])

    def spoof_bootloader(self, bootloader_version):
        """Spoof bootloader version."""
        if not bootloader_version:
            return
        self._adb(["shell", "setprop", "ro.bootloader", bootloader_version])

    def spoof_all(self, identity):
        """Apply all hardware spoofing and return the verified prop report.

        ``spoof_build_props`` runs FIRST so any zygote reload or reboot
        completes before runtime settings/props are written.
        """
        self.spoof_build_props(identity)
        self.spoof_imei(identity.get("imei"))
        self.spoof_serial(identity.get("serial"))
        self.spoof_android_id(identity.get("android_id"))
        self.spoof_device_name(identity.get("model"))
        self.spoof_mac_address(identity.get("wifi_mac"))
        self.spoof_bluetooth_mac(identity.get("bluetooth_mac"))
        self.spoof_baseband(identity.get("baseband"))
        self.spoof_bootloader(identity.get("bootloader"))
        # Polish +48 line / matching carrier (profiles/phone_numbers.py).
        self.spoof_sim_identity(identity)
        self.spoof_gpu(identity)
        self.hide_emulator_files()
        return self.prop_report

    # ---------------------------------------------------------------- GPU
    def spoof_gpu(self, identity):
        """Apply GPU spoofing and record it in the report.

        Only cosmetic props are written (``debug.hwui.renderer``,
        ``debug.egl.hw``, ``debug.sf.hw``). Vendor/renderer/version/extensions/
        limits cannot be spoofed with properties - Android's loader resolves
        ``libEGL_<ro.hardware.egl>.so`` from them, so writing "adreno" there
        crashes every app's RenderThread. Those are spoofed by the Frida
        GLES20/EGL14/native-glGetString hooks instead, which read the same
        identity (see ``spoof/gpu_spoof.py``).

        Returns the GPU identity so callers can show/report it.
        """
        from spoof.gpu_spoof import build_gpu_identity

        gpu = build_gpu_identity(identity)
        self.gpu_report = gpu
        for key, value in (("debug.hwui.renderer", "skiagl"),
                           ("debug.egl.hw", "1"),
                           ("debug.sf.hw", "1")):
            self._adb(["shell", f"setprop {key} {value}"])

        if self.prop_report is not None:
            self.prop_report["gpu"] = {
                "renderer": gpu.get("gl_renderer"),
                "vendor": gpu.get("gl_vendor"),
                "egl_vendor": gpu.get("egl_vendor"),
                "version": gpu.get("gl_version"),
                "limits": gpu.get("gl_limits"),
                "method": "frida GLES/EGL hooks + cosmetic props",
            }
        return gpu
