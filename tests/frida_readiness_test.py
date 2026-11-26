#!/usr/bin/env python3
"""Regression: a Frida agent bug must not be reported as a dead server.

Screenshot that triggered this: 37/37 props verified, device Running, but
"⚠ Hooks failed" with

    frida-server not reachable via Frida API: java.lang.NullPointerException:
    Attempt to invoke virtual method 'java.util.List
    android.content.pm.PackageManager.queryIntentActivities(android.content.Intent,
    int)' on a null object reference

That NPE comes from Frida's own internal Java agent on some Android builds
(frida/frida#1879). The old readiness probe tried one API, let its exception
abort the whole hook flow, and never tried the others or ADB. These tests run
the *real* _verify_frida_ready() body against fake devices where the server
is demonstrably alive.
"""
import sys
import textwrap
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

failures = []


def check(label, ok, detail=""):
    if ok:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label}" + (f" — {detail}" if detail else ""))
        failures.append(label)


def load_verify():
    src = (REPO / "core" / "hook_manager.py").read_text(encoding="utf-8")
    start = src.index("    def _verify_frida_ready(self) -> bool:")
    end = src.index("    def _frida_device(", start)
    ns = {"List": list}
    exec(textwrap.dedent(src[start:end]), ns)
    return ns["_verify_frida_ready"]


NPE = (
    "java.lang.NullPointerException: Attempt to invoke virtual method "
    "'java.util.List android.content.pm.PackageManager.queryIntentActivities"
    "(android.content.Intent, int)' on a null object reference"
)


class FakeDevice:
    """A device where only selected probes fail."""

    def __init__(self, failing=()):
        self.failing = set(failing)
        self.calls = []

    def _probe(self, name):
        self.calls.append(name)
        if name in self.failing:
            raise RuntimeError(NPE)
        return object()

    def get_process(self, _name):
        return self._probe("get_process")

    def query_system_parameters(self):
        return self._probe("query_system_parameters")

    def enumerate_processes(self):
        return self._probe("enumerate_processes")


class FakeManager:
    def __init__(self, device=None, server_running=True, connect_error=None):
        self.device = device
        self.server_running = server_running
        self.connect_error = connect_error
        self.last_error = None

    def start_frida_server(self):
        return True

    def _frida_device(self, _frida_module, timeout=10):
        if self.connect_error:
            raise self.connect_error
        return self.device

    def is_frida_server_running(self):
        return self.server_running


verify = load_verify()
real_frida = sys.modules.get("frida")
sys.modules["frida"] = types.ModuleType("frida")
try:
    print("== 1. one broken probe cannot fail a healthy server ==")
    device = FakeDevice(failing={"get_process", "query_system_parameters"})
    manager = FakeManager(device=device)
    check("a Frida internal-agent NPE is survived", verify(manager) is True)
    check("the healthy server is not called unreachable", manager.last_error is None,
          repr(manager.last_error))
    check("all three probes were given a chance",
          device.calls == ["get_process", "query_system_parameters",
                           "enumerate_processes"], str(device.calls))

    print("\n== 2. the cheap probe alone is enough ==")
    device = FakeDevice()
    manager = FakeManager(device=device)
    check("get_process() answers -> ready", verify(manager) is True)
    check("later, buggy probes are never called", device.calls == ["get_process"],
          str(device.calls))

    print("\n== 3. an internal-agent NPE during connect is not a dead server ==")
    manager = FakeManager(connect_error=RuntimeError(NPE), server_running=True)
    check("ADB fallback keeps hooks usable", verify(manager) is True)
    check("no misleading error is stored", manager.last_error is None,
          repr(manager.last_error))

    print("\n== 4. a genuinely dead server still fails closed ==")
    manager = FakeManager(
        device=FakeDevice(failing={"get_process", "query_system_parameters",
                                   "enumerate_processes"}),
        server_running=False,
    )
    check("all probes and ADB failing -> not ready", verify(manager) is False)
    check("the real NPE is kept in the error for diagnosis",
          "NullPointerException" in (manager.last_error or ""),
          repr(manager.last_error))
    check("the error says the server is not reachable",
          "not reachable" in (manager.last_error or ""), repr(manager.last_error))
finally:
    if real_frida is None:
        sys.modules.pop("frida", None)
    else:
        sys.modules["frida"] = real_frida

if failures:
    print(f"\n{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
