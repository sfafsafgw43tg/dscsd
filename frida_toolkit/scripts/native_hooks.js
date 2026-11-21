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
    var hw = id.hardware || {};
    var cpuModel = id.cpuModel || "{{CPU_MODEL}}" || "Qualcomm Kryo 585";
    var cpuHardware = id.cpuHardware || "{{CPU_HARDWARE}}" || id.board || "qcom";
    var targetBoard = id.board || id.hardware || "qcom";

    // ------------------------------------------------------------------
    // /proc/cpuinfo for the SELECTED MODEL
    // ------------------------------------------------------------------
    // Settings -> CPU -> "CPU hardware" reads "Hardware : ranchu" from here,
    // and the emulator's per-core blocks advertise x86_64 ("CPU architecture:
    // 6", "CPU part: 0x0"). Both are replaced with the real ARM layout of the
    // spoofed SoC, with one processor block per real core.
    var coreCount = parseInt(hw.cpu_cores, 10);
    if (isNaN(coreCount) || coreCount < 1 || coreCount > 16) coreCount = 8;

    // ARM implementer is always 0x41 (ARM Ltd) for Cortex-based SoCs. The part
    // number follows the big/little split of the SoC family.
    var soc = String(hw.soc || id.cpuModel || "");
    var bigPart = "0xd46";   // Cortex-A710 by default
    var cpuArch = 8;         // ARMv8 by default
    var features = "fp asimd evtstrm aes pmull sha1 sha2 crc32 atomics fphp asimdhp cpuid asimdrdm";
    // ARMv9 SoCs
    if (/Snapdragon 8 Gen 3|Snapdragon 8 Elite|Dimensity 9300|Dimensity 9400|Exynos 2400|Tensor G3|Tensor G4/.test(soc)) {
        bigPart = "0xd85";      // Cortex-X4
        cpuArch = 9;
        features += " asimddp lrcpc dcpop sha3 sm3 sm4 asimddp sha512 sve sve2";
    }
    else if (/Snapdragon 8 Gen 2|Exynos 2200/.test(soc)) bigPart = "0xd4d";   // Cortex-X3
    else if (/Gen 1|Tensor G2/.test(soc)) bigPart = "0xd44";                 // Cortex-X1/X2
    else if (/Tensor|Dimensity 9/.test(soc)) bigPart = "0xd44";
    else if (/Exynos 1[23]\d\d|Dimensity 6|Dimensity 7|G57|G68/.test(soc)) bigPart = "0xd03";  // Cortex-A53/A55
    var littlePart = "0xd05";  // Cortex-A55

    var fakeCpuInfo = "";
    for (var core = 0; core < coreCount; core++) {
        var isLittle = core >= Math.ceil(coreCount * 0.75);
        fakeCpuInfo +=
            "processor\t: " + core + "\n" +
            "BogoMIPS\t: 38.40\n" +
            "Features\t: " + features + "\n" +
            "CPU implementer\t: 0x41\n" +
            "CPU architecture: " + cpuArch + "\n" +
            "CPU variant\t: 0x2\n" +
            "CPU part\t: " + (isLittle ? littlePart : bigPart) + "\n" +
            "CPU revision\t: 0\n\n";
    }
    // Hardware line: use the SoC name without spaces (real phones use the
    // board/SoC codename here like "kalama", "e2s", "zuma", "mt6985").
    var hwLine = cpuHardware;
    if (hw.soc) {
        // Map marketing names to internal codenames like real /proc/cpuinfo
        var socCodename = soc.replace(/\s+/g, "");
        if (/Snapdragon8Gen3|Snapdragon8Elite/.test(socCodename)) hwLine = "pineapple";
        else if (/Snapdragon8Gen2/.test(socCodename)) hwLine = "kalama";
        else if (/Snapdragon8Gen1/.test(socCodename)) hwLine = "taro";
        else if (/Snapdragon7\+Gen2/.test(socCodename)) hwLine = "capes";
        else if (/Exynos2400/.test(socCodename)) hwLine = "s5e9945";
        else if (/Exynos1380/.test(socCodename)) hwLine = "s5e8835";
        else if (/Exynos1280/.test(socCodename)) hwLine = "s5e8825";
        else if (/TensorG3/.test(socCodename)) hwLine = "zuma";
        else if (/TensorG2/.test(socCodename)) hwLine = "gs201";
        else if (/Dimensity9200/.test(socCodename)) hwLine = "mt6985";
        else if (/Dimensity8300|Dimensity8200/.test(socCodename)) hwLine = "mt6895";
        else if (/Dimensity7200/.test(socCodename)) hwLine = "mt6886";
        else if (/Dimensity6100/.test(socCodename)) hwLine = "mt6835";
        else hwLine = socCodename.toLowerCase();
    }
    fakeCpuInfo +=
        "Hardware\t: " + hwLine + "\n" +
        "Model\t\t: " + cpuModel + "\n";

    // ------------------------------------------------------------------
    // /proc/meminfo for the SELECTED MODEL
    // ------------------------------------------------------------------
    // Settings -> About -> RAM and most memory probes read MemTotal/MemAvailable
    // from here (not just ActivityManager), so both must agree.
    var fakeMemInfo = null;
    (function () {
        var totalMb = parseInt(hw.ram_reported_mb, 10);
        if (isNaN(totalMb) || totalMb <= 0) return;
        var availMb = parseInt(hw.ram_available_mb, 10);
        var freeRatio = (availMb > 0) ? Math.min(0.62, Math.max(0.28, availMb / totalMb)) : 0.35;
        var totalKb = totalMb * 1024;
        var freeKb = Math.floor(totalKb * freeRatio);
        var cachedKb = Math.floor(totalKb * 0.18);
        var buffersKb = Math.floor(totalKb * 0.02);
        var availKb = freeKb + cachedKb;
        var swapKb = Math.floor(totalKb * 0.5);
        var zramKb = Math.floor(totalKb * 0.45);
        fakeMemInfo =
            "MemTotal:       " + totalKb + " kB\n" +
            "MemFree:        " + freeKb + " kB\n" +
            "MemAvailable:   " + availKb + " kB\n" +
            "Buffers:        " + buffersKb + " kB\n" +
            "Cached:         " + cachedKb + " kB\n" +
            "SwapCached:            0 kB\n" +
            "SwapTotal:      " + swapKb + " kB\n" +
            "SwapFree:       " + Math.floor(swapKb * 0.9) + " kB\n" +
            "ZramTotal:      " + zramKb + " kB\n" +
            "ZramFree:       " + Math.floor(zramKb * 0.85) + " kB\n" +
            "Dirty:              1024 kB\n" +
            "Writeback:             0 kB\n" +
            "Shmem:           " + Math.floor(totalKb * 0.04) + " kB\n" +
            "Slab:            " + Math.floor(totalKb * 0.05) + " kB\n";
    })();

    var memfdCreateFn = null;
    var writeFn = null;
    var lseekFn = null;
    var closeFn = null;
    var fcloseFn = null;
    var memfdNamePtr = null;
    var cpuInfoBufPtr = null;
    var cpuInfoBufLen = 0;
    var memfdMemNamePtr = null;
    var memInfoBufPtr = null;
    var memInfoBufLen = 0;

    // Self-defense guard: avoid the ultra-hot libc open/fopen/access interceptors
    // inside critical Android system UI processes (com.android.settings,
    // com.android.systemui, com.android.providers.settings, system_server)
    // because those trigger RenderThread/ART GC crashes on Android 14+.
    // We STILL apply the __system_property_get hook and the /proc/cpuinfo &
    // /proc/meminfo memfd replacements in those processes - those are what
    // make Settings -> CPU stop saying "ranchu", "SwiftShader", "x86_64" and
    // the wrong RAM total. We just don't intercept access/fopen/open for
    // emulator-file hiding (which is what causes the ANRs).
    var isProtectedProcess = false;
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
                        isProtectedProcess = true;
                        console.log("[NativeHooks] Protected system process detected: " + procName +
                                    " - applying safe hooks only (prop/cpuinfo/meminfo, no file hiding)");
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
            if (fakeMemInfo) {
                memfdMemNamePtr = Memory.allocUtf8String("meminfo");
                memInfoBufPtr = Memory.allocUtf8String(fakeMemInfo);
                memInfoBufLen = fakeMemInfo.length;
            }
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

    // Same memfd trick for /proc/meminfo (RAM totals from the model).
    function createSpoofedMeminfoFd() {
        if (!memfdCreateFn || !memInfoBufPtr) return -1;
        try {
            var mfd = memfdCreateFn(memfdMemNamePtr, 0);
            if (mfd < 0) return -1;
            writeFn(mfd, memInfoBufPtr, memInfoBufLen);
            lseekFn(mfd, 0, 0);
            return mfd;
        } catch (e) {
            return -1;
        }
    }

    console.log("[NativeHooks] Initializing lightweight native bypass...");

    // =============================================
    // 1. LIBC ACCESS() HOOK (non-HAL indicator files only)
    //    Skipped in protected system processes to avoid ANRs.
    // =============================================
    if (!isProtectedProcess) {
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
        //    Skipped in protected system processes.
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
    } else {
        console.log("[NativeHooks] Skipping access()/fopen() file-hiding hooks in protected process ✓");
    }

    // =============================================
    // 3. LIBC OPEN() HOOK (non-HAL hiding + memfd /proc/cpuinfo swap)
    //    Zero read() / close() Interceptor overhead; closes replaced FDs so
    //    no file descriptors ever leak.
    //    In protected system processes we still swap /proc/cpuinfo and
    //    /proc/meminfo (Settings needs those to stop showing "ranchu" and
    //    the AVD's RAM), but we SKIP the file-hiding path to avoid ANRs.
    // =============================================
    try {
        var openPtr = findExport("libc.so", "open");
        if (openPtr) {
            Interceptor.attach(openPtr, {
                onEnter: function (args) {
                    var path = safeReadUtf8(args[0]);
                    // Only hide emulator files in non-protected processes
                    this.hide = (!isProtectedProcess) && isHiddenPath(path);
                    this.isCpuinfo = (path === "/proc/cpuinfo");
                    this.isMeminfo = (path === "/proc/meminfo");
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
                    if (this.isMeminfo) {
                        var memFd = retval.toInt32();
                        if (memFd >= 0) {
                            var fakeMem = createSpoofedMeminfoFd();
                            if (fakeMem >= 0) {
                                try { closeFn(memFd); } catch (e) { }
                                retval.replace(fakeMem);
                            }
                        }
                    }
                }
            });
            console.log("[NativeHooks] open() + memfd /proc/cpuinfo + /proc/meminfo hooked" +
                        (isProtectedProcess ? " (safe mode - no file hiding)" : "") + " ✓");
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

                // SoC / chipset props - Settings "Processor" and apps reading these
                // should see a real phone SoC name, not "ranchu" or "goldfish".
                if (hw && hw.soc) {
                    // Board platform codename (e.g. "kalama", "zuma", "mt6985", "s5e9945")
                    var boardPlatform = targetBoard;
                    // Try to map marketing SoC to codename like real phones
                    var socName = String(hw.soc);
                    if (/Snapdragon.*Gen.*3|Snapdragon.*Elite/i.test(socName)) boardPlatform = "pineapple";
                    else if (/Snapdragon.*Gen.*2/i.test(socName)) boardPlatform = "kalama";
                    else if (/Snapdragon.*Gen.*1/i.test(socName)) boardPlatform = "taro";
                    else if (/Snapdragon.*7.*Gen.*2/i.test(socName)) boardPlatform = "capes";
                    else if (/Exynos.*2400/i.test(socName)) boardPlatform = "s5e9945";
                    else if (/Exynos.*1380/i.test(socName)) boardPlatform = "s5e8835";
                    else if (/Exynos.*1280/i.test(socName)) boardPlatform = "s5e8825";
                    else if (/Tensor.*G3/i.test(socName)) boardPlatform = "zuma";
                    else if (/Tensor.*G2/i.test(socName)) boardPlatform = "gs201";
                    else if (/Tensor\b/i.test(socName)) boardPlatform = "gs101";
                    else if (/Dimensity.*9200/i.test(socName)) boardPlatform = "mt6985";
                    else if (/Dimensity.*8300|Dimensity.*8200/i.test(socName)) boardPlatform = "mt6895";
                    else if (/Dimensity.*7200/i.test(socName)) boardPlatform = "mt6886";
                    else if (/Dimensity.*6100/i.test(socName)) boardPlatform = "mt6835";
                    propReplacements["ro.board.platform"] = boardPlatform;
                    propReplacements["ro.hardware.chipname"] = socName;
                    propReplacements["ro.soc.manufacturer"] = /Exynos/i.test(socName) ? "Samsung" :
                        /Tensor/i.test(socName) ? "Google" :
                        /Dimensity/i.test(socName) ? "MediaTek" : "Qualcomm";
                    propReplacements["ro.soc.model"] = socName;
                }

                // CPU ABI list - spoof to ARM like a real phone (not x86_64)
                var hwAbi = (hw && hw.abi) ? hw.abi : ["arm64-v8a", "armeabi-v7a", "armeabi"];
                if (hwAbi && hwAbi.length) {
                    var abi64list = hwAbi.filter(function(a){return a.indexOf("64") !== -1;});
                    var abi32list = hwAbi.filter(function(a){return a.indexOf("64") === -1;});
                    propReplacements["ro.product.cpu.abilist"] = hwAbi.join(",");
                    propReplacements["ro.product.cpu.abilist64"] = abi64list.join(",") || "arm64-v8a";
                    propReplacements["ro.product.cpu.abilist32"] = abi32list.join(",") || "armeabi-v7a,armeabi";
                    ["system", "vendor", "odm"].forEach(function (part) {
                        propReplacements["ro." + part + ".product.cpu.abilist"] = hwAbi.join(",");
                    });
                }

                // Telephony - Polish SIM carrier/MCC-MNC/line
                var phoneNumber = id.phoneNumber || "";
                var carrierName = id.carrier || (hw && hw.carrier) || "Play";
                var numericOp = id.operatorNumeric || (hw && hw.operator_numeric) || "26006";
                propReplacements["gsm.sim.operator.alpha"] = carrierName;
                propReplacements["gsm.operator.alpha"] = carrierName;
                propReplacements["gsm.sim.operator.iso-country"] = "pl";
                propReplacements["gsm.operator.iso-country"] = "pl";
                if (numericOp) propReplacements["gsm.sim.operator.numeric"] = numericOp;
                if (numericOp) propReplacements["gsm.operator.numeric"] = numericOp;

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
