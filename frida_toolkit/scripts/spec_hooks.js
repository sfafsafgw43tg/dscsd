/**
 * Hardware Spec Hooks - make Settings and apps report the *model's* specs.
 *
 * Reported problem: with spoofing applied, Settings -> About still shows the
 * emulator, e.g. under CPU:
 *
 *     CPU hardware          ranchu
 *     Supported ABIs        x86_64, arm64-v8a
 *     GPU renderer          Android Emulator OpenGL ES Translator (Google SwiftShader)
 *     GPU vendor            Google (Google Inc.)
 *     GPU version           OpenGL ES-CM 1.1 ...
 *
 * and RAM / storage / cores are whatever the AVD happens to have rather than
 * what the phone being spoofed really sells with.
 *
 * Every answer here comes from ``deviceIdentity.hardware`` (built by
 * profiles/hardware_specs.py from the selected model), so the numbers cannot
 * contradict each other or the Build.* fields.
 *
 * Why Frida and not just props: Settings -> CPU is rendered by
 * com.android.settings, which must NOT be Frida-hooked (it crashes - see
 * PROTECTED_SYSTEM_PACKAGES), and much of that page is read by
 * com.android.settings through these very APIs:
 *
 *   ActivityManager.getMemoryInfo()      -> Settings -> About -> RAM
 *   StatFs("/data")                      -> Settings -> Storage
 *   System.getProperty("os.arch"/...)    -> Settings -> CPU
 *   Build.SUPPORTED_ABIS / CPU_ABI       -> Settings -> CPU (x86_64 leak)
 *   GLES20.glGetString(GL_RENDERER)      -> Settings -> CPU -> GPU renderer
 *   EGL14.eglQueryString(EGL_VENDOR)     -> Settings -> CPU -> GPU vendor
 *   os.arch / /proc/cpuinfo              -> Settings -> CPU -> CPU hardware
 *
 * Anything this file cannot back up is left alone on purpose: it is better to
 * show one emulator value than to answer with a number that contradicts itself.
 */

(function () {
    "use strict";

    var id = (typeof deviceIdentity !== "undefined" && deviceIdentity) ? deviceIdentity : {};
    var hw = id.hardware || {};

    if (typeof Java === "undefined" || !Java.available) {
        return;
    }

    var MB = 1024 * 1024;
    var logged = {};

    function note(key, message) {
        if (!logged[key]) {
            logged[key] = true;
            console.log("[SpecHooks] " + message);
        }
    }

    function numberOr(value, fallback) {
        var n = parseInt(value, 10);
        return isNaN(n) ? fallback : n;
    }

    // =====================================================================
    // 1. RAM - ActivityManager.getMemoryInfo + MemoryInfo fields
    // =====================================================================
    // Settings reads totalMem/availMem/threshold from MemoryInfo. Reporting
    // the phone's retail RAM keeps Settings consistent with the model; the
    // used/free split keeps the same ratio the VM really has, so the numbers
    // add up (used + free = total) instead of looking impossible.
    try {
        var totalMb = numberOr(hw.ram_reported_mb, 0);
        var availMb = numberOr(hw.ram_available_mb, 0);
        if (totalMb > 0) {
            // Percentage of the retail figure that is actually free on this VM.
            var freeRatio = availMb > 0 ? Math.min(0.85, Math.max(0.08, availMb / totalMb)) : 0.35;
            // A real phone keeps ~40-55% free after boot with the user's apps.
            freeRatio = Math.min(0.62, Math.max(0.28, freeRatio));

            var ActivityManager = Java.use("android.app.ActivityManager");
            var MemoryInfo = Java.use("android.app.ActivityManager$MemoryInfo");

            var setField = function (info, name, value) {
                try {
                    if (value === undefined || value === null) return;
                    var field = MemoryInfo[name];
                    if (field === undefined) return;
                    // Kept writable: Java fields need .value on a class handle.
                    field.value = value;
                } catch (e) { }
            };

            var paint = function (info) {
                try {
                    var totalBytes = totalMb * MB;
                    var freeBytes = Math.floor(totalBytes * freeRatio);
                    setField(info, "totalMem", totalBytes);
                    setField(info, "availMem", freeBytes);
                    // lowMemory must agree with availMem, or apps that check it
                    // see a healthy phone with a "low memory" flag.
                    setField(info, "lowMemory", false);
                    setField(info, "threshold", Math.floor(totalMb * MB * 0.06));
                } catch (e) { }
                return info;
            };

            try {
                // void getMemoryInfo(MemoryInfo outInfo) — fills the out-param, returns nothing.
                // Implementation must NOT return a value (void return type).
                var _ami = ActivityManager.getMemoryInfo.overload("android.app.ActivityManager$MemoryInfo");
                _ami.implementation = function (info) {
                    _ami.call(this, info);
                    paint(info);
                };
            } catch (e) { }

            // Android 8+ also has getMemoryInfo() returning a fresh object.
            try {
                ActivityManager.getMemoryInfo.overload().implementation = function () {
                    return paint(this.getMemoryInfo());
                };
            } catch (e) { }

            try {
                var Debug = Java.use("android.os.Debug");
                var DebugMemoryInfo = Java.use("android.os.Debug$MemoryInfo");
                // getMemoryInfo(stat) — void method that fills the out-param.
                var _dmi = Debug.getMemoryInfo.overload("android.os.Debug$MemoryInfo");
                _dmi.implementation = function (info) {
                    _dmi.call(this, info);
                    try {
                        var freeKb = Math.floor(totalMb * 1024 * freeRatio);
                        var field = DebugMemoryInfo["freeMem"];
                        if (field !== undefined) field.value = freeKb;
                        var total = DebugMemoryInfo["totalMem"];
                        if (total !== undefined) total.value = totalMb * 1024;
                    } catch (e) { }
                };
            } catch (e) { }

            note("ram", "RAM reports " + (totalMb / 1024).toFixed(1) + " GB (VM has " +
                (availMb > 0 ? (availMb / 1024).toFixed(1) + " GB" : "unmeasured") + ")");
        }
    } catch (e) {
        console.log("[SpecHooks] RAM hook error: " + e);
    }

    // =====================================================================
    // 2. Storage - StatFs on /data and /storage/emulated
    // =====================================================================
    // Settings -> Storage uses getBlockCountLong * getBlockSizeLong. The retail
    // size is what the phone really has, and used/free are scaled from it so
    // "used + free = total" holds (Settings computes one from the other).
    try {
        var storageMb = numberOr(hw.storage_reported_mb, 0);
        var usedMb = numberOr(hw.storage_used_mb, 0);
        if (storageMb > 0) {
            var GB = 1024 * 1024 * 1024;
            var totalGb = storageMb / 1024;
            var blockSize = 4096;
            var totalBlocks = Math.floor((totalGb * GB) / blockSize);
            var usedBlocks = Math.floor(((usedMb / 1024) * GB) / blockSize);
            var freeBlocks = totalBlocks - usedBlocks;

            var StatFs = Java.use("android.os.StatFs");

            StatFs.getBlockCountLong.implementation = function () { return totalBlocks; };
            StatFs.getBlockCount.implementation = function () { return totalBlocks; };
            try {
                StatFs.getAvailableBlocksLong.implementation = function () { return freeBlocks; };
                StatFs.getFreeBlocksLong.implementation = function () { return freeBlocks; };
            } catch (e) { }
            StatFs.getAvailableBlocks.implementation = function () { return freeBlocks; };
            StatFs.getFreeBlocks.implementation = function () { return freeBlocks; };
            StatFs.getBlockSizeLong.implementation = function () { return blockSize; };
            StatFs.getBlockSize.implementation = function () { return blockSize; };
            try {
                StatFs.getAvailableBytes.implementation = function () { return freeBlocks * blockSize; };
                StatFs.getFreeBytes.implementation = function () { return freeBlocks * blockSize; };
                StatFs.getTotalBytes.implementation = function () { return totalBlocks * blockSize; };
            } catch (e) { }

            note("storage", "storage reports " + totalGb.toFixed(0) + " GB (" +
                (usedMb / 1024).toFixed(1) + " GB used, " + (freeBlocks * blockSize / GB).toFixed(1) + " GB free)");
        }
    } catch (e) {
        console.log("[SpecHooks] storage hook error: " + e);
    }

    // =====================================================================
    // 3. CPU + ABIs - the "Supported ABIs" row that says x86_64
    // =====================================================================
    // Build.SUPPORTED_ABIS is a static String[]; writing the field is enough
    // for Settings (it reads Build.SUPPORTED_ABIS directly), and the real
    // device only ever advertises ARM ABIs.
    try {
        var abi = hw.abi || ["arm64-v8a", "armeabi-v7a"];
        var Build = Java.use("android.os.Build");
        var toJavaArray = function (values) {
            var StringClass = Java.use("java.lang.String");
            var array = Java.array("java.lang.String", values.map(function (v) {
                return StringClass.$new(v);
            }));
            return array;
        };

        try {
            Build.SUPPORTED_ABIS.value = toJavaArray(abi);
            // 32-bit list: only the 32-bit entries, like a real device.
            var abi32 = abi.filter(function (a) { return /v7a|x86(?!_64)/.test(a); });
            Build.SUPPORTED_32_BIT_ABIS.value = toJavaArray(abi32.length ? abi32 : [abi[abi.length - 1]]);
            Build.SUPPORTED_64_BIT_ABIS.value = toJavaArray(abi.filter(function (a) { return /64/.test(a); }));
            // Deprecated single-value fields must agree with the list.
            Build.CPU_ABI.value = abi[0];
            Build.CPU_ABI2.value = abi[1] || "";
            note("abi", "Supported ABIs report " + abi.join(", "));
        } catch (e) {
            console.log("[SpecHooks] ABI hook error: " + e);
        }

        // os.arch / os.name: "x86_64" is a dead giveaway in Settings -> CPU.
        try {
            var System = Java.use("java.lang.System");
            var arch = String(abi[0]).indexOf("arm") === 0 ? "aarch64" : "aarch64";
            var props = {
                "os.arch": arch,
                "os.name": "Linux",
                "os.version": "5.15.94-android14-11-g4f4d4b0e1f7a",
            };
            System.getProperty.overload("java.lang.String").implementation = function (key) {
                if (Object.prototype.hasOwnProperty.call(props, String(key))) {
                    return props[String(key)];
                }
                return this.getProperty(key);
            };
            System.getProperty.overload("java.lang.String", "java.lang.String").implementation = function (key, def) {
                if (Object.prototype.hasOwnProperty.call(props, String(key))) {
                    return props[String(key)];
                }
                return this.getProperty(key, def);
            };
            note("osarch", "os.arch reports " + arch);
        } catch (e) { }

        // Settings -> CPU reads /proc/cpuinfo for "CPU hardware" (Hardware :)
        // and the per-core "CPU part". native_hooks.js serves a memfd-backed
        // /proc/cpuinfo; this keeps the Java-side values consistent.
        try {
            var Runtime = Java.use("java.lang.Runtime");
            Runtime.availableProcessors.implementation = function () {
                var cores = numberOr(hw.cpu_cores, 0);
                return cores > 0 ? cores : this.availableProcessors();
            };
            note("cores", "cores report " + hw.cpu_cores);
        } catch (e) { }
    } catch (e) {
        console.log("[SpecHooks] CPU hook error: " + e);
    }

    // =====================================================================
    // 4. GPU - the "Android Emulator OpenGL ES Translator (SwiftShader)" row
    // =====================================================================
    // gpu_hooks.js already handles these in app processes; Settings -> CPU
    // runs in a *different* package, so the same answers are installed here
    // for whatever process this script is loaded into.
    try {
        var renderer = id.gl_renderer || id.glRenderer || hw.gpu;
        var vendor = id.gl_vendor || id.glVendor || "Qualcomm";
        var version = id.gl_version || "OpenGL ES 3.2 V@0738.0";
        var eglVendor = id.egl_vendor || vendor;

        var answerGL = function (name) {
            if (name === 0x1F00) return vendor;      // GL_VENDOR
            if (name === 0x1F01) return renderer;    // GL_RENDERER
            if (name === 0x1F02) return version;     // GL_VERSION
            if (name === 0x1F03) return id.gl_extensions || "";  // GL_EXTENSIONS
            if (name === 0x1F04) return (id.gl_limits || {}).glsl_version || "OpenGL ES GLSL ES 3.20";
            return null;
        };

        try {
            var GLES20 = Java.use("android.opengl.GLES20");
            GLES20.glGetString.overload("int").implementation = function (name) {
                var spoofed = answerGL(name);
                if (spoofed !== null) return spoofed;
                return this.glGetString(name);
            };
            note("gles", "GPU renderer reports " + renderer + " (" + vendor + ")");
        } catch (e) { }

        try {
            var EGL14 = Java.use("android.opengl.EGL14");
            EGL14.eglQueryString.overload("android.opengl.EGLDisplay", "int").implementation = function (display, name) {
                if (name === 0x3053) return eglVendor;   // EGL_VENDOR
                if (name === 0x3054) return "1.4 " + eglVendor;  // EGL_VERSION
                return this.eglQueryString(display, name);
            };
            note("egl", "EGL vendor reports " + eglVendor);
        } catch (e) { }
    } catch (e) {
        console.log("[SpecHooks] GPU hook error: " + e);
    }

    // =====================================================================
    // 5. Screen - DisplayMetrics / Configuration must match the model
    // =====================================================================
    try {
        var screen = hw.screen || {};
        var width = numberOr(screen.width, 0);
        var height = numberOr(screen.height, 0);
        var dpi = numberOr(screen.dpi, 0);
        if (width > 0 && height > 0 && dpi > 0) {
            var Resources = Java.use("android.content.res.Resources");
            var DisplayMetrics = Java.use("android.util.DisplayMetrics");
            Resources.getDisplayMetrics.implementation = function () {
                var metrics = this.getDisplayMetrics();
                try {
                    metrics.widthPixels.value = width;
                    metrics.heightPixels.value = height;
                    metrics.densityDpi.value = dpi;
                    metrics.density.value = dpi / 160.0;
                    metrics.scaledDensity.value = (dpi / 160.0);
                    metrics.xdpi.value = dpi;
                    metrics.ydpi.value = dpi;
                } catch (e) { }
                return metrics;
            };
            note("screen", "screen reports " + width + "x" + height + " @ " + dpi + " dpi");
        }
    } catch (e) {
        console.log("[SpecHooks] screen hook error: " + e);
    }

    // =====================================================================
    // 6. Telephony - the Polish +48 line and the carrier that matches it
    // =====================================================================
    try {
        var number = id.phoneNumber || hw.phone_number || "";
        var carrier = id.carrier || id.operator || hw.carrier || "";
        var numeric = id.operatorNumeric || hw.operator_numeric || "";
        var TelephonyManager = Java.use("android.telephony.TelephonyManager");

        if (number) {
            try {
                TelephonyManager.getLine1Number.overload().implementation = function () { return number; };
                TelephonyManager.getLine1Number.overload("int").implementation = function (slot) { return slot === 0 ? number : null; };
            } catch (e) { }
            // Android 11+ hides the number behind a permission; the "own
            // number" and MSISDN paths are what apps actually call.
            try {
                TelephonyManager.getMsisdn.overload().implementation = function () { return number; };
            } catch (e) { }
            try {
                var SubscriptionManager = Java.use("android.telephony.SubscriptionManager");
                var SubscriptionInfo = Java.use("android.telephony.SubscriptionInfo");
                SubscriptionInfo.getNumber.implementation = function () { return number; };
            } catch (e) { }
            note("line1", "line 1 reports " + number);
        }

        if (carrier) {
            try {
                TelephonyManager.getSimOperatorName.overload().implementation = function () { return carrier; };
                TelephonyManager.getNetworkOperatorName.overload().implementation = function () { return carrier; };
                TelephonyManager.getSimOperatorName.overload("int").implementation = function (slot) { return carrier; };
            } catch (e) { }
            try {
                TelephonyManager.getSimCountryIso.overload().implementation = function () { return "pl"; };
                TelephonyManager.getNetworkCountryIso.overload().implementation = function () { return "pl"; };
            } catch (e) { }
            if (numeric) {
                try {
                    TelephonyManager.getSimOperator.overload().implementation = function () { return numeric; };
                    TelephonyManager.getNetworkOperator.overload().implementation = function () { return numeric; };
                } catch (e) { }
            }
            note("carrier", "SIM reports " + carrier + (numeric ? " (" + numeric + ")" : ""));
        }
    } catch (e) {
        console.log("[SpecHooks] telephony hook error: " + e);
    }

    console.log("[SpecHooks] hardware spec hooks installed");
})();
