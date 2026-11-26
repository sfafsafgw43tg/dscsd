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


print("\n== 5. the spawn gate can never strand a frozen process ==")
hm_full = (REPO / "core" / "hook_manager.py").read_text(encoding="utf-8")
check("the gating device is pinned so its callback cannot be collected",
      "_SPAWN_GATING_DEVICES" in hm_full
      and "_SPAWN_GATING_DEVICES[serial or \"\"] = (device, _on_spawned)" in hm_full)
check("the pin is released when the gate is disarmed",
      "_SPAWN_GATING_DEVICES.pop(serial" in hm_full)
check("a janitor resumes anything the handler missed",
      "_start_spawn_gating_janitor" in hm_full
      and "enumerate_pending_spawn" in hm_full
      and "handler missed it" in hm_full)
check("the janitor stops with the gate", "_SPAWN_GATING_JANITORS.pop" in hm_full)
# A spawn-handler failure must still resume; relying on a possibly-unbound
# local `pid` meant a bad spawn was simply never resumed (guaranteed freeze).
check("the spawn handler resumes even when its own code failed",
      "resume_pid = locals().get(\"pid\")" in hm_full)

print("\n== 6. the janitor really resumes a frozen process ==")
import time as _time
from core.hook_manager import _SPAWN_GATING_JANITORS

class _Spawn:
    def __init__(self, pid, ident): self.pid, self.identifier = pid, ident

class _FakeGatedDevice:
    def __init__(self):
        self.pending = [_Spawn(4242, "com.android.settings")]
        self.resumed = []
    def enumerate_pending_spawn(self): return list(self.pending)
    def resume(self, pid):
        self.resumed.append(pid)
        self.pending = [p for p in self.pending if p.pid != pid]

gdev = _FakeGatedDevice()
_jan = HookManager(device_serial="emulator-janitor")
_jan._start_spawn_gating_janitor(gdev, "emulator-janitor")
_deadline = _time.time() + 14
while _time.time() < _deadline and not gdev.resumed:
    _time.sleep(0.3)
check("a gated Settings process is resumed by the janitor",
      4242 in gdev.resumed, f"resumed={gdev.resumed}")
check("nothing is left frozen afterwards", not gdev.pending,
      str([p.pid for p in gdev.pending]))
_stop = _SPAWN_GATING_JANITORS.pop("emulator-janitor", None)
if _stop:
    _stop[0].set()

print("\n== 7. Settings repair clears the gate BEFORE killing the server ==")
doc_src = (REPO / "core" / "settings_doctor.py").read_text(encoding="utf-8")
_repair = doc_src[doc_src.index("def _repair_frida"):doc_src.index("def _repair_props")]
check("the gate is disarmed before frida-server is killed",
      _repair.index("gate cleared before stopping server")
      < _repair.index("pkill -f frida-server"),
      "killing an armed server strands its gated children")

print("\n== 8. the UI cannot stay at \"Hooking...\" forever ==")
appjs = (REPO / "gui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
_upd = appjs[appjs.index("socket.on('hooks_updated'"):]
_upd = _upd[:_upd.index("socket.on(", 10)] if "socket.on(" in _upd[10:] else _upd
check("the launch path's hooks-finished event clears the busy flag",
      "busyActions.delete(payload.name)" in _upd[:800])
check("it repaints so the button label changes", "renderDevices()" in _upd[:900])
check("loading devices clears stale server-tracked phases",
      "if (!d.hooks_in_flight && busy === 'hooking') busyActions.delete(d.name)"
      in appjs)
check("a deleted device is never left busy",
      "devices.some(d => d.name === name)" in appjs)

if failures:
    print(f"\n{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("\nAll hook-flow checks passed.")
