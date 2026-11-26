"""End-to-end verification of the spoofing pipeline ("is it actually working?").

The user's problem was never a single bug. It was that the dashboard could
display **"Verified (24/24 props)"** and **"✓ Hooks"** while the phone was
demonstrably broken - Settings dying, hooks not applying to new apps. Every
individual component reported success, so nothing told the truth about the
system as a whole.

This module answers one question honestly: **on this device, right now, does
the spoof actually work?** It checks each link in the chain and reports PASS /
FAIL / SKIP per link, with the *observed* evidence, and an overall verdict
that is only "ok" when nothing failed.

The chain:

1. **device**   - adb sees it and it has booted
2. **props**    - the guarded keys hold native values (a poisoned device fails)
3. **identity** - the cosmetic identity props really read back as spoofed
4. **frida**    - frida-server runs with -P and answers an RPC
5. **gating**   - spawn gating is armed *and its resume path is alive*
6. **settings** - the Android Settings app opens and stays up

Design rules, learned the hard way:

* A check that cannot run is SKIP, never PASS ("we could not tell" is not "it
  works"). That is what makes the verdict trustworthy.
* "Hooks active" is only claimed when something was *observed* hooked - a
  server answering, or a spawned process actually resumed. A gate that is
  armed but has no live resume path is reported as broken, because that is
  precisely the state that kills apps.
* Settings is verified by observation (`pidof` twice), not by inference.
"""

from __future__ import annotations

import shutil
import subprocess
import time

import config

PASS = "pass"
FAIL = "fail"
SKIP = "skip"

#: Order matters for reading the report: the first failure is usually the
#: cause of the ones after it.
CHAIN = (
    ("device", "Device is connected and booted"),
    ("props", "Guarded system props hold native values"),
    ("identity", "Spoofed identity reads back on the device"),
    ("frida", "frida-server runs with -P and answers"),
    ("gating", "Spawn gating is armed with a live resume path"),
    ("settings", "Android Settings opens and stays up"),
)


def _run(cmd, timeout=25):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode == 0, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return False, "", f"timed out after {timeout}s"
    except Exception as exc:
        return False, "", str(exc)


class PipelineCheck:
    """Verify the whole spoofing chain on one device."""

    def __init__(self, device_serial, identity=None, expected=None):
        self.device_serial = device_serial
        self.identity = identity or {}
        # key -> expected value, for the identity read-back
        self.expected = expected or {}
        self.results = []

    # ------------------------------------------------------------------ io
    def _adb(self, args, timeout=25):
        return _run([config.ADB_PATH, "-s", self.device_serial, *args], timeout=timeout)

    def _shell(self, command, timeout=25):
        return self._adb(["shell", command], timeout=timeout)

    def _prop(self, key):
        ok, out, _ = self._shell(f"getprop {key}", timeout=15)
        return out if ok else ""

    # -------------------------------------------------------------- checks
    def check_device(self):
        ok, out, err = _run([config.ADB_PATH, "devices"], timeout=20)
        if not ok:
            return FAIL, f"adb could not list devices: {err or 'no output'}", {}
        state = ""
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[0] == self.device_serial:
                state = parts[1]
                break
        if not state:
            return FAIL, f"{self.device_serial} is not listed by adb", {"serial": self.device_serial}
        if state != "device":
            return FAIL, f"{self.device_serial} is '{state}', not ready", {"state": state}
        ok_boot, boot, _ = self._shell("getprop sys.boot_completed", timeout=15)
        if not (ok_boot and boot.strip() == "1"):
            return FAIL, "device is listed but has not finished booting", {"boot_completed": boot}
        return PASS, "connected and booted", {"state": state}

    def check_props(self):
        """The guarded keys must hold NATIVE values, or Settings/SystemUI die."""
        try:
            from spoof.hardware_spoof import HardwareSpoofer
            from spoof import prop_policy
        except Exception as exc:
            return SKIP, f"cannot load the prop policy: {exc}", {}

        # A short, load-bearing subset - the same idea as the full audit, kept
        # cheap enough to run on every "is it working?" request.
        keys = [
            "ro.hardware",
            "ro.product.board",
            "ro.board.platform",
            "ro.product.device",
            "ro.build.product",
            "ro.build.characteristics",
            "ro.product.cpu.abilist",
            "ro.zygote",
            "ro.boot.verifiedbootstate",
            "ro.boot.flash.locked",
        ]
        wrong = {}
        for key in keys:
            value = self._prop(key)
            if not value:
                continue  # absent is not "wrong"
            if prop_policy.is_never_set(key) and self._holds_phone_value(key, value):
                wrong[key] = value
        if wrong:
            detail = ", ".join(f"{k}={v}" for k, v in sorted(wrong.items()))
            return FAIL, f"load-bearing props hold phone values: {detail}", {"wrong": wrong}
        return PASS, f"{len(keys)} load-bearing props hold native values", {}

    @staticmethod
    def _holds_phone_value(key, value):
        """True when *value* looks like a retail phone's, not the emulator's."""
        from spoof import prop_policy

        hints = getattr(prop_policy, "_NATIVE_HINTS", {})
        native = hints.get(key)
        low = str(value).lower()
        if native is not None:
            if not native:
                return False
            return not any(hint and hint in low for hint in native if hint)
        # For everything else: the emulator's own vocabulary.
        native_words = ("ranchu", "goldfish", "generic", "emulator", "sdk_",
                        "x86", "qemu", "null", "unknown", "verified")
        return not any(word in low for word in native_words)

    def check_identity(self):
        """The cosmetic identity must READ BACK as spoofed, not merely be set."""
        if not self.expected:
            return SKIP, "no expected identity supplied", {}
        mismatched = {}
        for key, want in self.expected.items():
            got = self._prop(key)
            if not got:
                mismatched[key] = "empty"
            elif str(got).strip() != str(want).strip():
                mismatched[key] = got
        if mismatched:
            detail = ", ".join(f"{k}: want {self.expected[k]!r} got {v!r}"
                               for k, v in sorted(mismatched.items()))
            return FAIL, f"identity did not stick: {detail}", {"mismatched": mismatched}
        return PASS, f"{len(self.expected)} identity props read back correctly", {}

    def check_frida(self):
        """frida-server must be running WITHOUT preload, and answer an RPC."""
        try:
            from frida_toolkit.frida_server_manager import FridaServerManager
            from core.hook_manager import HookManager
        except Exception as exc:
            return SKIP, f"cannot load the Frida integration: {exc}", {}

        try:
            manager = FridaServerManager(self.device_serial)
            running = manager.is_running()
        except Exception as exc:
            return SKIP, f"could not query frida-server: {exc}", {}

        if not running:
            return FAIL, "frida-server is not running (hooks cannot apply)", {}

        data = {}
        try:
            without_preload = manager.running_without_disable_preload()
            data["preload_enabled"] = bool(without_preload)
        except Exception:
            without_preload = False
        if without_preload:
            return FAIL, (
                "frida-server is running WITHOUT -P: it pre-injects into zygote, "
                "which crashes forked apps (Settings first). Restart it via Hooks."
            ), data

        hooks = HookManager(self.device_serial)
        try:
            ready = hooks._verify_frida_ready()
        except Exception as exc:
            return FAIL, f"frida-server did not answer: {exc}", data
        if not ready:
            return FAIL, f"frida-server did not answer: {hooks.last_error or 'no detail'}", data
        return PASS, "frida-server is running with -P and answering", data

    def check_gating(self):
        """Spawn gating armed AND its resume path alive - both, not either."""
        try:
            from core import hook_manager as hm
            from core.hook_manager import HookManager
        except Exception as exc:
            return SKIP, f"cannot load the hook manager: {exc}", {}

        # Ask the PROCESS-wide registry, not one instance's flag: the gate is a
        # latch inside frida-server, and the hook run that armed it used a
        # different HookManager instance than this check.
        pinned = bool(hm._SPAWN_GATING_DEVICES.get(self.device_serial))
        janitor = bool(hm._SPAWN_GATING_JANITORS.get(self.device_serial))
        armed = self.device_serial in getattr(hm, "_SPAWN_GATING_ARMED", set())
        data = {"armed": armed, "callback_pinned": pinned, "janitor": janitor}

        if not armed:
            return SKIP, (
                "spawn gating is not armed in this process - new apps will not be "
                "hooked automatically. Press Hooks on the card to arm it."
            ), data

        # An armed gate with no resume path is the state that kills apps: every
        # spawn is held at exec and nothing releases it.
        if not pinned and not janitor:
            return FAIL, (
                "spawn gating is armed but has NO resume path in this process - "
                "every app you open will be frozen and then killed (Settings first)."
            ), data
        return PASS, "spawn gating is armed with a live resume path", data

    def check_settings(self, repair=False):
        """Settings must open AND stay up - observed, not inferred."""
        try:
            from core.settings_doctor import SettingsHealth
        except Exception as exc:
            return SKIP, f"cannot load the Settings doctor: {exc}", None

        doctor = SettingsHealth(self.device_serial)
        try:
            report = doctor.diagnose(repair=repair)
        except Exception as exc:
            return FAIL, f"the Settings check itself failed: {exc}", None

        data = report
        if report.get("healthy"):
            steps = report.get("steps") or []
            return PASS, steps[-1] if steps else "Settings opens and stays up", data
        cause = report.get("cause_label") or "cause not recognised"
        return FAIL, f"Settings does not stay up: {cause}", data

    # ------------------------------------------------------------- driver
    def run(self, repair_settings=False):
        """Run every check in order and return an honest report."""
        self.results = []
        started = time.time()

        runners = {
            "device": lambda: self.check_device(),
            "props": self.check_props,
            "identity": self.check_identity,
            "frida": self.check_frida,
            "gating": self.check_gating,
            "settings": lambda: self.check_settings(repair=repair_settings),
        }

        # If the device is unreachable, every later check is meaningless: mark
        # them SKIP rather than pretending to have verified anything.
        device_ok = True
        for key, label in CHAIN:
            if not device_ok:
                self.results.append({
                    "id": key, "label": label, "status": SKIP,
                    "detail": "skipped: the device is not reachable",
                })
                continue
            try:
                status, detail, data = runners[key]()
            except Exception as exc:
                status, detail, data = FAIL, f"check crashed: {exc}", {}
            if key == "device" and status != PASS:
                device_ok = False
            entry = {"id": key, "label": label, "status": status, "detail": detail}
            if data is not None:
                entry["data"] = data
            self.results.append(entry)

        failed = [r for r in self.results if r["status"] == FAIL]
        skipped = [r for r in self.results if r["status"] == SKIP]
        if failed:
            verdict = FAIL
            summary = (
                f"{len(failed)} of {len(CHAIN)} checks FAILED - the first failure "
                f"is '{failed[0]['label']}': {failed[0]['detail']}"
            )
        elif skipped and len(skipped) == len(CHAIN):
            verdict = SKIP
            summary = "nothing could be verified"
        else:
            verdict = PASS
            summary = "the spoof works on this device: apps open, hooks apply, Settings survives"
            if skipped:
                summary += f" ({len(skipped)} check(s) skipped - not verified)"

        return {
            "success": True,
            "serial": self.device_serial,
            "verdict": verdict,
            "summary": summary,
            "checks": self.results,
            "failed": [r["id"] for r in failed],
            "skipped": [r["id"] for r in skipped],
            "elapsed_seconds": round(time.time() - started, 1),
        }


def verify_device(device, repair_settings=False):
    """Convenience wrapper: verify the pipeline for a saved *device* dict."""
    serial = device.get("serial") or ""
    identity = device.get("identity") or {}
    expected = {}
    for key, field in (
        ("ro.product.model", "model"),
        ("ro.product.brand", "brand"),
        ("ro.build.fingerprint", "fingerprint"),
    ):
        value = identity.get(field)
        if value:
            expected[key] = value
    return PipelineCheck(serial, identity=identity, expected=expected).run(
        repair_settings=repair_settings
    )


def format_report(report, colour=True):
    """Human-readable lines for the CLI (and plain text for logs)."""
    icons = {PASS: "PASS", FAIL: "FAIL", SKIP: "SKIP"}
    if colour:
        icons = {PASS: "\033[92mPASS\033[0m", FAIL: "\033[91mFAIL\033[0m",
                 SKIP: "\033[93mSKIP\033[0m"}
    lines = []
    for row in report.get("checks", []):
        lines.append(f"[{icons.get(row['status'], row['status'])}] "
                     f"{row['label']}\n        {row['detail']}")
    lines.append("")
    lines.append(f"verdict: {report.get('verdict', '?').upper()} - {report.get('summary', '')}")
    return "\n".join(lines)
