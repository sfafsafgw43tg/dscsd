"""
GPU model database - one coherent identity per chip.

Shared by ``profiles.profile_generator`` (device identity) and
``spoof.gpu_spoof`` (spoofing / Frida payload) so a device never reports a
renderer from one chip together with another chip's vendor, driver version or
extension list - that mismatch is itself an emulator tell.

Values: renderer -> vendor, GL driver version, EGL vendor, vendor-specific
shader extensions, extra extensions, GL numeric limits and a rough score.
"""
# ---------------------------------------------------------------------------
# GPU model database
# ---------------------------------------------------------------------------
# driver: the real vendor driver-version prefix used in GL_VERSION strings.
# shader: vendor-specific shader-binary / tile-rendering extensions.
# extras: further plausible extensions for that chip.
# limits: GL_MAX_TEXTURE_SIZE, GL_MAX_VIEWPORT_DIMS, GL_MAX_VERTEX_ATTRIBS,
#         GL_MAX_TEXTURE_IMAGE_UNITS, GL_MAX_RENDERBUFFER_SIZE,
#         GL_MAX_COMBINED_TEXTURE_IMAGE_UNITS, glsl version.
GPU_MODELS = {
    "Adreno (TM) 750": {
        "vendor": "Qualcomm", "egl_vendor": "Qualcomm",
        "driver": "V@0800.0", "hw": "adreno",
        "shader": ["GL_QCOM_tiled_rendering", "GL_QCOM_alpha_test",
                   "GL_EXT_disjoint_timer_query"],
        "extras": ["GL_KHR_debug", "GL_EXT_texture_filter_anisotropic",
                   "GL_EXT_sRGB", "GL_QCOM_shader_framebuffer_fetch_noncoherent"],
        "limits": {"texture": 16384, "viewport": 16384, "attribs": 32,
                   "texture_units": 16, "renderbuffer": 16384,
                   "combined_units": 96, "glsl": "OpenGL ES GLSL ES 3.20"},
        "score": 92, "year": 2023,
    },
    "Adreno (TM) 740": {
        "vendor": "Qualcomm", "egl_vendor": "Qualcomm",
        "driver": "V@0738.0", "hw": "adreno",
        "shader": ["GL_QCOM_tiled_rendering", "GL_QCOM_alpha_test",
                   "GL_EXT_disjoint_timer_query"],
        "extras": ["GL_KHR_debug", "GL_EXT_texture_filter_anisotropic",
                   "GL_EXT_sRGB"],
        "limits": {"texture": 16384, "viewport": 16384, "attribs": 32,
                   "texture_units": 16, "renderbuffer": 16384,
                   "combined_units": 96, "glsl": "OpenGL ES GLSL ES 3.20"},
        "score": 90, "year": 2022,
    },
    "Adreno (TM) 730": {
        "vendor": "Qualcomm", "egl_vendor": "Qualcomm",
        "driver": "V@0715.0", "hw": "adreno",
        "shader": ["GL_QCOM_tiled_rendering", "GL_QCOM_alpha_test",
                   "GL_EXT_disjoint_timer_query"],
        "extras": ["GL_KHR_debug", "GL_EXT_texture_filter_anisotropic"],
        "limits": {"texture": 16384, "viewport": 16384, "attribs": 32,
                   "texture_units": 16, "renderbuffer": 16384,
                   "combined_units": 96, "glsl": "OpenGL ES GLSL ES 3.20"},
        "score": 88, "year": 2022,
    },
    "Adreno (TM) 660": {
        "vendor": "Qualcomm", "egl_vendor": "Qualcomm",
        "driver": "V@0615.0", "hw": "adreno",
        "shader": ["GL_QCOM_tiled_rendering", "GL_QCOM_alpha_test",
                   "GL_EXT_disjoint_timer_query"],
        "extras": ["GL_KHR_debug", "GL_EXT_texture_filter_anisotropic"],
        "limits": {"texture": 16384, "viewport": 16384, "attribs": 32,
                   "texture_units": 16, "renderbuffer": 16384,
                   "combined_units": 96, "glsl": "OpenGL ES GLSL ES 3.20"},
        "score": 70, "year": 2019,
    },
    "Mali-G720": {
        "vendor": "ARM", "egl_vendor": "ARM",
        "driver": "v1.r43p0", "hw": "mali",
        "shader": ["GL_ARM_rgba8", "GL_ARM_mali_shader_binary",
                   "GL_ARM_mali_program_binary"],
        "extras": ["GL_KHR_debug", "GL_EXT_texture_filter_anisotropic",
                   "GL_EXT_sRGB", "GL_EXT_multisampled_render_to_texture"],
        "limits": {"texture": 16384, "viewport": 16384, "attribs": 32,
                   "texture_units": 16, "renderbuffer": 16384,
                   "combined_units": 96, "glsl": "OpenGL ES GLSL ES 3.20"},
        "score": 93, "year": 2023,
    },
    "Mali-G715": {
        "vendor": "ARM", "egl_vendor": "ARM",
        "driver": "v1.r40p0", "hw": "mali",
        "shader": ["GL_ARM_rgba8", "GL_ARM_mali_shader_binary",
                   "GL_ARM_mali_program_binary"],
        "extras": ["GL_KHR_debug", "GL_EXT_texture_filter_anisotropic",
                   "GL_EXT_multisampled_render_to_texture"],
        "limits": {"texture": 16384, "viewport": 16384, "attribs": 32,
                   "texture_units": 16, "renderbuffer": 16384,
                   "combined_units": 96, "glsl": "OpenGL ES GLSL ES 3.20"},
        "score": 91, "year": 2022,
    },
    "Mali-G710": {
        "vendor": "ARM", "egl_vendor": "ARM",
        "driver": "v1.r38p0", "hw": "mali",
        "shader": ["GL_ARM_rgba8", "GL_ARM_mali_shader_binary",
                   "GL_ARM_mali_program_binary"],
        "extras": ["GL_KHR_debug", "GL_EXT_texture_filter_anisotropic",
                   "GL_EXT_multisampled_render_to_texture"],
        "limits": {"texture": 16384, "viewport": 16384, "attribs": 32,
                   "texture_units": 16, "renderbuffer": 16384,
                   "combined_units": 96, "glsl": "OpenGL ES GLSL ES 3.20"},
        "score": 85, "year": 2021,
    },
    "Mali-G610 MC6": {
        "vendor": "ARM", "egl_vendor": "ARM",
        "driver": "v1.r38p0", "hw": "mali",
        "shader": ["GL_ARM_rgba8", "GL_ARM_mali_shader_binary",
                   "GL_ARM_mali_program_binary"],
        "extras": ["GL_KHR_debug", "GL_EXT_texture_filter_anisotropic"],
        "limits": {"texture": 16384, "viewport": 16384, "attribs": 32,
                   "texture_units": 16, "renderbuffer": 16384,
                   "combined_units": 96, "glsl": "OpenGL ES GLSL ES 3.20"},
        "score": 66, "year": 2021,
    },
    "Mali-G68": {
        "vendor": "ARM", "egl_vendor": "ARM",
        "driver": "v1.r38p0", "hw": "mali",
        "shader": ["GL_ARM_rgba8", "GL_ARM_mali_shader_binary"],
        "extras": ["GL_KHR_debug", "GL_EXT_texture_filter_anisotropic"],
        "limits": {"texture": 16384, "viewport": 16384, "attribs": 32,
                   "texture_units": 16, "renderbuffer": 16384,
                   "combined_units": 96, "glsl": "OpenGL ES GLSL ES 3.20"},
        "score": 60, "year": 2021,
    },
    "Xclipse 940": {
        "vendor": "Samsung", "egl_vendor": "Samsung",
        "driver": "V@0800.0", "hw": "xclipse",
        "shader": ["GL_EXT_disjoint_timer_query", "GL_EXT_texture_filter_anisotropic"],
        "extras": ["GL_KHR_debug", "GL_EXT_sRGB",
                   "GL_EXT_multisampled_render_to_texture"],
        "limits": {"texture": 16384, "viewport": 16384, "attribs": 32,
                   "texture_units": 16, "renderbuffer": 16384,
                   "combined_units": 96, "glsl": "OpenGL ES GLSL ES 3.20"},
        "score": 88, "year": 2023,
    },
    "Xclipse 920": {
        "vendor": "Samsung", "egl_vendor": "Samsung",
        "driver": "V@0635.0", "hw": "xclipse",
        "shader": ["GL_EXT_disjoint_timer_query", "GL_EXT_texture_filter_anisotropic"],
        "extras": ["GL_KHR_debug", "GL_EXT_sRGB"],
        "limits": {"texture": 16384, "viewport": 16384, "attribs": 32,
                   "texture_units": 16, "renderbuffer": 16384,
                   "combined_units": 96, "glsl": "OpenGL ES GLSL ES 3.20"},
        "score": 82, "year": 2022,
    },
    # ---- Chips the model table actually needs --------------------------------
    # Without these, a Galaxy A14 / A34 / A54 (Mali-G68), a Pixel (Mali-G710 /
    # G715 / G720 and Immortalis) or a Redmi Note on Mali-G57/G610 silently
    # fell back to a random Adreno, so the spoofed phone reported a chip its
    # SoC does not have - an instant fake-device tell.
    "Mali-G720 MC12": {
        "vendor": "ARM", "egl_vendor": "ARM",
        "driver": "v1.r43p0", "hw": "mali",
        "shader": ["GL_ARM_rgba8", "GL_ARM_mali_shader_binary",
                   "GL_ARM_mali_program_binary", "GL_EXT_shader_pixel_local_storage"],
        "extras": ["GL_KHR_debug", "GL_KHR_blend_equation_advanced",
                   "GL_EXT_texture_filter_anisotropic", "GL_EXT_sRGB",
                   "GL_EXT_multisampled_render_to_texture"],
        "limits": {"texture": 16384, "viewport": 16384, "attribs": 32,
                   "texture_units": 16, "renderbuffer": 16384,
                   "combined_units": 96, "glsl": "OpenGL ES GLSL ES 3.20"},
        "score": 90, "year": 2023,
    },
    "Mali-G715 MC10": {
        "vendor": "ARM", "egl_vendor": "ARM",
        "driver": "v1.r43p0", "hw": "mali",
        "shader": ["GL_ARM_rgba8", "GL_ARM_mali_shader_binary",
                   "GL_ARM_mali_program_binary"],
        "extras": ["GL_KHR_debug", "GL_EXT_texture_filter_anisotropic",
                   "GL_EXT_sRGB", "GL_EXT_multisampled_render_to_texture"],
        "limits": {"texture": 16384, "viewport": 16384, "attribs": 32,
                   "texture_units": 16, "renderbuffer": 16384,
                   "combined_units": 96, "glsl": "OpenGL ES GLSL ES 3.20"},
        "score": 84, "year": 2022,
    },
    "Mali-G710 MC10": {
        "vendor": "ARM", "egl_vendor": "ARM",
        "driver": "v1.r40p0", "hw": "mali",
        "shader": ["GL_ARM_rgba8", "GL_ARM_mali_shader_binary",
                   "GL_ARM_mali_program_binary"],
        "extras": ["GL_KHR_debug", "GL_EXT_texture_filter_anisotropic",
                   "GL_EXT_sRGB"],
        "limits": {"texture": 16384, "viewport": 16384, "attribs": 32,
                   "texture_units": 16, "renderbuffer": 16384,
                   "combined_units": 96, "glsl": "OpenGL ES GLSL ES 3.20"},
        "score": 80, "year": 2021,
    },
    "Mali-G78 MP20": {
        "vendor": "ARM", "egl_vendor": "ARM",
        "driver": "v1.r38p0", "hw": "mali",
        "shader": ["GL_ARM_rgba8", "GL_ARM_mali_shader_binary",
                   "GL_ARM_mali_program_binary"],
        "extras": ["GL_KHR_debug", "GL_EXT_texture_filter_anisotropic"],
        "limits": {"texture": 16384, "viewport": 16384, "attribs": 32,
                   "texture_units": 16, "renderbuffer": 16384,
                   "combined_units": 96, "glsl": "OpenGL ES GLSL ES 3.20"},
        "score": 74, "year": 2020,
    },
    "Mali-G68 MC4": {
        "vendor": "ARM", "egl_vendor": "ARM",
        "driver": "v1.r38p0", "hw": "mali",
        "shader": ["GL_ARM_rgba8", "GL_ARM_mali_shader_binary",
                   "GL_ARM_mali_program_binary"],
        "extras": ["GL_KHR_debug", "GL_EXT_texture_filter_anisotropic"],
        "limits": {"texture": 16384, "viewport": 16384, "attribs": 32,
                   "texture_units": 16, "renderbuffer": 16384,
                   "combined_units": 96, "glsl": "OpenGL ES GLSL ES 3.20"},
        "score": 62, "year": 2020,
    },
    "Mali-G68 MP5": {
        "vendor": "ARM", "egl_vendor": "ARM",
        "driver": "v1.r38p0", "hw": "mali",
        "shader": ["GL_ARM_rgba8", "GL_ARM_mali_shader_binary",
                   "GL_ARM_mali_program_binary"],
        "extras": ["GL_KHR_debug", "GL_EXT_texture_filter_anisotropic"],
        "limits": {"texture": 16384, "viewport": 16384, "attribs": 32,
                   "texture_units": 16, "renderbuffer": 16384,
                   "combined_units": 96, "glsl": "OpenGL ES GLSL ES 3.20"},
        "score": 63, "year": 2020,
    },
    "Mali-G68 MP4": {
        "vendor": "ARM", "egl_vendor": "ARM",
        "driver": "v1.r38p0", "hw": "mali",
        "shader": ["GL_ARM_rgba8", "GL_ARM_mali_shader_binary",
                   "GL_ARM_mali_program_binary"],
        "extras": ["GL_KHR_debug", "GL_EXT_texture_filter_anisotropic"],
        "limits": {"texture": 16384, "viewport": 16384, "attribs": 32,
                   "texture_units": 16, "renderbuffer": 16384,
                   "combined_units": 96, "glsl": "OpenGL ES GLSL ES 3.20"},
        "score": 60, "year": 2020,
    },
    "Mali-G68 MP2": {
        "vendor": "ARM", "egl_vendor": "ARM",
        "driver": "v1.r38p0", "hw": "mali",
        "shader": ["GL_ARM_rgba8", "GL_ARM_mali_shader_binary"],
        "extras": ["GL_KHR_debug", "GL_EXT_texture_filter_anisotropic"],
        "limits": {"texture": 16384, "viewport": 16384, "attribs": 32,
                   "texture_units": 16, "renderbuffer": 16384,
                   "combined_units": 96, "glsl": "OpenGL ES GLSL ES 3.20"},
        "score": 55, "year": 2020,
    },
    "Mali-G57 MC2": {
        "vendor": "ARM", "egl_vendor": "ARM",
        "driver": "v1.r38p0", "hw": "mali",
        "shader": ["GL_ARM_rgba8", "GL_ARM_mali_shader_binary"],
        "extras": ["GL_KHR_debug", "GL_EXT_texture_filter_anisotropic"],
        "limits": {"texture": 16384, "viewport": 16384, "attribs": 32,
                   "texture_units": 16, "renderbuffer": 16384,
                   "combined_units": 96, "glsl": "OpenGL ES GLSL ES 3.20"},
        "score": 48, "year": 2020,
    },
    "Mali-G610 MC4": {
        "vendor": "ARM", "egl_vendor": "ARM",
        "driver": "v1.r38p0", "hw": "mali",
        "shader": ["GL_ARM_rgba8", "GL_ARM_mali_shader_binary",
                   "GL_ARM_mali_program_binary"],
        "extras": ["GL_KHR_debug", "GL_EXT_texture_filter_anisotropic"],
        "limits": {"texture": 16384, "viewport": 16384, "attribs": 32,
                   "texture_units": 16, "renderbuffer": 16384,
                   "combined_units": 96, "glsl": "OpenGL ES GLSL ES 3.20"},
        "score": 64, "year": 2021,
    },
    "Immortalis-G715 MC11": {
        "vendor": "ARM", "egl_vendor": "ARM",
        "driver": "v1.r43p0", "hw": "mali",
        "shader": ["GL_ARM_rgba8", "GL_ARM_mali_shader_binary",
                   "GL_ARM_mali_program_binary"],
        "extras": ["GL_KHR_debug", "GL_EXT_texture_filter_anisotropic",
                   "GL_EXT_sRGB"],
        "limits": {"texture": 16384, "viewport": 16384, "attribs": 32,
                   "texture_units": 16, "renderbuffer": 16384,
                   "combined_units": 96, "glsl": "OpenGL ES GLSL ES 3.20"},
        "score": 86, "year": 2022,
    },
    "Maleoon 910 MP4": {
        "vendor": "Huawei", "egl_vendor": "Huawei",
        "driver": "OpenGL ES 3.2 V@0800.0", "hw": "maleoon",
        "shader": ["GL_EXT_disjoint_timer_query", "GL_EXT_texture_filter_anisotropic"],
        "extras": ["GL_KHR_debug", "GL_EXT_sRGB"],
        "limits": {"texture": 16384, "viewport": 16384, "attribs": 32,
                   "texture_units": 16, "renderbuffer": 16384,
                   "combined_units": 96, "glsl": "OpenGL ES GLSL ES 3.20"},
        "score": 70, "year": 2023,
    },
    "Adreno (TM) 720": {
        "vendor": "Qualcomm", "egl_vendor": "Qualcomm",
        "driver": "V@0738.0", "hw": "adreno",
        "shader": ["GL_QCOM_tiled_rendering", "GL_QCOM_alpha_test",
                   "GL_EXT_disjoint_timer_query"],
        "extras": ["GL_KHR_debug", "GL_EXT_texture_filter_anisotropic",
                   "GL_EXT_sRGB"],
        "limits": {"texture": 16384, "viewport": 16384, "attribs": 32,
                   "texture_units": 16, "renderbuffer": 16384,
                   "combined_units": 96, "glsl": "OpenGL ES GLSL ES 3.20"},
        "score": 72, "year": 2023,
    },
    "Adreno (TM) 725": {
        "vendor": "Qualcomm", "egl_vendor": "Qualcomm",
        "driver": "V@0738.0", "hw": "adreno",
        "shader": ["GL_QCOM_tiled_rendering", "GL_QCOM_alpha_test",
                   "GL_EXT_disjoint_timer_query"],
        "extras": ["GL_KHR_debug", "GL_EXT_texture_filter_anisotropic",
                   "GL_EXT_sRGB"],
        "limits": {"texture": 16384, "viewport": 16384, "attribs": 32,
                   "texture_units": 16, "renderbuffer": 16384,
                   "combined_units": 96, "glsl": "OpenGL ES GLSL ES 3.20"},
        "score": 76, "year": 2023,
    },
    "Adreno (TM) 642L": {
        "vendor": "Qualcomm", "egl_vendor": "Qualcomm",
        "driver": "V@0615.0", "hw": "adreno",
        "shader": ["GL_QCOM_tiled_rendering", "GL_QCOM_alpha_test"],
        "extras": ["GL_KHR_debug", "GL_EXT_texture_filter_anisotropic"],
        "limits": {"texture": 16384, "viewport": 16384, "attribs": 32,
                   "texture_units": 16, "renderbuffer": 16384,
                   "combined_units": 96, "glsl": "OpenGL ES GLSL ES 3.20"},
        "score": 58, "year": 2021,
    },
}

# Extensions a real phone always advertises, whatever the GPU.
_BASE_EXTENSIONS = [
    "GL_OES_EGL_image", "GL_OES_EGL_image_external", "GL_OES_depth_texture",
    "GL_OES_packed_depth_stencil", "GL_OES_rgb8_rgba8",
    "GL_EXT_texture_format_BGRA8888", "GL_OES_texture_float",
    "GL_OES_texture_half_float", "GL_EXT_texture_rg",
    "GL_EXT_shader_texture_lod", "GL_OES_vertex_array_object",
]

# Cosmetic props. NEVER ro.hardware.egl / ro.hardware.vulkan (see module doc).
_GPU_COSMETIC_PROPS = {
    "debug.egl.hw": "1",
    "debug.sf.hw": "1",
    "debug.hwui.renderer": "skiagl",
}


def gpu_extension_string(model):
    """Full, vendor-consistent GL_EXTENSIONS string for *model*."""
    profile = _lookup(model)
    parts = list(_BASE_EXTENSIONS) + list(profile["shader"]) + list(profile["extras"])
    # Stable order, no duplicates (a real driver sorts its extension string).
    seen, out = set(), []
    for ext in sorted(parts):
        if ext not in seen:
            seen.add(ext)
            out.append(ext)
    return " ".join(out)




def normalize(model):
    """Normalise a renderer string for matching ("Adreno (TM) 740" == "Adreno 740")."""
    return (model or "").strip().replace("(TM) ", "").replace("(TM)", "").replace(" ", "").lower()


def vendor_for_renderer(renderer):
    """Vendor implied by a renderer name (Adreno->Qualcomm, Mali->ARM, ...)."""
    name = renderer or ""
    lowered = name.lower()
    if "adreno" in lowered or "qualcomm" in lowered:
        return "Qualcomm"
    if "mali" in lowered or lowered.startswith("arm"):
        return "ARM"
    if "xclipse" in lowered:
        return "Samsung"
    if "powervr" in lowered or "imagination" in lowered:
        return "Imagination"
    if "immortalis" in lowered:
        return "ARM"
    if "maleoon" in lowered or "kirin" in lowered:
        return "Huawei"
    return "Qualcomm"


def gpu_profile(model):
    """Profile for a renderer string, matched exactly then loosely.

    Every returned profile carries ``matched``: True when the renderer is a
    known chip, False when it was "unknown" and inherited a vendor profile.
    Identity checks use that to spot a renderer/vendor mismatch instead of
    silently accepting whatever vendor the unknown name happens to imply.
    """
    model = (model or "").strip()
    if model in GPU_MODELS:
        return dict(GPU_MODELS[model], model=model, matched=True)
    norm = normalize(model)
    for name, profile in GPU_MODELS.items():
        if normalize(name) == norm:
            return dict(profile, model=name, matched=True)
    # Unknown chip: inherit the flagship profile of the vendor the name
    # points at, so the answers stay self-consistent.
    vendor = vendor_for_renderer(model)
    best = None
    for name, profile in GPU_MODELS.items():
        if profile["vendor"] != vendor:
            continue
        if best is None or profile["score"] > GPU_MODELS[best]["score"]:
            best = name
    if best:
        return dict(GPU_MODELS[best], model=model, matched=False)
    return dict(GPU_MODELS["Adreno (TM) 740"], model=model or "Adreno (TM) 740",
                matched=False)


def gpu_extension_string(model):
    """Full, vendor-consistent GL_EXTENSIONS string for *model*."""
    profile = gpu_profile(model)
    parts = list(_BASE_EXTENSIONS) + list(profile["shader"]) + list(profile["extras"])
    # Stable order, no duplicates (a real driver sorts its extension string).
    seen, out = set(), []
    for ext in sorted(parts):
        if ext not in seen:
            seen.add(ext)
            out.append(ext)
    return " ".join(out)
