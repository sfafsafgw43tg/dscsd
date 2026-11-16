"""
GPU Spoofing - one coherent GPU identity per device, for spoofing + hooks.

A GPU is not one string. Apps and anti-emulator SDKs cross-check several
sources and treat any mismatch as "fake device":

  glGetString(GL_VENDOR / GL_RENDERER / GL_VERSION / GL_EXTENSIONS),
  eglQueryString(EGL_VENDOR), GLES20.glGetIntegerv limits,
  glGetShaderPrecisionFormat, and the driver strings in /vendor/lib*/egl.

So a real Adreno phone must advertise Adreno's extension list and a Qualcomm
driver version - never ARM's Mali shader extensions. ``generate_gpu_identity()``
derives every value from a single ``profiles.gpu_profiles.GPU_MODELS`` entry
(model, vendor, EGL vendor, GL version, extension string, numeric limits),
so nothing can contradict itself.

Deliberately NOT written (it bricks the emulator):
  ``ro.hardware.egl`` / ``ro.hardware.vulkan`` must keep pointing at the
  emulator's own ``libEGL_emulation.so`` / ``vulkan.ranchu.so`` - Android's
  loader uses those values as exact .so filename suffixes, so pointing them at
  "adreno"/"arm" makes RenderThread fail to load a driver and crashes every
  app. ``spoof_gpu_props()`` only writes harmless cosmetic props; the
  vendor/renderer strings are spoofed by
  ``frida_toolkit/scripts/anti_detect.js`` (GLES20 / EGL14 / native libGLESv2)
  using this identity.
"""
import subprocess

import config
from profiles.gpu_profiles import (
    gpu_extension_string,
    gpu_profile,
    vendor_for_renderer,
)
from profiles.profile_generator import generate_gpu_renderer

# Kept for backwards compatibility: ``GPU_PROFILES`` used to live here.
from profiles.gpu_profiles import GPU_MODELS as GPU_PROFILES  # noqa: F401

# Cosmetic props only. NEVER ro.hardware.egl / ro.hardware.vulkan (see above).
_GPU_COSMETIC_PROPS = {
    "debug.egl.hw": "1",
    "debug.sf.hw": "1",
    "debug.hwui.renderer": "skiagl",
}


class GPUSpoofer:
    """Build (and optionally apply) a coherent GPU identity."""

    def __init__(self, device_serial="emulator-5554"):
        self.device_serial = device_serial
        self.adb = config.ADB_PATH

    def _adb(self, command):
        """Execute ADB command"""
        cmd = [self.adb, "-s", self.device_serial] + command
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return result.stdout.strip()
        except Exception as e:
            print(f"ADB error: {e}")
            return None

    def get_gpu_profile(self, brand, model_name):
        """Renderer string for this brand/model."""
        return generate_gpu_renderer(brand, model_name)

    def generate_gpu_identity(self, brand, model_name, renderer=None):
        """Complete, internally consistent GPU identity for this device.

        *renderer* forces a chip (used when the saved identity already has one,
        so re-applying spoofing never changes what apps already saw).
        """
        renderer = renderer or self.get_gpu_profile(brand, model_name)
        profile = gpu_profile(renderer)
        limits = profile["limits"]
        return {
            "gl_renderer": profile["model"],
            "gl_vendor": profile["vendor"],
            "gl_version": f"OpenGL ES 3.2 {profile['driver']}",
            "egl_vendor": profile["egl_vendor"],
            "gl_extensions": gpu_extension_string(profile["model"]),
            # Numeric limits: apps compare these with the renderer name to spot
            # emulators (a phone never reports 8192 for an Adreno 750).
            "gl_limits": {
                "max_texture_size": limits["texture"],
                "max_viewport_dims": [limits["viewport"], limits["viewport"]],
                "max_vertex_attribs": limits["attribs"],
                "max_texture_units": limits["texture_units"],
                "max_renderbuffer_size": limits["renderbuffer"],
                "max_combined_texture_units": limits["combined_units"],
                "glsl_version": limits["glsl"],
            },
            # Plain-language facts for the dashboard / doctor / CLI.
            "gpu_name": profile["model"],
            "gpu_vendor": profile["vendor"],
            "gpu_hw": profile["hw"],
            "gpu_score": profile["score"],
            "gpu_year": profile["year"],
        }

    def spoof_gpu_props(self, brand, model_name):
        """Apply only safe, cosmetic GPU props and return the identity.

        NEVER writes ``ro.hardware.egl`` / ``ro.hardware.vulkan``: Android's
        native loader uses those as .so suffixes (libEGL_<val>.so /
        vulkan.<val>.so), so a wrong value crashes RenderThread in every app.
        """
        identity = self.generate_gpu_identity(brand, model_name)
        for key, value in _GPU_COSMETIC_PROPS.items():
            self._adb(["shell", "setprop", key, value])
        return identity


def build_gpu_identity(identity):
    """GPU identity for a saved device (identity dict or device JSON).

    Prefers the fully generated, coherent block saved on the device
    (``device["gpu"]`` / ``identity["gpu"]``) so re-applying spoofing or
    regenerating hooks can never change the GPU apps already saw. Falls back to
    the saved renderer, then to the device model - never randomises.
    """
    identity = identity or {}
    # Precedence: the device's own coherent block first (that is what the hooks
    # reported), then a nested identity, then the bare renderer.
    saved = identity.get("gpu")
    if isinstance(saved, dict) and saved.get("gl_renderer") and saved.get("gl_extensions"):
        return dict(saved)
    saved_identity = identity.get("identity")
    if isinstance(saved_identity, dict):
        nested = saved_identity.get("gpu")
        if isinstance(nested, dict) and nested.get("gl_renderer"):
            return dict(nested)
        if saved_identity.get("gl_renderer"):
            return GPUSpoofer().generate_gpu_identity(
                saved_identity.get("brand", "Samsung"),
                saved_identity.get("model", ""),
                renderer=saved_identity["gl_renderer"],
            )
    brand = identity.get("brand", "Samsung")
    model_name = identity.get("model", "")
    renderer = identity.get("gl_renderer") or None
    return GPUSpoofer().generate_gpu_identity(brand, model_name, renderer=renderer)


def get_gpu_for_device(brand, model_name):
    """Convenience function to get GPU data"""
    spoofer = GPUSpoofer()
    return spoofer.generate_gpu_identity(brand, model_name)
