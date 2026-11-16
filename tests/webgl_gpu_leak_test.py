#!/usr/bin/env python3
"""WebGL / host-GPU leak guards.

A fingerprint report showed

    gpu:  Google (ATI Technologies Inc.)
          Android Emulator OpenGL ES Translator (AMD Radeon RX 6700 XT)

i.e. the *host* graphics stack, with "confidence: high". Two independent
bugs caused it and both are pinned here:

1. `anti_detect.js` only replaced GL strings it recognised as emulator-ish
   ("SwiftShader"/"Android Emulator"), so a passthrough host vendor/renderer
   string was returned untouched. GPUs must now be answered from the identity
   unconditionally.
2. WebGL strings are read in Chromium's sandboxed renderer/GPU process
   (`<pkg>:privileged_process0`), which the hooks never attached to. Those
   processes must now be found and hooked, with a graphics-only script (the
   full one bails out early there by design).
"""
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_sdk import setup_fake_sdk  # noqa: E402

paths = setup_fake_sdk()
TMP = paths["tmp"]
os.environ["HOME"] = str(TMP / "home")
(TMP / "home").mkdir(exist_ok=True)
for key in ("ANDROID_SDK_ROOT", "ANDROID_HOME", "ANDROID_SDK_HOME"):
    os.environ[key] = str(paths["sdk"])

failures = []


def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(label)


from profiles.device_profiles import get_model_profile  # noqa: E402
from profiles.profile_generator import generate_device_identity  # noqa: E402

SCRIPTS = REPO / "frida_toolkit" / "scripts"
anti = (SCRIPTS / "anti_detect.js").read_text(encoding="utf-8")
gpu_js = (SCRIPTS / "gpu_hooks.js").read_text(encoding="utf-8")
hook_src = (REPO / "core" / "hook_manager.py").read_text(encoding="utf-8")
app_src = (REPO / "gui" / "app.py").read_text(encoding="utf-8")
ui = (REPO / "gui" / "static" / "js" / "app.js").read_text(encoding="utf-8")

# ------------------------------------------------------- 1. no passthrough
print("== host GPU strings can no longer pass through ==")
check("GLES20 answers from the identity, not a heuristic",
      "if (spoof) return spoof;" in anti
      and "result.indexOf(\"Android Emulator\")" not in anti)
check("the old emulator-only filter is gone",
      'result.indexOf("SwiftShader")' not in anti)
check("EGL vendor is always spoofed",
      "// Always the spoofed vendor" in anti and "return eglVendor;" in anti)
check("the leak is explained in a comment (host passthrough case)",
      "AMD Radeon RX 6700 XT" in anti and "host" in anti)
check("a host leak is logged, not silent",
      "[emulator/host leak]" in anti)
check("GL_RENDERER/VENDOR/VERSION/EXTENSIONS/GLSL all covered",
      all(e in anti for e in ("0x1F00", "0x1F01", "0x1F02", "0x1F03", "0x1F04")))
check("EGL vendor/version/extensions covered",
      all(e in anti for e in ("0x3053", "0x3054", "0x3055")))

# ------------------------------------------------- 2. renderer processes
print("== Chromium renderer / GPU processes are hooked ==")
check("renderer discovery exists", "def find_renderer_processes" in hook_src)
check("injection helper exists", "def inject_renderer_processes" in hook_src)
check("matches the sandboxed/privileged process names",
      '"privileged_process"' in hook_src and '"sandboxed_process"' in hook_src)
check("matches on the app's own package prefix only",
      'prefix = f"{package_name}:"' in hook_src)
check("a package cannot hook another app's processes",
      "if not name.startswith(prefix):" in hook_src)
check("renderer count is capped", "RENDERER_PROCESS_LIMIT" in hook_src)
_main_ready = hook_src.index("Hooks active in {package_name} ({mode_label})")
_renderer_call = hook_src.index("renderer_count = self.inject_renderer_processes")
check("renderer injection runs after the main process is live",
      _renderer_call > _main_ready
      and hook_src.index("def find_renderer_processes") < _renderer_call)
check("lazy GPU processes are re-scanned after injection",
      "def _schedule_renderer_followup" in hook_src
      and "RENDERER_RETRY_DELAYS" in hook_src)
check("already-hooked renderer pids are skipped",
      'in _LIVE_SESSIONS' in hook_src and "# already hooked by an earlier pass" in hook_src)
check("ps fallback when the Frida API is unavailable",
      '["shell", "ps", "-A", "-o", "PID,NAME"]' in hook_src)

print("== renderer processes get a graphics-only script ==")
check("dedicated generator exists", "def generate_graphics_script" in hook_src)
check("it loads gpu_hooks.js", 'script_path = self.scripts_dir / "gpu_hooks.js"' in hook_src)
check("renderer injection uses the graphics script",
      "wrap_with_java_bridge(self.generate_graphics_script(identity))" in hook_src)
check("the full script still ships every layer",
      all(n in hook_src for n in ("native_hooks.js", "anti_detect.js",
                                  "camera_hooks.js", "safetynet_bypass.js")))
check("gpu_hooks.js exists and is graphics-only", "GPU Hooks" in gpu_js)
check("it does not contain libc file hiding", "isHiddenPath" not in gpu_js)
check("it explains why it is separate (WebGL runs in the GPU process)",
      "privileged_process0" in gpu_js and "WebGL" in gpu_js)

print("== gpu_hooks.js covers the WebGL surfaces ==")
for label, needle in (
    ("native glGetString", 'findExport(GL_LIBS, "glGetString")'),
    ("native glGetStringi (GLES3 extension list)", 'findExport(GL_LIBS, "glGetStringi")'),
    ("native eglQueryString", 'findExport(["libEGL.so"], "eglQueryString")'),
    ("Java GLES20.glGetString", "GLES20.glGetString"),
    ("Java glGetIntegerv limits", "glGetIntegerv"),
    ("Java EGL14.eglQueryString", "EGL14.eglQueryString"),
):
    check(f"{label} spoofed", needle in gpu_js)
check("strings are cached (render-loop hot path)", "stringCache" in gpu_js)
check("answers come from the identity, never hardcoded",
      "id.gl_renderer" in gpu_js and "Adreno (TM) 660" not in gpu_js)
check("EGL_VERSION derived from the spoofed vendor/version",
      '"1.4 " + eglVendor' in gpu_js)
check("extension list is split for glGetStringi", "glExtensions.split" in gpu_js)

# ------------------------------------------------------------- reporting
print("== the dashboard reports the coverage ==")
check("hook-app reports renderer sessions", "renderer_sessions_for(serial, package)" in app_src)
check("hook-app mentions the WebGL/GPU process", "WebGL/GPU process" in app_src)
check("renderer_sessions_for exists", "def renderer_sessions_for" in hook_src)
check("Details explains the GPU process", "WebGL/GPU process" in ui)
check("gpu fact row names the coverage", "app + WebGL/GPU process" in ui)

# ------------------------------------------------------ generated scripts
print("== generated scripts ==")
from core.hook_manager import HookManager  # noqa: E402

identity = generate_device_identity(get_model_profile("Samsung Galaxy S24"))
hooks = HookManager("emulator-5554")
graphics = hooks.generate_graphics_script(identity)
full = hooks.generate_combined_script(identity)
check("graphics script carries the identity config", '"gl_renderer"' in graphics)
check("graphics script carries the real extensions", identity["gl_extensions"].split()[0] in graphics)
check("graphics script has the native hooks", "native glGetString spoofed" in graphics)
check("graphics script has no libc file hiding", "hiddenFiles" not in graphics)
check("full script keeps everything else", "camera_hooks" in full and "isHiddenPath" in full)

# ------------------------------------------------------- renderer matcher
print("== renderer process matching (unit) ==")


class _Proc:
    def __init__(self, name, pid):
        self.name, self.pid = name, pid


class _Dev:
    def __init__(self, procs):
        self._procs = procs

    def enumerate_processes(self):
        return self._procs


procs = [
    _Proc("com.android.chrome", 100),
    _Proc("com.android.chrome:privileged_process0", 101),
    _Proc("com.android.chrome:privileged_process1", 102),
    _Proc("com.android.chrome:sandboxed_process3", 103),
    _Proc("com.android.chrome:remote", 104),
    _Proc("com.other.app:privileged_process0", 105),
    _Proc("com.android.chrome_extra", 106),
]
found = hooks.find_renderer_processes("com.android.chrome", frida_device=_Dev(procs))
names = sorted(p["name"] for p in found)
check("finds privileged + sandboxed renderer processes",
      names == ["com.android.chrome:privileged_process0",
                "com.android.chrome:privileged_process1",
                "com.android.chrome:sandboxed_process3"], str(names))
check("ignores the app's main process and other services",
      all(not n.endswith(":remote") and n != "com.android.chrome" for n in names))
check("ignores other apps' renderer processes",
      all(not n.startswith("com.other") for n in names))
check("ignores prefix look-alikes", "com.android.chrome_extra" not in names)

limited = hooks.find_renderer_processes(
    "com.android.chrome",
    frida_device=_Dev([_Proc(f"com.android.chrome:privileged_process{i}", 200 + i)
                       for i in range(20)]),
)
check("result count is capped", len(limited) == HookManager.RENDERER_PROCESS_LIMIT,
      str(len(limited)))
check("no processes -> empty list",
      hooks.find_renderer_processes("com.android.chrome", frida_device=_Dev([])) == [])

if failures:
    print(f"\n{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("\nAll WebGL/host-GPU leak checks passed.")
