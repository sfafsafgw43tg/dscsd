/**
 * Native Hooks - Zero-ANR C-level detection bypass for Android emulator.
 *
 * Compatible with both Frida 16 and Frida 17 (uses Process.findModuleByName
 * + module.findExportByName instead of the removed static Module.findExportByName).
 *
 * Performance & stability invariants (prevents ANR / RenderThread crashes):
 * - NEVER hooks read(), close(), stat(), lstat(), or getenv() — those ultra-hot
 *   libc functions fire tens of thousands of times/sec across Looper, Binder,
 *   InputChannel, ART, and RenderThread and starve Frida's single-threaded JS VM.
 * - Spoofs /proc/cpuinfo cleanly on open() using libc memfd_create() (zero read()
 *   or close() overhead, zero fd leaks, zero buffer overflows).
 * - Emulator hardware pipes (/dev/qemu_pipe, /dev/goldfish_*, /dev/socket/qemud)
 *   and system graphics/audio libraries (libgoldfish*, libqemu*) are NEVER blocked
 *   at the native libc level so Android's EGL, Vulkan, Gralloc, camera, sensor,
 *   and audio HALs work normally inside every app process.
 * - Skips hooking __system_property_get when OS-level resetprop-rs / build.prop
 *   has already updated /dev/__properties__ natively.
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

    // Non-HAL emulator indicator files safe to hide from native access/open/fopen
    // (never block live /dev/qemu_pipe, /dev/goldfish_*, /dev/socket/qemud pipes
    // that EGL/Vulkan/sensors/camera HALs probe and open at runtime)
    var hiddenFiles = [
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

    // Fast pre-filter so normal app/framework paths exit immediately
    var hiddenHintRe = /qemu|goldfish|ranchu|vbox|nox|droid4x|androVM|microvirt|ttVM|youwave|android_x86|x86\.prop|ttyGF/i;

    function isHiddenPath(path) {
        if (!path || path.length < 7) return false;
        // Fast skip for hot ART/app/apex/proc/vendor paths that never match hiddenFiles
        if (path.charCodeAt(0) === 47) {
            var c1 = path.charCodeAt(1);
            // Skip /proc, /apex, /vendor, /product, /storage, /mnt, /cache, /odm
            if (c1 === 112 || c1 === 97 || c1 === 118 || c1 === 109 || c1 === 99 || c1 === 111) {
                return false;
            }
        }
        if (!hiddenHintRe.test(path)) return false;
        for (var i = 0; i < hiddenFiles.length; i++) {
            if (path.indexOf(hiddenFiles[i]) !== -1) return true;
        }
        return false;
    }

    var id = (typeof deviceIdentity !== "undefined" && deviceIdentity) ? deviceIdentity : {};
    var cpuModel = id.cpuModel || "{{CPU_MODEL}}" || "Qualcomm Kryo 585";
    var cpuHardware = id.cpuHardware || "{{CPU_HARDWARE}}" || id.board || "qcom";
    var targetBoard = id.board || id.hardware || "qcom";

    // Realistic spoofed /proc/cpuinfo served via memfd_create (zero read() hook)
    var fakeCpuInfo =
        "processor\t: 0\n" +
        "BogoMIPS\t: 38.40\n" +
        "Features\t: fp asimd evtstrm aes pmull sha1 sha2 crc32 atomics fphp asimdhp\n" +
        "CPU implementer\t: 0x41\n" +
        "CPU architecture: 8\n" +
        "CPU variant\t: 0x1\n" +
        "CPU part\t: 0xd46\n" +
        "CPU revision\t: 0\n\n" +
        "Hardware\t: " + cpuHardware + "\n" +
        "Model\t\t: " + cpuModel + "\n";

    var memfdCreateFn = null;
    var writeFn = null;
    var lseekFn = null;
    var closeFn = null;
    var fcloseFn = null;
    var memfdNamePtr = null;
    var cpuInfoBufPtr = null;
    var cpuInfoBufLen = 0;

    // Self-defense guard: NEVER attach native libc open/fopen/access interceptors
    // inside critical Android system UI processes (com.android.settings,
    // com.android.systemui, com.android.providers.settings, system_server).
    try {
        var rawOpenPtr = findExport("libc.so", "open");
        var rawReadPtr = findExport("libc.so", "read");
        var rawClosePtr = findExport("libc.so", "close");
        if (rawOpenPtr && rawReadPtr && rawClosePtr) {
            var rawOpen = new NativeFunction(rawOpenPtr, "int", ["pointer", "int"]);
            var rawRead = new NativeFunction(rawReadPtr, "long", ["int", "pointer", "ulong"]);
            var rawClose = new NativeFunction(rawClosePtr, "int", ["int"]);
            var cfd = rawOpen(Memory.allocUtf8String("/proc/self/cmdline"), 0);
            if (cfd >= 0) {
                var cbuf = Memory.alloc(256);
                var n = rawRead(cfd, cbuf, 255);
                rawClose(cfd);
                if (n > 0) {
                    cbuf.add(n).writeU8(0);
                    var procName = safeReadUtf8(cbuf) || "";
                    if (procName === "com.android.settings" ||
                        procName === "com.android.systemui" ||
                        procName === "com.android.providers.settings" ||
                        procName === "system_server") {
                        console.log("[NativeHooks] Skipping native libc hooks in protected system process: " + procName);
                        return;
                    }
                }
            }
        }
    } catch (e) { }

    try {
        var mfdPtr = findExport("libc.so", "memfd_create");
        var wPtr = findExport("libc.so", "write");
        var lsPtr = findExport("libc.so", "lseek");
        var cPtr = findExport("libc.so", "close");
        var fcPtr = findExport("libc.so", "fclose");
        if (cPtr) {
            closeFn = new NativeFunction(cPtr, "int", ["int"]);
        }
        if (fcPtr) {
            fcloseFn = new NativeFunction(fcPtr, "int", ["pointer"]);
        }
        if (mfdPtr && wPtr && lsPtr && closeFn) {
            memfdCreateFn = new NativeFunction(mfdPtr, "int", ["pointer", "uint"]);
            writeFn = new NativeFunction(wPtr, "long", ["int", "pointer", "ulong"]);
            lseekFn = new NativeFunction(lsPtr, "long", ["int", "long", "int"]);
            memfdNamePtr = Memory.allocUtf8String("cpuinfo");
            cpuInfoBufPtr = Memory.allocUtf8String(fakeCpuInfo);
            cpuInfoBufLen = fakeCpuInfo.length;
        }
    } catch (e) { }

    function createSpoofedCpuinfoFd() {
        if (!memfdCreateFn) return -1;
        try {
            var mfd = memfdCreateFn(memfdNamePtr, 0);
            if (mfd < 0) return -1;
            writeFn(mfd, cpuInfoBufPtr, cpuInfoBufLen);
            lseekFn(mfd, 0, 0);
            return mfd;
        } catch (e) {
            return -1;
        }
    }

    console.log("[NativeHooks] Initializing lightweight native bypass...");

    // =============================================
    // 1. LIBC ACCESS() HOOK (non-HAL indicator files only)
    // =============================================
    try {
        var accessPtr = findExport("libc.so", "access");
        if (accessPtr) {
            Interceptor.attach(accessPtr, {
                onEnter: function (args) {
                    this.hide = isHiddenPath(safeReadUtf8(args[0]));
                },
                onLeave: function (retval) {
                    if (this.hide) {
                        try { this.errno = 2; } catch (e) { }
                        retval.replace(-1);
                    }
                }
            });
            console.log("[NativeHooks] access() hooked ✓");
        }
    } catch (e) {
        console.log("[NativeHooks] access() hook error: " + e);
    }

    // =============================================
    // 2. LIBC FOPEN() HOOK (non-HAL indicator files only)
    // =============================================
    try {
        var fopenPtr = findExport("libc.so", "fopen");
        if (fopenPtr) {
            Interceptor.attach(fopenPtr, {
                onEnter: function (args) {
                    this.hide = isHiddenPath(safeReadUtf8(args[0]));
                },
                onLeave: function (retval) {
                    if (this.hide) {
                        if (fcloseFn && !retval.isNull()) {
                            try { fcloseFn(retval); } catch (e) { }
                        }
                        try { this.errno = 2; } catch (e) { }
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
    // 3. LIBC OPEN() HOOK (non-HAL hiding + memfd /proc/cpuinfo swap)
    //    Zero read() / close() Interceptor overhead; closes replaced FDs so
    //    no file descriptors ever leak.
    // =============================================
    try {
        var openPtr = findExport("libc.so", "open");
        if (openPtr) {
            Interceptor.attach(openPtr, {
                onEnter: function (args) {
                    var path = safeReadUtf8(args[0]);
                    this.hide = isHiddenPath(path);
                    this.isCpuinfo = (path === "/proc/cpuinfo");
                },
                onLeave: function (retval) {
                    if (this.hide) {
                        var leakedFd = retval.toInt32();
                        if (closeFn && leakedFd >= 0) {
                            try { closeFn(leakedFd); } catch (e) { }
                        }
                        try { this.errno = 2; } catch (e) { }
                        retval.replace(-1);
                        return;
                    }
                    if (this.isCpuinfo) {
                        var origFd = retval.toInt32();
                        if (origFd >= 0) {
                            var fakeFd = createSpoofedCpuinfoFd();
                            if (fakeFd >= 0) {
                                try { closeFn(origFd); } catch (e) { }
                                retval.replace(fakeFd);
                            }
                        }
                    }
                }
            });
            console.log("[NativeHooks] open() + memfd /proc/cpuinfo hooked ✓");
        }
    } catch (e) {
        console.log("[NativeHooks] open() hook error: " + e);
    }

    // =============================================
    // 4. __system_property_get HOOK (only when OS-level props aren't spoofed yet)
    // =============================================
    try {
        var propGetPtr = findExport("libc.so", "__system_property_get");
        if (propGetPtr) {
            // Probe OS-level /dev/__properties__ once. When resetprop-rs or
            // build.prop already spoofed the OS natively, skip hooking this
            // ultra-hot libc function to keep app startup instant.
            var nativeAlreadySpoofed = false;
            try {
                var nativePropGet = new NativeFunction(propGetPtr, "int", ["pointer", "pointer"]);
                var tmpBuf = Memory.alloc(128);
                function readNativeProp(k) {
                    tmpBuf.writeU8(0);
                    var len = nativePropGet(Memory.allocUtf8String(k), tmpBuf);
                    return len > 0 ? tmpBuf.readUtf8String(len) : "";
                }
                var curModel = readNativeProp("ro.product.model");
                var curSerial = readNativeProp("ro.serialno");
                var curQemu = readNativeProp("ro.kernel.qemu");
                if (id.model && curModel === String(id.model) &&
                    (!id.serial || curSerial === String(id.serial)) &&
                    curQemu === "0") {
                    nativeAlreadySpoofed = true;
                }
            } catch (e) { }

            if (nativeAlreadySpoofed) {
                console.log("[NativeHooks] OS props already spoofed natively — skipping __system_property_get hook ✓");
            } else {
                var propReplacements = {
                    "ro.kernel.qemu": "0",
                    "ro.boot.qemu": "0",
                    "ro.hardware": targetBoard,
                    "ro.boot.hardware": targetBoard,
                    "ro.build.characteristics": "default",
                    "ro.boot.verifiedbootstate": "green",
                    "ro.boot.flash.locked": "1",
                    "ro.boot.veritymode": "enforcing",
                    "init.svc.qemu-props": ""
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
                        this.replacement = null;
                        this.value = null;
                        if (!args[0] || args[0].isNull() || !args[1] || args[1].isNull()) return;
                        // Fast first-byte filter: only 'r' (ro.*), 'g' (gsm.*), 'i' (init.svc.*)
                        try {
                            var c0 = args[0].readU8();
                            if (c0 !== 114 && c0 !== 103 && c0 !== 105) return;
                            var name = args[0].readUtf8String();
                            if (name && Object.prototype.hasOwnProperty.call(propReplacements, name)) {
                                this.replacement = propReplacements[name];
                                this.value = args[1];
                            }
                        } catch (e) { }
                    },
                    onLeave: function (retval) {
                        if (this.replacement !== null && this.value) {
                            var rep = this.replacement;
                            if (rep.length > 91) rep = rep.slice(0, 91);
                            try {
                                this.value.writeUtf8String(rep);
                                retval.replace(rep.length);
                            } catch (e) { }
                        }
                    }
                });
                console.log("[NativeHooks] __system_property_get fallback hooked ✓");
            }
        }
    } catch (e) {
        console.log("[NativeHooks] property_get hook error: " + e);
    }

    console.log("[NativeHooks] Native hooks ready ✓");
})();
