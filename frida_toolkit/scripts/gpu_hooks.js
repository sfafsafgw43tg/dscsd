/**
 * GPU Hooks - GL/EGL vendor + renderer spoofing for Chromium GPU processes.
 *
 * Why a separate script: Chrome-based apps compute the WebGL fingerprint
 * (`UNMASKED_VENDOR_WEBGL` / `UNMASKED_RENDERER_WEBGL`, i.e. the "gpu:" row of
 * any fingerprint report) inside a sandboxed renderer process
 * (`<package>:privileged_process0`), NOT in the app's main process. The
 * combined anti-detect script is attached to the main process, and
 * `native_hooks.js` intentionally bails out in sandboxed/system processes, so
 * that process kept answering with the real host GPU:
 *
 *   Google (ATI Technologies Inc.)
 *   Android Emulator OpenGL ES Translator (AMD Radeon RX 6700 XT)
 *
 * This script only touches graphics entry points, so it is safe to load in a
 * renderer process:
 *  - Java: `GLES20.glGetString` / `glGetIntegerv` / `glGetShaderPrecisionFormat`
 *    and `EGL14.eglQueryString` (used by apps and WebView-based code).
 *  - Native: `glGetString`, `glGetStringi` and `eglQueryString` in
 *    libGLESv2 / libGLESv1_CM / libGLESv3 / libEGL (what Chromium's GPU
 *    process actually calls).
 *
 * Every answer comes from `deviceIdentity` (one coherent chip), and strings
 * are returned unconditionally - filtering on "looks emulator-ish" is what
 * let a passthrough host GPU through. No libc/Java identity hooks, no file
 * hiding: nothing here can destabilise a renderer sandbox.
 */

(function () {
    "use strict";

    var id = (typeof deviceIdentity !== "undefined" && deviceIdentity) ? deviceIdentity : {};
    var glRenderer = id.gl_renderer || id.glRenderer || "Adreno (TM) 740";
    var glVendor = id.gl_vendor || id.glVendor || "Qualcomm";
    var glVersion = id.gl_version || "OpenGL ES 3.2 V@0738.0";
    var eglVendor = id.egl_vendor || glVendor;
    var glExtensions = id.gl_extensions || "";
    var glLimits = id.gl_limits || {};

    // GL enums. 0x1F00 GL_VENDOR, 0x1F01 GL_RENDERER, 0x1F02 GL_VERSION,
    // 0x1F03 GL_EXTENSIONS, 0x1F04 GL_SHADING_LANGUAGE_VERSION.
    function stringFor(name) {
        if (name === 0x1F00) return glVendor;
        if (name === 0x1F01) return glRenderer;
        if (name === 0x1F02) return glVersion;
        if (name === 0x1F03) return glExtensions;
        if (name === 0x1F04) return (glLimits.glsl_version || "OpenGL ES GLSL ES 3.20");
        return null;
    }

    // EGL enums. 0x3053 EGL_VENDOR, 0x3054 EGL_VERSION, 0x3055 EGL_EXTENSIONS.
    function eglStringFor(name) {
        if (name === 0x3053) return eglVendor;
        if (name === 0x3054) return "1.4 " + eglVendor + " " + glVersion.replace("OpenGL ES ", "");
        if (name === 0x3055) {
            return "EGL_KHR_image_base EGL_KHR_gl_texture_2D_image " +
                   "EGL_ANDROID_image_native_buffer EGL_ANDROID_recordable";
        }
        return null;
    }

    // System properties that carry the *host* GPU (Chromium and several
    // fingerprinting SDKs read them without touching GL):
    //   ro.hardware.egl / ro.hardware.vulkan stay untouched on purpose -
    //   they are .so filename suffixes and changing them crashes RenderThread.
    function propFor(key) {
        if (!key) return null;
        if (key === "ro.opengles.version") return "196610";      // GLES 3.2
        if (key === "ro.hardware.egl") return null;              // must NOT change
        if (key === "ro.hardware.vulkan") return null;           // must NOT change
        if (key === "ro.gfx.driver.0" || key === "ro.gfx.driver.1") return "";
        if (key === "debug.hwui.renderer") return "skiagl";
        return null;
    }

    // ---------------------------------------------------------------
    // Native GL/EGL (the path Chromium's GPU process uses)
    // ---------------------------------------------------------------
    function findExport(libNames, exportName) {
        var addr = null;
        try {
            if (typeof Process !== "undefined" && Process.findModuleByName) {
                for (var i = 0; i < libNames.length && !addr; i++) {
                    var mod = Process.findModuleByName(libNames[i]);
                    if (mod && mod.findExportByName) {
                        addr = mod.findExportByName(exportName);
                    }
                }
            }
        } catch (e) { }
        if (!addr) {
            try {
                if (typeof Module !== "undefined" && Module.findExportByName) {
                    addr = Module.findExportByName(null, exportName);
                }
            } catch (e) { }
        }
        return addr;
    }

    var stringCache = {};

    function cachedString(key, value) {
        // glGetString is called from render loops: allocate once.
        if (!stringCache[key] || stringCache[key].value !== value) {
            stringCache[key] = { value: value, ptr: Memory.allocUtf8String(String(value)) };
        }
        return stringCache[key].ptr;
    }

    var GL_LIBS = ["libGLESv2.so", "libGLESv1_CM.so", "libGLESv3.so"];

    try {
        var glGetStringAddr = findExport(GL_LIBS, "glGetString");
        if (glGetStringAddr && typeof Interceptor !== "undefined") {
            var origGlGetString = new NativeFunction(glGetStringAddr, "pointer", ["uint"]);
            Interceptor.replace(glGetStringAddr, new NativeCallback(function (name) {
                var spoof = null;
                try { spoof = stringFor(name); } catch (e) { spoof = null; }
                if (spoof) {
                    try { return cachedString("s" + name, spoof); } catch (e) { }
                }
                return origGlGetString(name);
            }, "pointer", ["uint"]));
            console.log("[GPU] native glGetString spoofed (" + glVendor + " / " + glRenderer + ")");
        }
    } catch (e) {
        console.log("[GPU] glGetString hook skipped: " + e);
    }

    // glGetStringi(GL_EXTENSIONS, index) - the GLES3+ way browsers enumerate
    // extensions. Strings are split on spaces so indices stay stable.
    try {
        var glGetStringiAddr = findExport(GL_LIBS, "glGetStringi");
        if (glGetStringiAddr && glExtensions) {
            var extList = glExtensions.split(" ");
            var origGlGetStringi = new NativeFunction(glGetStringiAddr, "pointer", ["uint", "uint"]);
            Interceptor.replace(glGetStringiAddr, new NativeCallback(function (name, index) {
                try {
                    if (name === 0x1F03) {
                        if (index >= extList.length) return ptr(0);
                        return cachedString("e" + index, extList[index]);
                    }
                } catch (e) { }
                return origGlGetStringi(name, index);
            }, "pointer", ["uint", "uint"]));
            console.log("[GPU] glGetStringi spoofed (" + extList.length + " extensions)");
        }
    } catch (e) {
        console.log("[GPU] glGetStringi hook skipped: " + e);
    }

    // glGetIntegerv: numeric limits are cross-checked against the renderer
    // name by anti-emulator SDKs (an Adreno 750 answering SWIFTSHADER's 8192
    // is a contradiction). Answered from the same chip profile.
    try {
        var glGetIntegervAddr = findExport(GL_LIBS, "glGetIntegerv");
        if (glGetIntegervAddr && typeof Interceptor !== "undefined") {
            var origGlGetIntegerv = new NativeFunction(glGetIntegervAddr, "void",
                                                       ["uint", "pointer"]);
            var limitFor = function (pname) {
                if (pname === 0x0D33) return glLimits.max_texture_size;              // GL_MAX_TEXTURE_SIZE
                if (pname === 0x8869) return glLimits.max_vertex_attribs;            // GL_MAX_VERTEX_ATTRIBS
                if (pname === 0x8872) return glLimits.max_texture_units;             // GL_MAX_TEXTURE_IMAGE_UNITS
                if (pname === 0x84E8) return glLimits.max_renderbuffer_size;         // GL_MAX_RENDERBUFFER_SIZE
                if (pname === 0x8B4D) return glLimits.max_combined_texture_units;    // GL_MAX_COMBINED_TEXTURE_IMAGE_UNITS
                return null;
            };
            Interceptor.replace(glGetIntegervAddr, new NativeCallback(function (pname, params) {
                try {
                    if (pname === 0x0D3A && glLimits.max_viewport_dims && params) {
                        params.writeInt(glLimits.max_viewport_dims[0]);
                        params.add(4).writeInt(glLimits.max_viewport_dims[1]);
                        return;
                    }
                    var value = limitFor(pname);
                    if (value !== null && value !== undefined && params) {
                        params.writeInt(value);
                        return;
                    }
                } catch (e) { }
                return origGlGetIntegerv(pname, params);
            }, "void", ["uint", "pointer"]));
            console.log("[GPU] native glGetIntegerv limits spoofed");
        }
    } catch (e) {
        console.log("[GPU] glGetIntegerv hook skipped: " + e);
    }

    // glGetShaderPrecisionFormat: software renderers report a different
    // precision/range triple than a real mobile GPU.
    try {
        var precisionAddr = findExport(GL_LIBS, "glGetShaderPrecisionFormat");
        if (precisionAddr && typeof Interceptor !== "undefined") {
            var origPrecision = new NativeFunction(precisionAddr, "void",
                                                  ["uint", "uint", "pointer"]);
            Interceptor.replace(precisionAddr, new NativeCallback(
                function (shaderType, precisionType, range) {
                    try {
                        if (range) {
                            range.writeInt(23);          // rangeMin
                            range.add(4).writeInt(23);   // rangeMax
                            range.add(8).writeInt(23);   // precision
                            return;
                        }
                    } catch (e) { }
                    return origPrecision(shaderType, precisionType, range);
                }, "void", ["uint", "uint", "pointer"]));
            console.log("[GPU] glGetShaderPrecisionFormat spoofed");
        }
    } catch (e) {
        console.log("[GPU] glGetShaderPrecisionFormat hook skipped: " + e);
    }

    try {
        var eglQueryStringAddr = findExport(["libEGL.so"], "eglQueryString");
        if (eglQueryStringAddr && typeof Interceptor !== "undefined") {
            var origEglQueryString = new NativeFunction(
                eglQueryStringAddr, "pointer", ["pointer", "int"]);
            Interceptor.replace(eglQueryStringAddr, new NativeCallback(function (display, name) {
                var spoof = null;
                try { spoof = eglStringFor(name); } catch (e) { spoof = null; }
                if (spoof) {
                    try { return cachedString("q" + name, spoof); } catch (e) { }
                }
                return origEglQueryString(display, name);
            }, "pointer", ["pointer", "int"]));
            console.log("[GPU] native eglQueryString spoofed (" + eglVendor + ")");
        }
    } catch (e) {
        console.log("[GPU] eglQueryString hook skipped: " + e);
    }

    // ---------------------------------------------------------------
    // Java GLES/EGL (WebView + apps that use android.opengl)
    // ---------------------------------------------------------------
    function installJavaHooks() {
        if (typeof Java === "undefined" || !Java.available) return;

        Java.perform(function () {
            try {
                var GLES20 = Java.use("android.opengl.GLES20");
                var origJavaGetString = GLES20.glGetString.overload("int");
                origJavaGetString.implementation = function (name) {
                    var spoof = null;
                    try { spoof = stringFor(name); } catch (e) { spoof = null; }
                    if (spoof) return spoof;
                    return origJavaGetString.call(this, name);
                };
                if (GLES20.glGetIntegerv) {
                    var origGetIntegerv = GLES20.glGetIntegerv.overload("int", "[I", "int");
                    origGetIntegerv.implementation = function (pname, params, offset) {
                        try {
                            var off = offset || 0;
                            if (params) {
                                if (pname === 0x0D33 && glLimits.max_texture_size) {
                                    params[off] = glLimits.max_texture_size;
                                    return;
                                }
                                if (pname === 0x0D3A && glLimits.max_viewport_dims) {
                                    params[off] = glLimits.max_viewport_dims[0];
                                    params[off + 1] = glLimits.max_viewport_dims[1];
                                    return;
                                }
                                if (pname === 0x8869 && glLimits.max_vertex_attribs) {
                                    params[off] = glLimits.max_vertex_attribs;
                                    return;
                                }
                                if (pname === 0x8872 && glLimits.max_texture_units) {
                                    params[off] = glLimits.max_texture_units;
                                    return;
                                }
                            }
                        } catch (e) { }
                        return origGetIntegerv.call(this, pname, params, offset);
                    };
                }
                console.log("[GPU] Java GLES20 spoofed");
            } catch (e) {
                console.log("[GPU] Java GLES20 hook skipped: " + e);
            }

            // glGetShaderPrecisionFormat (Java): same triple as the native hook
            // so a WebView app comparing the two answers sees one GPU.
            try {
                if (GLES20.glGetShaderPrecisionFormat) {
                    var origJavaPrecision = GLES20.glGetShaderPrecisionFormat.overload(
                        "int", "int", "[I");
                    origJavaPrecision.implementation = function (shaderType, precisionType, range) {
                        try {
                            if (range) {
                                range[0] = 23;
                                range[1] = 23;
                                range[2] = 23;
                                return;
                            }
                        } catch (e) { }
                        return origJavaPrecision.call(this, shaderType, precisionType, range);
                    };
                }
            } catch (e) { }

            // WebView/Chromium read a few GPU facts straight from
            // SystemProperties (no GL call involved), so those go through the
            // same chip profile. ro.hardware.egl / ro.hardware.vulkan are
            // deliberately left alone (they are .so suffixes; see propFor).
            try {
                var SystemProperties = Java.use("android.os.SystemProperties");
                var origSpGet = SystemProperties.get.overload("java.lang.String");
                origSpGet.implementation = function (key) {
                    try {
                        var spoof = propFor(key);
                        if (spoof) return spoof;
                    } catch (e) { }
                    return origSpGet.call(this, key);
                };
            } catch (e) { }

            try {
                var EGL14 = Java.use("android.opengl.EGL14");
                var origEglQ = EGL14.eglQueryString.overload(
                    "android.opengl.EGLDisplay", "int");
                origEglQ.implementation = function (display, name) {
                    var spoof = null;
                    try { spoof = eglStringFor(name); } catch (e) { spoof = null; }
                    if (spoof) return spoof;
                    return origEglQ.call(this, display, name);
                };
                console.log("[GPU] Java EGL14 spoofed");
            } catch (e) {
                console.log("[GPU] Java EGL14 hook skipped: " + e);
            }
        });
    }

    if (typeof setImmediate === "function") {
        setImmediate(installJavaHooks);
    } else {
        installJavaHooks();
    }

    console.log("[GPU] Graphics-only hooks ready ✓");
})();
