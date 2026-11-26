"""Diagnose - and repair - a crashing Android Settings app.

The Settings app (``com.android.settings``) is the app users judge the whole
spoof by: if it dies on launch, the spoof is "broken" no matter how many props
verified. It is also uniquely fragile, because it is a system app that reads
almost every subsystem at start-up (telephony, storage, display, HAL-wrapped
sensors, verified-boot state).

Rather than hoping a prop change fixed it, this module asks the emulator what
actually happened:

1. launch Settings and see whether it stays up (``pidof`` before/after);
2. when it dies, read ``logcat -b crash`` and classify the failure into one of
   the known causes, each of which has a real repair;
3. apply the repair, relaunch Settings, and report honestly whether it now
   survives.

The classification matters as much as the repair: "Settings keeps stopping"
has three completely different causes on an AVD, and the wrong repair wastes a
user's time:

``frida_injection``
    A Frida session/gate is attached to Settings (or spawn gating is holding it
    at exec). Injected ART + libc interceptors SIGSEGV in system apps. Repair:
    detach the sessions, drop spawn gating, restart frida-server, relaunch.
``guard_props``
    A load-bearing ``ro.*`` prop holds a phone value (``ro.hardware=zuma``,
    ``ro.kernel.qemu=0``, ``ro.product.device=husky``). Repair: restore native
    values (see :mod:`spoof.prop_policy`) and bounce zygote.
``settings_data``
    Corrupt or inconsistent Settings app data/user state after many spoofs.
    Repair: clear only the app's cache and re-enable its component - never the
    user's other app data.
``unknown``
    The crash did not match a known signature; the raw log is surfaced so the
    user (or the next bug report) can see the actual stack instead of a guess.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import time

import config
from spoof.prop_policy import (
    CHARACTERISTICS_NATIVE,
    NEVER_SET,
    is_never_set,
)

SETTINGS_PACKAGE = "com.android.settings"
SETTINGS_ACTIVITY = f"{SETTINGS_PACKAGE}/.Settings"

#: Reasons a Settings launch can fail, mapped to a human label.
CAUSES = {
    "frida_injection": "Frida was attached to Settings (system apps crash when hooked)",
    "guard_props": "A system property held a phone value where Android expects its own",
    "settings_data": "Settings app data/cache was inconsistent",
    "not_running": "Settings could not be started at all",
    "unknown": "The crash did not match a known cause",
}


class SettingsHealth:
    """Launch Settings, find out why it dies, repair it, verify."""

    def __init__(self, device_serial):
        self.device_serial = device_serial

    # ------------------------------------------------------------------ adb IO
    def _adb(self, args, timeout=30):
        cmd = [config.ADB_PATH, "-s", self.device_serial, *args]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "", f"adb timed out after {timeout}s"
        except Exception as exc:
            return False, "", str(exc)
        return proc.returncode == 0, (proc.stdout or "").strip(), (proc.stderr or "").strip()

    def _shell(self, command, timeout=30):
        return self._adb(["shell", command], timeout=timeout)

    # ------------------------------------------------------------------ probes
    def settings_pid(self):
        """PID of a running Settings process, or 0."""
        ok, out, _ = self._shell(f"pidof {SETTINGS_PACKAGE}", timeout=15)
        if not ok or not out:
            return 0
        for token in out.split():
            if token.isdigit():
                return int(token)
        return 0

    def clear_crash_buffer(self):
        """Drop old crash entries so a diagnosis cannot read a stale crash."""
        # `logcat -b crash -c` is the documented way; failure is harmless.
        self._shell("logcat -b crash -c 2>/dev/null", timeout=15)
        self._shell("logcat -c 2>/dev/null", timeout=15)

    def launch_settings(self, wait=6.0):
        """Start Settings and report ``(started, pid, detail)``.

        ``am start -W`` waits for the activity, which is enough to catch the
        immediate "keeps stopping" case; a slower crash-loop is caught by the
        second pid probe.
        """
        self._shell(f"am force-stop {SETTINGS_PACKAGE}", timeout=15)
        ok, out, err = self._shell(
            f"am start -W -n {shlex.quote(SETTINGS_ACTIVITY)} 2>&1", timeout=max(20, wait * 4)
        )
        detail = f"{out} {err}".strip()
        pid = self.settings_pid()
        if not pid:
            # Some images need the resolver rather than an explicit component.
            self._shell(f"monkey -p {SETTINGS_PACKAGE} -c android.intent.category.LAUNCHER 1", timeout=20)
            pid = self.settings_pid()
        if not pid:
            return False, 0, detail
        # Still alive after the wait window means it really came up.
        time.sleep(min(wait, 8.0))
        still = self.settings_pid()
        return bool(still), still, detail

    def crash_log(self, lines=200):
        """Recent crash-buffer lines mentioning Settings (or the whole buffer)."""
        ok, out, _ = self._adb(
            ["logcat", "-d", "-b", "crash", "-t", str(max(20, lines))], timeout=20
        )
        text = out if ok else ""
        if not text:
            # Some builds put the crash in the main buffer instead.
            ok, out, _ = self._adb(["logcat", "-d", "-t", str(max(20, lines))], timeout=20)
            text = out if ok else ""
        return text or ""

    # ------------------------------------------------------- classification
    @staticmethod
    def classify(crash_text, frida_attached=False, spawn_gating=False,
                 spoofed_qemu=False, frida_server_running=False):
        """Map a crash log (plus live state) to one of :data:`CAUSES`.

        Ordered by how specific the evidence is: an explicit Frida/native-bridge
        signature beats prop evidence, which beats a generic app-data guess.
        """
        low = (crash_text or "").lower()

        frida_markers = (
            "frida",
            "libfrida",
            "gum-js-loop",
            "gmain",
            "frida-agent",
            "pthread_sigmask",
        )
        if any(marker in low for marker in frida_markers):
            return "frida_injection"
        # `spawn_gating` is passed when the caller KNOWS a gate is armed; merely
        # having frida-server up is not evidence, so it is not used here. A
        # gate that is only suspected is handled by the repair path, which
        # disarms it unconditionally (it is a no-op otherwise).
        if frida_attached or spawn_gating:
            return "frida_injection"

        prop_markers = (
            "ro.hardware",
            "ro.product.device",
            "ro.product.board",
            "ro.build.characteristics",
            "ego_suspend",
            "inaccessible or not found",
            "hw_get_module",
            "libegl",
            "libvulkan",
            "unsatisfiedlinkerror",
            "system.loadlibrary",
        )
        if any(marker in low for marker in prop_markers):
            return "guard_props"
        if spoofed_qemu:
            # A phone-valued ro.kernel.qemu with no other evidence: the HAL and
            # renderer paths are the first thing it breaks on AOSP emulator code.
            return "guard_props"

        data_markers = (
            "android.database.sqlite",
            "sqliteexception",
            "sharedpreferences",
            "parcel",
            "badparcelableexception",
            "nullpointerexception",
            "classnotfoundexception",
            "inflateexception",
        )
        if any(marker in low for marker in data_markers):
            return "settings_data"

        if "unable to resolve intent" in low or "does not exist" in low:
            return "not_running"
        return "unknown"

    def frida_running(self):
        """Whether frida-server is alive on the device.

        Delegates to the same probe the hook manager uses, so this diagnosis and
        the app's Frida status card can never disagree.
        """
        try:
            from core.hook_manager import HookManager

            return bool(HookManager(self.device_serial).is_frida_server_running())
        except Exception:
            ok, out, _ = self._shell("ps -A 2>/dev/null | grep frida-server", timeout=15)
            return bool(ok and "frida-server" in (out or ""))

    def guarded_props_wrong(self):
        """Guarded props currently holding a non-native (phone) value.

        Uses one ``getprop`` dump, falling back to per-key reads.
        """
        guarded = set(NEVER_SET)
        found = {}
        ok, out, _ = self._shell("getprop", timeout=30)
        if ok and out:
            for line in out.splitlines():
                line = line.strip()
                if not line.startswith("[") or "]: [" not in line:
                    continue
                name, _, value = line[1:].partition("]: [")
                name = name.strip()
                value = value.rstrip("]").strip()
                if name in guarded and value:
                    found[name] = value
        if not found:
            for key in sorted(guarded):
                ok, out, _ = self._shell(f"getprop {key}", timeout=10)
                if ok and out.strip():
                    found[key] = out.strip()

        wrong = {}
        for key, value in found.items():
            low = value.lower()
            if key == "ro.build.characteristics":
                if value != CHARACTERISTICS_NATIVE:
                    wrong[key] = value
                continue
            if key in ("ro.kernel.qemu", "ro.boot.qemu"):
                # These must be hidden from apps but are safe to mirror; a
                # *phone* value here (not 0/1) is the broken case.
                if value not in ("0", "1"):
                    wrong[key] = value
                continue
            if key == "ro.hardware" or key.startswith("ro.hardware."):
                if not any(t in low for t in ("ranchu", "goldfish", "vsoc", "emulation", "emulator")):
                    wrong[key] = value
                continue
            if key.startswith("ro.product.cpu.abi"):
                if "x86" not in low and value:
                    wrong[key] = value
                continue
            if key in ("ro.boot.verifiedbootstate", "ro.boot.flash.locked", "ro.boot.veritymode"):
                wrong[key] = value  # these are never safe to spoof
                continue
            if is_never_set(key):
                # ro.product.device / ro.product.board / ro.build.product echo
                # the image's own generic codename; anything else is a spoof.
                if not any(t in low for t in ("generic", "ranchu", "emu", "sdk_", "goldfish", "cutf")):
                    wrong[key] = value
        return wrong

    # ------------------------------------------------------------- repairs
    def repair(self, cause):
        """Apply the repair for *cause* and return ``(ok, message)``."""
        if cause == "frida_injection":
            return self._repair_frida()
        if cause == "guard_props":
            return self._repair_props()
        if cause == "settings_data":
            return self._repair_data()
        if cause == "not_running":
            return False, (
                "Settings could not be started. Enable the app: "
                f"`adb shell pm enable {SETTINGS_PACKAGE}`, then run this check again."
            )
        return False, (
            "Could not identify the cause from the crash log - the raw log is "
            "shown in Details so it can be reported."
        )

    def _repair_frida(self):
        """Detach every Frida session from Settings and drop spawn gating."""
        done = []
        try:
            from core.hook_manager import HookManager, detach_device_sessions

            detach_device_sessions(self.device_serial)
            done.append("detached Frida sessions")
            mgr = HookManager(self.device_serial)
            try:
                mgr.disable_spawn_gating()
                done.append("spawn gating disabled")
            except Exception:
                pass
        except Exception as exc:
            return False, f"Could not detach Frida sessions: {exc}"

        # A frida-server that is still running keeps an agent loaded in the
        # app's address space; restart it so Settings starts from a clean slate.
        #
        # ORDER MATTERS: the gate must be disarmed (and everything it holds
        # resumed) BEFORE the server is killed. Killing an armed server strands
        # its gated children with no resume path, and they are then reaped -
        # which is the very symptom being repaired.
        ok, out, _ = self._shell("ps -A 2>/dev/null | grep -c frida-server", timeout=15)
        if ok and out.strip().isdigit() and int(out.strip()) > 0:
            try:
                from core.hook_manager import HookManager as _HM

                _ok, detail = _HM(self.device_serial).disable_spawn_gating()
                if detail and "was not enabled" not in detail:
                    done.append(f"gate cleared before stopping server ({detail})")
            except Exception:
                pass
            self._shell("pkill -f frida-server 2>/dev/null || killall frida-server 2>/dev/null", timeout=15)
            done.append("stopped frida-server")
        self._shell(f"am force-stop {SETTINGS_PACKAGE}", timeout=15)
        # Re-register the component so it is not left in a disabled/crashing
        # state after the bad sessions were closed.
        self._shell(f"pm enable {SETTINGS_PACKAGE} 2>/dev/null", timeout=15)
        return True, "; ".join(done) or "no Frida sessions were attached"

    def _repair_props(self):
        """Restore guarded props to native values and bounce zygote."""
        from spoof.hardware_spoof import HardwareSpoofer

        spoofer = HardwareSpoofer(device_serial=self.device_serial)
        repaired = spoofer._repair_toxic_props()
        if repaired:
            return True, f"restored {len(repaired)} system properties ({', '.join(repaired)})"
        if spoofer.unresolved_toxic:
            return False, (
                "could not restore "
                f"{', '.join(spoofer.unresolved_toxic)} (no original build.prop backup)"
            )
        return True, "no system property needed restoring"

    def _settle_after_repair(self):
        """Give the app a clean restart cycle after a repair.

        A process stuck in a crash-loop is reaped the instant it starts, so a
        fix applied while it is looping does not take effect until the app is
        fully stopped and its crash-loop state cleared. This is deliberately
        limited to Settings itself - no other app's data is touched.
        """
        self._shell(f"am force-stop {SETTINGS_PACKAGE}", timeout=15)
        # A crash loop can leave the app's "crashing" flag set; `pm clear` would
        # wipe user data, so only the transient cache is removed.
        self._shell(
            "rm -rf /data/user/0/com.android.settings/cache "
            "/data/user_de/0/com.android.settings/cache 2>/dev/null",
            timeout=20,
        )
        time.sleep(0.5)

    def _repair_data(self):
        """Clear ONLY Settings' cache - user data stays untouched."""
        cmds = [
            f"am force-stop {SETTINGS_PACKAGE}",
            "rm -rf /data/user/0/com.android.settings/cache "
            "/data/user_de/0/com.android.settings/cache 2>/dev/null",
            f"pm enable {SETTINGS_PACKAGE} 2>/dev/null",
            f"pm enable {SETTINGS_PACKAGE}/com.android.settings.Settings 2>/dev/null",
            # Re-register the component so the launcher entry is not stuck in a
            # disabled state after a bad boot.
            f"cmd package install-existing {SETTINGS_PACKAGE} 2>/dev/null",
        ]
        for cmd in cmds:
            self._shell(cmd, timeout=30)
        return True, "cleared the Settings app cache and re-enabled its component"

    # -------------------------------------------------------------- report
    def diagnose(self, repair=True):
        """Full cycle: launch, classify, repair, re-verify.

        Returns a dict that the GUI/CLI can show verbatim - it never claims
        success it did not observe.
        """
        report = {
            "package": SETTINGS_PACKAGE,
            "serial": self.device_serial,
            "healthy": False,
            "cause": None,
            "cause_label": None,
            "repaired": False,
            "repair_detail": None,
            "crash_log": None,
            "steps": [],
        }

        # Already running and stable? Confirm rather than restart needlessly.
        before = self.settings_pid()
        if before:
            time.sleep(1.5)
            if self.settings_pid() == before:
                report.update(healthy=True, cause=None, steps=[
                    f"Settings already running (pid {before}) and stayed up",
                ])
                return report

        self.clear_crash_buffer()
        started, pid, start_detail = self.launch_settings()
        report["steps"].append(
            f"launch: {'started' if started else 'did NOT stay running'}"
            + (f" (pid {pid})" if pid else "")
            + (f" - {start_detail[:200]}" if start_detail and not started else "")
        )

        if started:
            report.update(healthy=True, steps=report["steps"] + ["Settings survived a launch"])
            return report

        crash = self.crash_log()
        report["crash_log"] = crash[:2000] or None
        wrong_props = self.guarded_props_wrong()
        frida_running = self.frida_running()
        cause = self.classify(
            crash,
            frida_attached=False,
            frida_server_running=frida_running,
            spawn_gating=False,
            spoofed_qemu=bool(wrong_props),
        )
        report["cause"] = cause
        report["cause_label"] = CAUSES.get(cause)
        report["guarded_props"] = wrong_props
        report["steps"].append(f"diagnosis: {CAUSES.get(cause)}")
        if wrong_props:
            report["steps"].append(
                f"guarded props with phone values: {', '.join(sorted(wrong_props))}"
            )

        if not repair:
            return report

        ok, detail = self.repair(cause)
        if ok:
            # Whatever was killing Settings, stop it from doing so again: an old
            # crash-loop can leave the app permanently in a "restarting" state
            # where every launch is reaped immediately.
            self._settle_after_repair()
        report["repaired"] = bool(ok)
        report["repair_detail"] = detail
        report["steps"].append(f"repair: {detail}")

        # Verify: the only claim worth making is an observed healthy Settings.
        self.clear_crash_buffer()
        started2, pid2, _ = self.launch_settings()
        report["healthy"] = bool(started2)
        report["steps"].append(
            "re-check: Settings "
            + ("opens and stays up" if started2 else "STILL crashes after the repair")
        )
        if not started2:
            report["crash_log"] = self.crash_log()[:2000] or report["crash_log"]
        return report
