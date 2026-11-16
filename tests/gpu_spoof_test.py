#!/usr/bin/env python3
"""GPU spoofing: one coherent GPU identity per device, applied by the hooks.

An emulator is caught on GPUs by *mismatches*, not by a single string:
an "Adreno" renderer together with ARM's Mali extensions, a Qualcomm driver
version on a Mali chip, emulator limits, or the emulator's own
"Android Emulator OpenGL ES Translator" GL_RENDERER. These checks pin the
consistency rules and the plumbing that ships them to the device.
"""
import json
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

failures = []


def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(label)


from profiles.device_profiles import get_model_profile  # noqa: E402
from profiles.gpu_profiles import (  # noqa: E402
    GPU_MODELS,
    gpu_extension_string,
    gpu_profile,
    vendor_for_renderer,
)
from profiles.profile_generator import generate_device_identity  # noqa: E402
from spoof.gpu_spoof import (  # noqa: E402
    GPUSpoofer,
    build_gpu_identity,
    get_gpu_for_device,
)

# --------------------------------------------------------------- database
print("== GPU model database is coherent ==")
bad = []
for name, profile in GPU_MODELS.items():
    if profile["vendor"] != vendor_for_renderer(name):
        bad.append(f"{name}: vendor {profile['vendor']}")
    exts = gpu_extension_string(name).split()
    if profile["vendor"] != "Qualcomm" and any("QCOM" in e for e in exts):
        bad.append(f"{name}: QCOM extensions on {profile['vendor']}")
    if profile["vendor"] != "ARM" and any("ARM_" in e for e in exts):
        bad.append(f"{name}: ARM extensions on {profile['vendor']}")
    for key in ("vendor", "egl_vendor", "driver", "hw", "shader", "extras",
                "limits", "score", "year"):
        if key not in profile:
            bad.append(f"{name}: missing {key}")
    if not profile["limits"].get("texture") or not profile["limits"].get("glsl"):
        bad.append(f"{name}: incomplete limits")
check("every model is vendor-consistent and complete", not bad, "; ".join(bad[:4]))

check("Adreno is Qualcomm", vendor_for_renderer("Adreno (TM) 740") == "Qualcomm")
check("Mali is ARM", vendor_for_renderer("Mali-G715") == "ARM")
check("Xclipse is Samsung", vendor_for_renderer("Xclipse 920") == "Samsung")
check("unknown chip falls back to a coherent profile",
      gpu_profile("SomeUnknownGPU 9000")["vendor"] in ("Qualcomm", "ARM", "Samsung"))

# ------------------------------------------------------------ identities
print("== generated identities are self-consistent ==")
for model_name in ("Samsung Galaxy S24", "Google Pixel 7a", "Xiaomi 13",
                   "OnePlus 11", "Samsung Galaxy S22"):
    profile = get_model_profile(model_name)
    identity = generate_device_identity(profile)
    renderer = identity["gl_renderer"]
    vendor = identity["gl_vendor"]
    exts = identity["gl_extensions"].split()
    limits = identity["gl_limits"]
    label = f"{model_name} ({vendor} {renderer})"
    check(f"{model_name}: renderer/vendor agree", vendor == vendor_for_renderer(renderer),
          f"{renderer} vs {vendor}")
    if vendor == "Qualcomm":
        check(f"{model_name}: Adreno advertises QCOM extensions",
              any("QCOM" in e for e in exts) and not any("ARM_" in e for e in exts))
    elif vendor == "ARM":
        check(f"{model_name}: Mali advertises ARM extensions",
              any("ARM_" in e for e in exts) and not any("QCOM" in e for e in exts))
    check(f"{model_name}: GL version matches the driver",
          identity["gl_version"].startswith("OpenGL ES 3.2")
          and gpu_profile(renderer)["driver"] in identity["gl_version"],
          identity["gl_version"])
    check(f"{model_name}: limits present and plausible",
          limits["max_texture_size"] >= 4096 and limits["max_viewport_dims"][0] >= 4096
          and limits["glsl_version"].startswith("OpenGL ES GLSL ES 3."))
    check(f"{model_name}: extension string is sorted/deduped",
          exts == sorted(set(exts)))
    check(f"{model_name}: egl vendor matches the GL vendor",
          identity["egl_vendor"] == vendor)
    print(f"       -> {label}: {len(exts)} extensions, "
          f"{limits['max_texture_size']} max texture")

check("no emulator renderer string leaks into a generated identity",
      "Android Emulator" not in identity["gl_extensions"]
      and "SwiftShader" not in identity["gl_renderer"])

# --------------------------------------------------------------- spoofer
print("== GPUSpoofer ==")
spoofer = GPUSpoofer("emulator-5554")
gen = spoofer.generate_gpu_identity("Samsung", "Samsung Galaxy S24")
check("generate_gpu_identity is complete",
      all(k in gen for k in ("gl_renderer", "gl_vendor", "gl_version", "egl_vendor",
                             "gl_extensions", "gl_limits", "gpu_name", "gpu_hw")))
forced = spoofer.generate_gpu_identity("Samsung", "Samsung Galaxy S24",
                                       renderer="Adreno (TM) 660")
check("an explicit renderer is respected (stable re-spoof)",
      forced["gl_renderer"] == "Adreno (TM) 660" and forced["gl_vendor"] == "Qualcomm")
check("build_gpu_identity never randomises a saved GPU",
      build_gpu_identity({"brand": "Google", "model": "Pixel 8",
                          "gl_renderer": "Mali-G715"})["gl_renderer"] == "Mali-G715")
_saved_block = {"gl_renderer": "Xclipse 940", "gl_vendor": "Samsung",
                "gl_extensions": "GL_OES_EGL_image"}
check("a saved coherent GPU block wins (re-applying never changes the GPU)",
      build_gpu_identity({"brand": "Google", "model": "Pixel 8",
                          "gpu": _saved_block}) == _saved_block)
check("a device JSON's nested identity is honoured",
      build_gpu_identity({"identity": {"brand": "Samsung", "model": "Galaxy S24",
                                       "gl_renderer": "Mali-G720"}})["gl_renderer"]
      == "Mali-G720")

applied = []


def _fake_adb(command):
    applied.append(" ".join(command))
    return ""


spoofer._adb = _fake_adb
result = spoofer.spoof_gpu_props("Xiaomi", "Xiaomi 13")
check("spoof_gpu_props writes cosmetic GPU props", len(applied) >= 3, str(applied))
check("spoof_gpu_props never touches ro.hardware.egl / vulkan",
      not any("ro.hardware.egl" in c or "ro.hardware.vulkan" in c for c in applied),
      str(applied))
check("spoof_gpu_props returns the identity", bool(result.get("gl_extensions")))

src = (REPO / "spoof" / "gpu_spoof.py").read_text(encoding="utf-8")
check("gpu_spoof.py documents / avoids the driver-breaking props",
      '"ro.hardware.egl":' not in src and '"ro.hardware.vulkan":' not in src)

# -------------------------------------------------------- hardware spoof
print("== spoofing pass reports the GPU ==")
from spoof.hardware_spoof import HardwareSpoofer  # noqa: E402

hw = HardwareSpoofer("emulator-5554")
calls = []
hw._adb = lambda cmd, timeout=30: (calls.append(" ".join(cmd)) or "")
hw.prop_report = {"props": {}, "method": "none", "verified": {}, "failed": []}
gpu_identity = hw.spoof_gpu({"brand": "Samsung", "model": "Samsung Galaxy S24",
                             "gl_renderer": "Mali-G720"})
check("spoof_gpu returns the identity", gpu_identity["gl_renderer"] == "Mali-G720")
check("spoof_gpu records it in prop_report",
      hw.prop_report["gpu"]["renderer"] == "Mali-G720")
check("spoof_gpu says how it is applied (frida hooks)",
      "frida" in hw.prop_report["gpu"]["method"].lower())
check("spoof_gpu writes only cosmetic props",
      all("ro.hardware" not in c for c in calls), str(calls))
check("spoof_all includes the GPU step",
      "self.spoof_gpu(identity)" in (REPO / "spoof" / "hardware_spoof.py").read_text())

# ----------------------------------------------------------- Frida payload
print("== Frida payload ==")
from core.hook_manager import HookManager  # noqa: E402

identity = generate_device_identity(get_model_profile("Google Pixel 7a"))
combined = HookManager("emulator-5554").generate_combined_script(identity)
for key in ("gl_renderer", "gl_vendor", "gl_version", "egl_vendor",
            "gl_extensions", "gl_limits", "gpu_name"):
    check(f"payload ships {key}", f'"{key}"' in combined)
check("payload carries the real extension string",
      identity["gl_extensions"].split()[0] in combined)
check("payload carries the numeric limits",
      str(identity["gl_limits"]["max_texture_size"]) in combined)

anti = (REPO / "frida_toolkit" / "scripts" / "anti_detect.js").read_text(encoding="utf-8")
check("GLES20.glGetString is hooked", "glGetString.overload" in anti)
check("renderer + vendor answered from the identity",
      "gpuStringFor" in anti and "id.gl_renderer" in anti)
check("GL_EXTENSIONS is spoofed", "0x1F03" in anti and "gl_extensions" in anti)
check("GL limits are spoofed", "glGetIntegerv" in anti and "max_texture_size" in anti)
check("shader precision is spoofed", "glGetShaderPrecisionFormat" in anti)
check("native glGetString is hooked (Unity/Unreal/Flutter)",
      "Interceptor.replace" in anti and "libGLESv2.so" in anti)
check("native strings are cached (no per-call leak)", "glStringCache" in anti)
check("EGL vendor/version/extensions spoofed",
      "eglQueryString" in anti and "0x3053" in anti and "eglVendor" in anti)
check("emulator GL strings are recognised", "SwiftShader" in anti and "Android Emulator" in anti)
check("no hardcoded Adreno 660 fallback", 'return "Adreno (TM) 660"' not in anti)

# ------------------------------------------------------------- surfacing
print("== surfacing (CLI / GUI / doctor) ==")
app_src = (REPO / "gui" / "app.py").read_text(encoding="utf-8")
check("apply-antidetect returns the GPU", '"gpu": gpu or None' in app_src)
check("apply-antidetect message names the GPU", "GPU: {gpu['vendor']}" in app_src)

js = (REPO / "gui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
check("Details shows the GPU", "identity.gl_renderer" in js and "gpu-facts" in js)
check("Details shows EGL vendor + limits", "egl_vendor" in js and "max_texture_size" in js)

css = (REPO / "gui" / "static" / "css" / "style.css").read_text(encoding="utf-8")
check("GPU fact styles present", ".gpu-facts" in css)

doctor_src = (REPO / "core" / "doctor.py").read_text(encoding="utf-8")
check("doctor checks GPU coherence", "def check_gpu_identity" in doctor_src)
check("doctor runs the GPU check", "check_gpu_identity()," in doctor_src)

apply_src = (REPO / "apply_antidetect.py").read_text(encoding="utf-8")
check("standalone apply_antidetect reports the GPU", "gpu_report" in apply_src)

# doctor on real devices
from core.doctor import check_gpu_identity  # noqa: E402
from core.device_manager import DeviceManager  # noqa: E402

DeviceManager().create_device("Samsung Galaxy S24", android_version="14", name="gpudev")
doctor = check_gpu_identity()
check("doctor passes on a freshly created device", doctor["ok"], doctor["detail"])
# The sample device depends on what other suites left in data/devices, so only
# require that a real chip from the table is named (Mali-G720 for this device).
check("doctor names a spoofed chip",
      any(model in doctor["detail"] for model in GPU_MODELS), doctor["detail"])

# The check must be read-only: list_devices() derives liveness and clears a
# recycled pid, which used to make the planted "stale pid" doctor row vanish
# when the GPU check ran before it.
import json as _json  # noqa: E402

_path = DeviceManager().devices_dir / "gpudev.json"
_device = _json.loads(_path.read_text(encoding="utf-8"))
_device["pid"] = os.getpid()          # alive, but no adb serial -> stale
_device["serial"] = "emulator-5991"
before = _path.read_bytes()
check_gpu_identity()
check("GPU check does not rewrite device files", _path.read_bytes() == before)

# Planted contradiction: a known ARM chip claiming a Qualcomm vendor. Both the
# top-level block AND the nested identity must agree, otherwise the (correct)
# fallback to the coherent identity would mask the planted problem.
mismatch = dict(_device)
mismatch["gpu"] = dict(_device["gpu"], gl_renderer="Mali-G715", gl_vendor="Qualcomm",
                       gl_extensions="GL_QCOM_tiled_rendering")
mismatch["identity"] = dict(_device["identity"], gl_renderer="Mali-G715",
                           gl_vendor="Qualcomm", gl_extensions="GL_QCOM_tiled_rendering")
_path.write_text(_json.dumps(mismatch), encoding="utf-8")
bad = check_gpu_identity()
check("doctor flags renderer/vendor mismatch", not bad["ok"], bad["detail"])
check("doctor explains the mismatch", "should be" in bad["detail"] or "vendor" in bad["detail"],
      bad["detail"])
_path.write_text(_json.dumps(_device), encoding="utf-8")

# ------------------------------------------------------- device plumbing
print("== device JSON carries the GPU block ==")
from core.device_manager import DeviceManager as _DM  # noqa: E402

_dev = _DM().get_device("gpudev")
check("device has a gpu block", bool((_dev.get("gpu") or {}).get("gl_renderer")))
check("device gpu block matches the identity",
      _dev["gpu"]["gl_renderer"] == _dev["identity"]["gl_renderer"])
regenerated = _DM().regenerate_identity("gpudev")["device"]
check("new identity refreshes the stored GPU",
      regenerated["gpu"]["gl_renderer"] == regenerated["identity"]["gl_renderer"])
check("regenerated GPU is still coherent",
      regenerated["gpu"]["gl_vendor"]
      == vendor_for_renderer(regenerated["gpu"]["gl_renderer"]))

if failures:
    print(f"\n{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("\nAll GPU spoofing checks passed.")
