/**
 * Native Hooks - C-level detection bypass for Android emulator.
 *
 * Compatible with both Frida 16 and Frida 17 (uses Process.findModuleByName
 * + module.findExportByName instead of the removed static Module.findExportByName).
 *
 * Important safety invariant:
 * - Emulator hardware pipes (/dev/qemu_pipe, /dev/goldfish_*, /dev/socket/qemud)
 *   and system graphics/audio shared libraries (libgoldfish*, libqemu*) are NOT
 *   blocked in open()/fopen()/dlopen() because Android's own EGL, camera, sensor
 *   and audio HALs open them lazily inside every app process. Existence probes
 *   (access/stat/lstat) still hide them.
 */

(function () {
    "use strict";

    // Frida 16 + Frida 17 portable export lookup
    function findExport(libName, exportName) {
        try {
            if (libName && typeof Process !== "undefined" && Process.findModuleByName) {
                var mod = Process.findModuleByName(libName);
                if (mod && mod.findExportByName) {
                    var addr = mod.findExportByName(exportName);
                    if (addr) return addr;
                }
            }
        } catch (e) { }
        try {
            if (typeof Module !== "undefined" && Module.findExportByName) {
                var addr2 = Module.findExportByName(libName, exportName);
                if (addr2) return addr2;
            }
        } catch (e) { }
        try {
            if (typeof Module !== "undefined" && Module.findGlobalExportByName) {
                return Module.findGlobalExportByName(exportName);
            }
        } catch (e) { }
        return null;
    }

    function safeReadUtf8(ptrArg) {
        if (!ptrArg || ptrArg.isNull()) return null;
        try {
            return ptrArg.readUtf8String();
        } catch (e) {
            return null;
        }
    }

    // Paths hidden from existence probes (access / stat / lstat)
    var probeHiddenFiles = [
        "/system/lib/libc_malloc_debug_qemu.so",
        "/sys/qemu_trace",
        "/system/bin/qemu-props",
        "/dev/socket/qemud",
        "/dev/qemu_pipe",
        "/dev/goldfish_pipe",
        "/dev/goldfish_audio",
        "/dev/goldfish_sync",
        "/dev/goldfish_events",
        "/system/bin/androVM-prop",
        "/system/bin/microvirt-prop",
        "/system/lib/libdroid4x.so",
        "/data/youwave_id",
        "/dev/vboxguest",
        "/dev/vboxuser",
        "/system/bin/nox-prop",
        "/system/bin/nox",
        "/system/bin/ttVM-prop",
        "/ueventd.android_x86.rc",
        "/x86.prop",
        "/ueventd.ttVM_x86.rc",
        "/init.ttVM_x86.rc",
        "/init.vbox86.rc",
        "/init.goldfish.rc",
        "/init.ranchu.rc",
        "/fstab.goldfish",
        "/fstab.ranchu",
        "/dev/ttyGF0",
        "/dev/ttyGF1"
    ];

    // Subset safe to block in open()/fopen() (never block live /dev/qemu_pipe
    // or /dev/goldfish_* pipes that EGL/sensors/camera need to function)
    var openHiddenFiles = [
        "/system/lib/libc_malloc_debug_qemu.so",
        "/sys/qemu_trace",
        "/system/bin/qemu-props",
        "/system/bin/androVM-prop",
        "/system/bin/microvirt-prop",
        "/system/lib/libdroid4x.so",
        "/data/youwave_id",
        "/dev/vboxguest",
        "/dev/vboxuser",
        "/system/bin/nox-prop",
        "/system/bin/nox",
        "/system/bin/ttVM-prop",
        "/init.goldfish.rc",
        "/init.ranchu.rc",
        "/fstab.goldfish",
        "/fstab.ranchu"
    ];

    function matchesList(path, list) {
        if (!path) return false;
        for (var i = 0; i < list.length; i++) {
            if (path.indexOf(list[i]) !== -1) return true;
        }
        return false;
    }

    var id = (typeof deviceIdentity !== "undefined" && deviceIdentity) ? deviceIdentity : {};
    var cpuModel = id.cpuModel || "{{CPU_MODEL}}" || "Qualcomm Kryo 585";
    var cpuHardware = id.cpuHardware || "{{CPU_HARDWARE}}" || id.board || "qcom";
    var targetBoard = id.board || id.hardware || "qcom";
    var targetModel = id.model || "Pixel 8";

    // Strings that indicate emulator in /proc/cpuinfo
    var cpuInfoReplacements = [
        [/QEMU Virtual CPU[^\n]*/g, cpuModel],
        [/Goldfish/g, "Qualcomm Technologies, Inc"],
        [/\branchu\b/g, targetBoard],
        [/generic_x86_64/g, targetBoard],
        [/Android Emulator/g, targetModel]
    ];

    console.log("[NativeHooks] Initializing native-level detection bypass...");

    // =============================================
    // 1. EXISTENCE PROBE HOOKS (access / stat / lstat)
    // =============================================
    ["access", "stat", "lstat"].forEach(function (fnName) {
        try {
            var fnPtr = findExport("libc.so", fnName);
            if (!fnPtr) return;
            Interceptor.attach(fnPtr, {
                onEnter: function (args) {
                    this.path = safeReadUtf8(args[0]);
                },
                onLeave: function (retval) {
                    if (matchesList(this.path, probeHiddenFiles)) {
                        retval.replace(-1);
                    }
                }
            });
            console.log("[NativeHooks] " + fnName + "() hooked ✓");
        } catch (e) {
            console.log("[NativeHooks] " + fnName + "() hook error: " + e);
        }
    });

    // =============================================
    // 2. LIBC FOPEN() HOOK (non-HAL indicator files only)
    // =============================================
    try {
        var fopenPtr = findExport("libc.so", "fopen");
        if (fopenPtr) {
            Interceptor.attach(fopenPtr, {
                onEnter: function (args) {
                    this.path = safeReadUtf8(args[0]);
                },
                onLeave: function (retval) {
                    if (matchesList(this.path, openHiddenFiles)) {
                        retval.replace(ptr(0));
                    }
                }
            });
            console.log("[NativeHooks] fopen() hooked ✓");
        }
    } catch (e) {
        console.log("[NativeHooks] fopen() hook error: " + e);
    }

    // =============================================
    // 3. OPEN() + CLOSE() + READ() FOR /proc/cpuinfo (safe bounds, no fd leak)
    // =============================================
    try {
        var cpuinfoFds = {};
        var openPtr = findExport("libc.so", "open");
        var readPtr = findExport("libc.so", "read");
        var closePtr = findExport("libc.so", "close");

        if (openPtr) {
            Interceptor.attach(openPtr, {
                onEnter: function (args) {
                    this.path = safeReadUtf8(args[0]);
                },
                onLeave: function (retval) {
                    if (!this.path) return;
                    if (matchesList(this.path, openHiddenFiles)) {
                        retval.replace(-1);
                        return;
                    }
                    var fd = retval.toInt32();
                    if (fd >= 0 && this.path.indexOf("/proc/cpuinfo") !== -1) {
                        cpuinfoFds[fd] = true;
                    }
                }
            });
            console.log("[NativeHooks] open() hooked ✓");
        }

        if (closePtr) {
            Interceptor.attach(closePtr, {
                onEnter: function (args) {
                    var fd = args[0].toInt32();
                    if (cpuinfoFds[fd]) {
                        delete cpuinfoFds[fd];
                    }
                }
            });
        }

        if (readPtr) {
            Interceptor.attach(readPtr, {
                onEnter: function (args) {
                    this.fd = args[0].toInt32();
                    this.buf = args[1];
                    this.count = args[2].toInt32();
                },
                onLeave: function (retval) {
                    var nread = retval.toInt32();
                    if (!cpuinfoFds[this.fd] || nread <= 0 || !this.buf || this.buf.isNull()) {
                        return;
                    }
                    try {
                        var content = this.buf.readUtf8String(nread);
                        if (!content) return;
                        var modified = content;
                        for (var i = 0; i < cpuInfoReplacements.length; i++) {
                            modified = modified.replace(
                                cpuInfoReplacements[i][0],
                                cpuInfoReplacements[i][1]
                            );
                        }
                        if (modified !== content) {
                            // Never write past the caller's buffer (this.count bytes)
                            var maxBytes = Math.min(this.count, nread);
                            var outStr = modified;
                            while (outStr.length > 0 && outStr.length > maxBytes) {
                                outStr = outStr.slice(0, maxBytes);
                            }
                            var bytes = [];
                            for (var j = 0; j < outStr.length && bytes.length < maxBytes; j++) {
                                var code = outStr.charCodeAt(j);
                                bytes.push(code < 128 ? code : 63);
                            }
                            this.buf.writeByteArray(bytes);
                            retval.replace(bytes.length);
                        }
                    } catch (e) { }
                }
            });
            console.log("[NativeHooks] /proc/cpuinfo spoofing hooked ✓");
        }
    } catch (e) {
        console.log("[NativeHooks] open/read/cpuinfo hook error: " + e);
    }

    // =============================================
    // 4. __system_property_get HOOK (identity-driven)
    // =============================================
    try {
        var propGetPtr = findExport("libc.so", "__system_property_get");
        if (propGetPtr) {
            var propReplacements = {
                "ro.kernel.qemu": "0",
                "ro.boot.qemu": "0",
                "ro.hardware": targetBoard,
                "ro.boot.hardware": targetBoard,
                "ro.build.characteristics": "default",
                "ro.boot.verifiedbootstate": "green",
                "ro.boot.flash.locked": "1",
                "ro.boot.veritymode": "enforcing",
                "init.svc.qemu-props": "",
                "qemu.hw.mainkeys": "",
                "qemu.sf.lcd_density": ""
            };

            function setIfPresent(k, v) {
                if (v !== undefined && v !== null && String(v) !== "") {
                    propReplacements[k] = String(v);
                }
            }

            setIfPresent("ro.product.model", id.model);
            setIfPresent("ro.product.brand", id.brand);
            setIfPresent("ro.product.manufacturer", id.manufacturer);
            setIfPresent("ro.product.device", id.device);
            setIfPresent("ro.product.board", id.board);
            setIfPresent("ro.product.name", id.productName || id.product || id.device);
            setIfPresent("ro.build.product", id.device);
            setIfPresent("ro.build.fingerprint", id.fingerprint);
            setIfPresent("ro.bootimage.build.fingerprint", id.fingerprint);
            setIfPresent("ro.serialno", id.serial);
            setIfPresent("ro.boot.serialno", id.serial);
            setIfPresent("ro.bootloader", id.bootloader);
            setIfPresent("ro.baseband", id.baseband);
            setIfPresent("gsm.version.baseband", id.baseband);
            setIfPresent("ro.build.id", id.buildId);
            setIfPresent("ro.build.display.id", id.buildDisplayId || id.buildId);
            setIfPresent("ro.build.version.incremental", id.buildIncremental);
            setIfPresent("ro.build.version.release", id.buildRelease);
            setIfPresent("ro.build.type", id.buildType || "user");
            setIfPresent("ro.build.tags", id.buildTags || "release-keys");
            setIfPresent("ro.build.flavor", id.buildFlavor);
            setIfPresent("ro.build.description", id.buildDescription);

            ["system", "system_ext", "product", "vendor", "odm"].forEach(function (part) {
                setIfPresent("ro.product." + part + ".model", id.model);
                setIfPresent("ro.product." + part + ".brand", id.brand);
                setIfPresent("ro.product." + part + ".manufacturer", id.manufacturer);
                setIfPresent("ro.product." + part + ".device", id.device);
                setIfPresent("ro.product." + part + ".name", id.productName || id.product || id.device);
                setIfPresent("ro." + part + ".build.fingerprint", id.fingerprint);
                setIfPresent("ro." + part + ".build.id", id.buildId);
                setIfPresent("ro." + part + ".build.type", id.buildType || "user");
                setIfPresent("ro." + part + ".build.tags", id.buildTags || "release-keys");
            });

            Interceptor.attach(propGetPtr, {
                onEnter: function (args) {
                    this.name = safeReadUtf8(args[0]);
                    this.value = args[1];
                },
                onLeave: function (retval) {
                    if (this.name && this.value && !this.value.isNull() &&
                        Object.prototype.hasOwnProperty.call(propReplacements, this.name)) {
                        var replacement = propReplacements[this.name];
                        // PROP_VALUE_MAX is 92 bytes (91 chars + NUL)
                        if (replacement.length > 91) {
                            replacement = replacement.slice(0, 91);
                        }
                        try {
                            this.value.writeUtf8String(replacement);
                            retval.replace(replacement.length);
                        } catch (e) { }
                    }
                }
            });
            console.log("[NativeHooks] __system_property_get hooked ✓");
        }
    } catch (e) {
        console.log("[NativeHooks] property_get hook error: " + e);
    }

    // =============================================
    // 5. GETENV HOOK
    // =============================================
    try {
        var getenvPtr = findExport("libc.so", "getenv");
        if (getenvPtr) {
            Interceptor.attach(getenvPtr, {
                onEnter: function (args) {
                    this.name = safeReadUtf8(args[0]);
                },
                onLeave: function (retval) {
                    if (this.name && this.name.indexOf("EMULATOR") !== -1) {
                        retval.replace(ptr(0));
                    }
                }
            });
            console.log("[NativeHooks] getenv() hooked ✓");
        }
    } catch (e) {
        console.log("[NativeHooks] getenv hook error: " + e);
    }

    console.log("[NativeHooks] All native hooks installed successfully! ✓");
})();
