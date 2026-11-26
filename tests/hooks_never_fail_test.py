#!/usr/bin/env python3
"""Regression test: the hook flow must be able to succeed at all.

The reported bug (from a screenshot): the dashboard said **"⚠ Hooks failed"**
while frida-server was demonstrably running (v17.26.2), and the Android
Settings app kept dying. The two turned out to be one problem.

``inject_system_wide()`` runs the frida readiness probe on a worker thread and
publishes the result as ``self._verify_ok``. The join branch then read a
*local* ``_verify_ok`` that had been initialised to ``False`` and never
touched, so:

    if t.is_alive() or not _verify_ok:      # always true

The method returned ``False`` on **every single attempt**, even when frida
answered perfectly. Consequences:

  * "Hooks failed" on a healthy device - which is what the screenshot showed;
  * spawn gating never enabled, so no app was ever hooked;
  * because no app was hooked, per-app identity never applied - the spoof
    looked "verified" (24/24 props, set natively by resetprop) while apps
    still saw emulator values.

This test executes the real method's control flow with a fake device whose
every operation *succeeds*. Success must therefore be reported as success.

It also pins a second guarantee: no code path may inject Frida into Settings.
Attaching to ``com.android.settings`` on Android 14 kills it, so "hooks never
touch Settings" has to hold even when a caller passes force/allow_protected.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO = Path(__file__).resolve().parent.parent
failures = []


def check(label, ok, detail=""):
    if ok:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label}" + (f" — {detail}" if detail else ""))
        failures.append(label)


def load_inject_system_wide():
    """Compile the real method body with all hardware calls stubbed to succeed."""
    src = (REPO / "core" / "hook_manager.py").read_text(encoding="utf-8")
    start = src.index("    def inject_system_wide(self, identity: Dict) -> bool:")
    end = src.index("    def inject_package(", start)
    fn_src = src[start:end]

    ns = {}
    exec("import threading", ns)
    exec(
        "class Fake:\n"
        "    last_error = None\n"
        "    _verify_ok = None\n"
        "    _gms_result = None\n"
        "    _sg_ok = None\n",
        ns,
    )
    Fake = ns["Fake"]
    # Every operation succeeds - so any failure report is a bug in the flow.
    for name, fn in {
        "_verify_frida_ready": lambda self: True,
        "stop_session": lambda self, p: None,
        "inject_hooks": lambda self, *a, **k: True,
        "_enable_spawn_gating": lambda self, identity: None,
    }.items():
        setattr(Fake, name, fn)

    g = {"Dict": dict}
    exec(fn_src.replace("    def inject_system_wide", "def inject_system_wide"), g)
    return g["inject_system_wide"], Fake


print("== 1. a healthy device must not be reported as a hook failure ==")
inject_system_wide, Fake = load_inject_system_wide()
fake = Fake()
result = inject_system_wide(fake, {})
check("inject_system_wide() returns True when the server answers", result is True,
      f"returned {result!r}, last_error={fake.last_error!r}")
check("it does not claim an unreachable server", not fake.last_error,
      f"last_error={fake.last_error!r}")

print("\n== 2. the worker's result is actually read ==")
hm_src = (REPO / "core" / "hook_manager.py").read_text(encoding="utf-8")
start = hm_src.index("    def inject_system_wide(self, identity: Dict) -> bool:")
end = hm_src.index("    def inject_package(", start)
body = hm_src[start:end]
check("the worker publishes its result on self",
      "self._verify_ok = self._verify_frida_ready()" in body)
check("the join branch reads the same attribute, not a shadowing local",
      "not self._verify_ok" in body)
# A local named _verify_ok that is assigned False and then read is the bug.
check("no local _verify_ok shadows the threaded result",
      not re.search(r"^\s+_verify_ok\s*=\s*False\s*$", body, re.M),
      "found a local '_verify_ok = False'")
check("a timeout still fails closed",
      "if t.is_alive() or not self._verify_ok:" in body)

print("\n== 3. Settings is never hooked automatically ==")
from core.hook_manager import HookManager, PROTECTED_SYSTEM_PACKAGES

check("Settings is a protected system package",
      "com.android.settings" in PROTECTED_SYSTEM_PACKAGES)
check("SystemUI is protected too", "com.android.systemui" in PROTECTED_SYSTEM_PACKAGES)

hm = HookManager(device_serial="emulator-5554")
# The default path - what happens on every launch - must refuse.
for pkg in ("com.android.settings", "com.android.systemui",
            "com.android.providers.settings", "system_server"):
    ok = hm.inject_hooks(pkg, {})
    check(f"inject_hooks({pkg}) refuses by default", ok is False)
check("the refusal explains the crash risk",
      "causes Android 14 crashes" in (hm.last_error or ""), repr(hm.last_error))

# A third-party app must NOT be blocked by the same guard.
_hm2 = HookManager(device_serial="emulator-5554")
_hm2.start_frida_server = lambda: False  # stop before touching a real device
_hm2.inject_hooks("com.spotify.music", {})
check("a third-party app is not caught by the system-app guard",
      "Skipped Frida injection into protected" not in (_hm2.last_error or ""),
      repr(_hm2.last_error))

# Spawn gating (what hooks every app the user opens) must skip system packages
# outright - this is the path that actually ran on the user's device.
hm_src_gate = hm_src[hm_src.index("def _enable_spawn_gating"):
                     hm_src.index("def _spawn_inject_worker")]
check("spawn gating lists Settings as do-not-hook",
      '"com.android.settings"' in hm_src_gate)
check("spawn gating skips every com.android.* package",
      'base_pkg.startswith("com.android.")' in hm_src_gate)
check("spawn gating skips every com.google.android.* package",
      'base_pkg.startswith("com.google.android.")' in hm_src_gate)

print("\n== 4. frida-server is always started without preload ==")
fsm = (REPO / "frida_toolkit" / "frida_server_manager.py").read_text(encoding="utf-8")
startup = fsm[fsm.index("def start_server"):]
# Only the lines that actually launch the server (not rm/pkill cleanup).
launch_lines = [l for l in startup.splitlines()
                if "REMOTE_PATH}" in l and "nohup" in l or "su -c '" in l]
check("every frida-server start passes -P", launch_lines and
      all("-P" in l for l in launch_lines),
      "; ".join(l.strip() for l in launch_lines))

if failures:
    print(f"\n{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("\nAll hook-flow checks passed.")
