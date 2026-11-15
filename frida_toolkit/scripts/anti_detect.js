/**
 * AntiDetect Frida Script - Comprehensive emulator detection bypass
 * Hooks Android Java APIs to spoof device identity and hide emulator traces.
 *
 * Stability & ANR invariants:
 * - Runs Java.perform inside setImmediate() so script.load() never blocks
 *   device.resume(pid) on spawned processes.
 * - Does NOT hook Resources.getString / Resources.getText (called 10k-25k times
 *   on the Main UI thread in Settings, causing ANRs).
 * - Skips hooking SystemProperties.get when OS-level resetprop-rs / build.prop
 *   has already updated /dev/__properties__ natively.
 * - Every hook captures its explicit overload reference (origFn.call(this, ...))
 *   and wraps its body in try/catch so a JS error or null arg can never crash
 *   the target app.
 */

// Configurable identity values (when run standalone; combined script provides deviceIdentity)
var config = {
    imei: "{{IMEI}}",
    serial: "{{SERIAL}}",
    androidId: "{{ANDROID_ID}}",
    model: "{{MODEL}}",
    brand: "{{BRAND}}",
    manufacturer: "{{MANUFACTURER}}",
    fingerprint: "{{FINGERPRINT}}",
    device: "{{DEVICE}}",
    board: "{{BOARD}}",
    product: "{{PRODUCT}}",
    hardware: "{{HARDWARE}}",
    bootloader: "{{BOOTLOADER}}",
    baseband: "{{BASEBAND}}"
};

console.log("[AntiDetect] Starting anti-detection hooks...");

function _installAntiDetectHooks() {
    if (typeof Java === "undefined" || !Java.available) return;
    Java.perform(function () {
        var id = (typeof deviceIdentity !== "undefined" && deviceIdentity) ? deviceIdentity : config;

        // =============================================
        // 1. BUILD CLASS SPOOFING (all fields + getSerial)
        // =============================================
        function safeSetField(cls, name, val) {
            if (val === undefined || val === null || val === "") return;
            try {
                if (cls[name]) cls[name].value = val;
            } catch (e) { }
        }

        try {
            var Build = Java.use("android.os.Build");

            safeSetField(Build, "MODEL", id.model);
            safeSetField(Build, "BRAND", id.brand);
            safeSetField(Build, "MANUFACTURER", id.manufacturer);
            safeSetField(Build, "FINGERPRINT", id.fingerprint);
            safeSetField(Build, "DEVICE", id.device);
            safeSetField(Build, "BOARD", id.board);
            safeSetField(Build, "HARDWARE", id.hardware || id.board);
            safeSetField(Build, "PRODUCT", id.productName || id.product || id.device);
            safeSetField(Build, "SERIAL", id.serial);
            safeSetField(Build, "BOOTLOADER", id.bootloader);
            safeSetField(Build, "ID", id.buildId);
            safeSetField(Build, "DISPLAY", id.buildDisplayId || id.buildId);
            safeSetField(Build, "TAGS", id.buildTags || "release-keys");
            safeSetField(Build, "TYPE", id.buildType || "user");
            safeSetField(Build, "HOST", "build.android.com");

            try {
                var origGetSerial = Build.getSerial.overload();
                origGetSerial.implementation = function () {
                    try {
                        if (id.serial) return id.serial;
                    } catch (e) { }
                    return origGetSerial.call(this);
                };
            } catch (e) { }

            console.log("[AntiDetect] Build properties spoofed ✓");
        } catch (e) {
            console.log("[AntiDetect] Build hook error: " + e);
        }

        // =============================================
        // 2. TELEPHONY MANAGER (IMEI / MEID)
        // =============================================
        try {
            var TelephonyManager = Java.use("android.telephony.TelephonyManager");

            if (id.imei) {
                try {
                    TelephonyManager.getDeviceId.overload().implementation = function () {
                        return id.imei;
                    };
                    TelephonyManager.getDeviceId.overload("int").implementation = function (slot) {
                        return id.imei;
                    };
                } catch (e) { }

                try {
                    TelephonyManager.getImei.overload().implementation = function () {
                        return id.imei;
                    };
                    TelephonyManager.getImei.overload("int").implementation = function (slot) {
                        return id.imei;
                    };
                } catch (e) { }

                try {
                    TelephonyManager.getMeid.overload().implementation = function () {
                        return id.imei;
                    };
                } catch (e) { }
            }

            try {
                var fakeImsi = "310260" + Math.random().toString().substring(2, 12);
                TelephonyManager.getSubscriberId.overload().implementation = function () {
                    return fakeImsi;
                };
            } catch (e) { }

            try {
                var fakeNumber = "+1" + Math.floor(Math.random() * 9000000000 + 1000000000);
                TelephonyManager.getLine1Number.overload().implementation = function () {
                    return fakeNumber;
                };
            } catch (e) { }

            try {
                var fakeSim = "89" + Math.random().toString().substring(2, 20);
                TelephonyManager.getSimSerialNumber.overload().implementation = function () {
                    return fakeSim;
                };
            } catch (e) { }

            console.log("[AntiDetect] TelephonyManager spoofed ✓");
        } catch (e) {
            console.log("[AntiDetect] TelephonyManager hook error: " + e);
        }

        // =============================================
        // 3. SETTINGS.SECURE (Android ID)
        // =============================================
        try {
            var Secure = Java.use("android.provider.Settings$Secure");
            var origSecureGetString = Secure.getString.overload(
                "android.content.ContentResolver",
                "java.lang.String"
            );
            origSecureGetString.implementation = function (resolver, name) {
                try {
                    if (name === "android_id" && id.androidId) {
                        return id.androidId;
                    }
                    if (name === "bluetooth_name" && id.model) {
                        return id.model;
                    }
                } catch (e) { }
                return origSecureGetString.call(this, resolver, name);
            };
            console.log("[AntiDetect] Settings.Secure spoofed ✓");
        } catch (e) {
            console.log("[AntiDetect] Settings.Secure hook error: " + e);
        }

        // =============================================
        // 4. FILE EXISTENCE CHECKS (O(1) getPath + fast RegExp pre-filter)
        // =============================================
        try {
            var File = Java.use("java.io.File");

            var emulatorFiles = [
                "/system/lib/libc_malloc_debug_qemu.so",
                "/sys/qemu_trace",
                "/system/bin/qemu-props",
                "/dev/socket/qemud",
                "/dev/qemu_pipe",
                "/dev/goldfish_pipe",
                "/system/bin/androVM-prop",
                "/system/bin/microvirt-prop",
                "/system/lib/libdroid4x.so",
                "/data/youwave_id",
                "/dev/vboxguest",
                "/dev/vboxuser",
                "/system/bin/nox-prop",
                "/system/bin/nox",
                "/system/bin/ttVM-prop",
                "/data/data/com.bluestacks",
                "/data/data/com.bignox.app",
                "/data/data/com.memu"
            ];

            var fileHintRe = /qemu|goldfish|vbox|nox|droid4x|androVM|microvirt|ttVM|youwave|bluestacks|bignox|memu/i;
            var origFileExists = File.exists.overload();
            var origGetPath = File.getPath.overload();

            origFileExists.implementation = function () {
                try {
                    var path = origGetPath.call(this);
                    if (path && fileHintRe.test(path)) {
                        for (var i = 0; i < emulatorFiles.length; i++) {
                            if (path === emulatorFiles[i] || path.indexOf(emulatorFiles[i]) !== -1) {
                                return false;
                            }
                        }
                    }
                } catch (e) { }
                return origFileExists.call(this);
            };

            console.log("[AntiDetect] File checks bypassed ✓");
        } catch (e) {
            console.log("[AntiDetect] File hook error: " + e);
        }

        // =============================================
        // 5. SYSTEM PROPERTIES (skip if OS-level resetprop already matches)
        // =============================================
        try {
            var SystemProperties = Java.use("android.os.SystemProperties");
            var origSpGet1 = SystemProperties.get.overload("java.lang.String");
            var origSpGet2 = SystemProperties.get.overload("java.lang.String", "java.lang.String");

            var osPropsReady = false;
            try {
                var curM = String(origSpGet1.call(SystemProperties, "ro.product.model") || "");
                var curS = String(origSpGet1.call(SystemProperties, "ro.serialno") || "");
                var curQ = String(origSpGet1.call(SystemProperties, "ro.kernel.qemu") || "");
                var curH = String(origSpGet1.call(SystemProperties, "ro.hardware") || "");
                var wantH = String(id.hardware || id.board || "");
                if (id.model && curM === String(id.model) &&
                    (!id.serial || curS === String(id.serial)) &&
                    curQ === "0" &&
                    (!wantH || curH === wantH)) {
                    osPropsReady = true;
                }
            } catch (e) { }

            if (osPropsReady) {
                console.log("[AntiDetect] OS SystemProperties already spoofed natively — skipping JS hook ✓");
            } else {
                var propsToSpoof = {
                    "ro.kernel.qemu": "0",
                    "ro.boot.qemu": "0",
                    "ro.hardware": id.hardware || id.board || "qcom",
                    "ro.boot.hardware": id.hardware || id.board || "qcom",
                    "init.svc.qemu-props": "",
                    "ro.build.characteristics": "default",
                    "ro.boot.verifiedbootstate": "green",
                    "ro.boot.flash.locked": "1",
                    "ro.boot.veritymode": "enforcing",
                    "ro.build.type": id.buildType || "user",
                    "ro.build.tags": id.buildTags || "release-keys"
                };

                function putProp(k, v) {
                    if (v !== undefined && v !== null && String(v) !== "") {
                        propsToSpoof[k] = String(v);
                    }
                }

                putProp("ro.product.model", id.model);
                putProp("ro.product.brand", id.brand);
                putProp("ro.product.manufacturer", id.manufacturer);
                putProp("ro.product.device", id.device);
                putProp("ro.product.board", id.board);
                putProp("ro.product.name", id.productName || id.product || id.device);
                putProp("ro.build.product", id.device);
                putProp("ro.build.fingerprint", id.fingerprint);
                putProp("ro.bootimage.build.fingerprint", id.fingerprint);
                putProp("ro.serialno", id.serial);
                putProp("ro.boot.serialno", id.serial);
                putProp("ro.baseband", id.baseband);
                putProp("gsm.version.baseband", id.baseband);
                putProp("ro.bootloader", id.bootloader);
                putProp("ro.build.id", id.buildId);
                putProp("ro.build.display.id", id.buildDisplayId || id.buildId);
                putProp("ro.build.version.incremental", id.buildIncremental);
                putProp("ro.build.version.release", id.buildRelease);
                putProp("ro.build.flavor", id.buildFlavor);
                putProp("ro.build.description", id.buildDescription);

                ["system", "system_ext", "product", "vendor", "odm"].forEach(function (part) {
                    putProp("ro.product." + part + ".model", id.model);
                    putProp("ro.product." + part + ".brand", id.brand);
                    putProp("ro.product." + part + ".manufacturer", id.manufacturer);
                    putProp("ro.product." + part + ".device", id.device);
                    putProp("ro.product." + part + ".name", id.productName || id.product || id.device);
                    putProp("ro." + part + ".build.fingerprint", id.fingerprint);
                    putProp("ro." + part + ".build.id", id.buildId);
                    putProp("ro." + part + ".build.type", id.buildType || "user");
                    putProp("ro." + part + ".build.tags", id.buildTags || "release-keys");
                });

                origSpGet1.implementation = function (key) {
                    try {
                        if (key && Object.prototype.hasOwnProperty.call(propsToSpoof, key)) {
                            return propsToSpoof[key];
                        }
                    } catch (e) { }
                    return origSpGet1.call(this, key);
                };

                origSpGet2.implementation = function (key, def) {
                    try {
                        if (key && Object.prototype.hasOwnProperty.call(propsToSpoof, key)) {
                            return propsToSpoof[key];
                        }
                    } catch (e) { }
                    return origSpGet2.call(this, key, def);
                };

                try {
                    var origSpGetInt = SystemProperties.getInt.overload("java.lang.String", "int");
                    origSpGetInt.implementation = function (key, def) {
                        try {
                            if (key === "ro.kernel.qemu" || key === "ro.boot.qemu") {
                                return 0;
                            }
                        } catch (e) { }
                        return origSpGetInt.call(this, key, def);
                    };
                } catch (e) { }

                console.log("[AntiDetect] SystemProperties fallback spoofed ✓");
            }
        } catch (e) {
            console.log("[AntiDetect] SystemProperties hook error: " + e);
        }

        // =============================================
        // 6. SENSORS (Hide emulator sensors)
        // =============================================
        try {
            var Sensor = Java.use("android.hardware.Sensor");
            var origSensorName = Sensor.getName.overload();
            var origSensorVendor = Sensor.getVendor.overload();

            origSensorName.implementation = function () {
                var name = origSensorName.call(this);
                try {
                    if (name) {
                        var low = String(name).toLowerCase();
                        if (low.indexOf("goldfish") !== -1 || low.indexOf("qemu") !== -1) {
                            return "LSM6DS3 Accelerometer";
                        }
                    }
                } catch (e) { }
                return name;
            };

            origSensorVendor.implementation = function () {
                var vendor = origSensorVendor.call(this);
                try {
                    if (vendor) {
                        var low = String(vendor).toLowerCase();
                        if (low.indexOf("goldfish") !== -1 || low.indexOf("qemu") !== -1) {
                            return "STMicroelectronics";
                        }
                    }
                } catch (e) { }
                return vendor;
            };

            console.log("[AntiDetect] Sensors spoofed ✓");
        } catch (e) {
            console.log("[AntiDetect] Sensor hook error: " + e);
        }

        // =============================================
        // 7. PACKAGE MANAGER (Hide root + emulator packages)
        // =============================================
        try {
            var PackageManager = Java.use("android.app.ApplicationPackageManager");
            var NameNotFoundException = Java.use("android.content.pm.PackageManager$NameNotFoundException");

            var hiddenPackages = [
                "com.topjohnwu.magisk",
                "eu.chainfire.supersu",
                "com.noshufou.android.su",
                "com.thirdparty.superuser",
                "com.koushikdutta.superuser",
                "com.zachspong.temprootremovejb",
                "com.ramdroid.appquarantine",
                "de.robv.android.xposed.installer",
                "com.saurik.substrate",
                "com.devadvance.rootcloak",
                "com.amphoras.hidemyroot",
                "com.genymotion.genyd",
                "com.bluestacks",
                "com.bignox.app",
                "com.memu",
                "com.ldmnq",
                "com.kaopu.gameassistant",
                "com.google.android.launcher.layouts.genymotion",
                "com.vphone.launcher"
            ];

            var hiddenPkgRe = /magisk|supersu|noshufou|superuser|rootcloak|hidemyroot|xposed|substrate|genymotion|bluestacks|bignox|memu|ldmnq|gameassistant|vphone|appquarantine|temproot/i;

            function isHiddenPkg(pkg) {
                if (!pkg || !hiddenPkgRe.test(pkg)) return false;
                for (var i = 0; i < hiddenPackages.length; i++) {
                    if (pkg.indexOf(hiddenPackages[i]) !== -1) return true;
                }
                return false;
            }

            var origGetPkgInt = PackageManager.getPackageInfo.overload("java.lang.String", "int");
            origGetPkgInt.implementation = function (pkg, flags) {
                if (isHiddenPkg(pkg)) {
                    throw NameNotFoundException.$new(String(pkg));
                }
                return origGetPkgInt.call(this, pkg, flags);
            };

            // Android 13+ (API 33+) PackageInfoFlags overload
            try {
                var origGetPkgFlags = PackageManager.getPackageInfo.overload(
                    "java.lang.String",
                    "android.content.pm.PackageManager$PackageInfoFlags"
                );
                origGetPkgFlags.implementation = function (pkg, flags) {
                    if (isHiddenPkg(pkg)) {
                        throw NameNotFoundException.$new(String(pkg));
                    }
                    return origGetPkgFlags.call(this, pkg, flags);
                };
            } catch (e) { }

            console.log("[AntiDetect] PackageManager spoofed ✓");
        } catch (e) {
            console.log("[AntiDetect] PackageManager hook error: " + e);
        }

        // =============================================
        // 8. DEBUG / ROOT DETECTION (word-boundary safe)
        // =============================================
        try {
            var Debug = Java.use("android.os.Debug");
            Debug.isDebuggerConnected.overload().implementation = function () {
                return false;
            };

            var rootCmdRe = /(?:^|[\s/])(su|which|busybox|magisk)(?:$|[\s/])/i;
            var Runtime = Java.use("java.lang.Runtime");
            var IOException = Java.use("java.io.IOException");

            var execStr = Runtime.exec.overload("java.lang.String");
            execStr.implementation = function (cmd) {
                if (cmd && rootCmdRe.test(String(cmd))) {
                    throw IOException.$new("Permission denied");
                }
                return execStr.call(this, cmd);
            };

            var execArr = Runtime.exec.overload("[Ljava.lang.String;");
            execArr.implementation = function (cmdArr) {
                if (cmdArr) {
                    var joined = [];
                    for (var i = 0; i < cmdArr.length; i++) {
                        joined.push(String(cmdArr[i]));
                    }
                    if (rootCmdRe.test(joined.join(" "))) {
                        throw IOException.$new("Permission denied");
                    }
                }
                return execArr.call(this, cmdArr);
            };

            console.log("[AntiDetect] Debug/Root checks bypassed ✓");
        } catch (e) {
            console.log("[AntiDetect] Debug hook error: " + e);
        }

        // =============================================
        // 9. SETTINGS APP UI SPOOFING
        //    Hooks non-final setText(CharSequence, BufferType) instead of final
        //    setText(CharSequence) so Android 14 ART does not deoptimize the
        //    inlined boot-image wrapper, and skips EDITABLE buffers (EditText).
        // =============================================
        try {
            var StringClass = Java.use("java.lang.String");
            var TextView = Java.use("android.widget.TextView");
            var origSetText = TextView.setText.overload(
                "java.lang.CharSequence",
                "android.widget.TextView$BufferType"
            );

            var uiReplacements = [
                ["About emulated device", "About phone"],
                ["Emulated device", id.model],
                ["sdk_gphone64_x86_64", id.model],
                ["sdk_gphone64", id.model],
                ["sdk_gphone", id.model],
                ["generic_x86_64", id.device],
                ["generic_x86", id.device],
                ["emu64xa", id.device],
                ["emu64x", id.device],
                ["ranchu", id.board],
                ["goldfish", id.board],
                ["Android SDK built for x86_64", id.model],
                ["Android SDK built for x86", id.model],
                ["AOSP on x86_64", id.model],
                ["AOSP on x86", id.model],
                ["emulator64_x86_64", id.device],
                ["emulator64_x86", id.device]
            ];

            var uiHintRe = /emulated|sdk_gphone|generic_x86|emu64x|ranchu|goldfish|Android SDK built|AOSP on x86|emulator64/i;

            origSetText.implementation = function (text, type) {
                try {
                    if (text !== null && text !== undefined) {
                        var isEditable = type !== null && type !== undefined && String(type) === "EDITABLE";
                        if (!isEditable) {
                            var str = text.toString();
                            if (str && uiHintRe.test(str)) {
                                var changed = false;
                                for (var i = 0; i < uiReplacements.length; i++) {
                                    var target = uiReplacements[i][0];
                                    var replacement = uiReplacements[i][1];
                                    if (replacement && str.indexOf(target) !== -1) {
                                        str = str.split(target).join(replacement);
                                        changed = true;
                                    }
                                }
                                if (changed) {
                                    return origSetText.call(this, StringClass.$new(str), type);
                                }
                            }
                        }
                    }
                } catch (e) { }
                return origSetText.call(this, text, type);
            };

            console.log("[AntiDetect] TextView setText hooked ✓");
        } catch (e) {
            console.log("[AntiDetect] TextView hook error: " + e);
        }

        // =============================================
        // 10. DEVICE NAME + ADB SETTINGS SPOOFING
        // =============================================
        try {
            var Global = Java.use("android.provider.Settings$Global");
            var origGlobalGetString = Global.getString.overload(
                "android.content.ContentResolver",
                "java.lang.String"
            );
            origGlobalGetString.implementation = function (resolver, name) {
                try {
                    if (name === "device_name" && id.model) {
                        return id.model;
                    }
                    if (name === "emulator_device_name") {
                        return "";
                    }
                } catch (e) { }
                return origGlobalGetString.call(this, resolver, name);
            };

            // Anti-detection without breaking adb: emulator management needs
            // adb_enabled ON, but apps probing Settings.Global see "off".
            var origGlobalGetInt2 = Global.getInt.overload(
                "android.content.ContentResolver",
                "java.lang.String"
            );
            origGlobalGetInt2.implementation = function (resolver, name) {
                if (name === "adb_enabled" || name === "development_settings_enabled") {
                    return 0;
                }
                return origGlobalGetInt2.call(this, resolver, name);
            };

            var origGlobalGetInt3 = Global.getInt.overload(
                "android.content.ContentResolver",
                "java.lang.String",
                "int"
            );
            origGlobalGetInt3.implementation = function (resolver, name, def) {
                if (name === "adb_enabled" || name === "development_settings_enabled") {
                    return 0;
                }
                return origGlobalGetInt3.call(this, resolver, name, def);
            };
            console.log("[AntiDetect] Device Name + adb_enabled spoofed in Settings ✓");
        } catch (e) {
            console.log("[AntiDetect] Device Name hook error: " + e);
        }

        // =============================================
        // 11. OPENGL / GPU RENDERER SPOOFING (identity-driven)
        // =============================================
        var glRenderer = id.glRenderer || "Adreno (TM) 740";
        var glVendor = id.glVendor || "Qualcomm";

        try {
            var GLES20 = Java.use("android.opengl.GLES20");
            var origGlGetString = GLES20.glGetString.overload("int");
            origGlGetString.implementation = function (name) {
                var result = origGlGetString.call(this, name);
                try {
                    if (name === 0x1F01) {  // GL_RENDERER
                        if (result && (result.indexOf("Android Emulator") !== -1 ||
                            result.indexOf("SwiftShader") !== -1 ||
                            result.indexOf("ANGLE") !== -1)) {
                            return glRenderer;
                        }
                    } else if (name === 0x1F00) {  // GL_VENDOR
                        if (result && (result.indexOf("Google") !== -1 || result.indexOf("Android") !== -1)) {
                            return glVendor;
                        }
                    }
                } catch (e) { }
                return result;
            };
            console.log("[AntiDetect] OpenGL spoofed (" + glVendor + " / " + glRenderer + ") ✓");
        } catch (e) {
            console.log("[AntiDetect] OpenGL hook error: " + e);
        }

        // =============================================
        // 12. EGL DISPLAY INFO (identity-driven)
        // =============================================
        try {
            var EGL14 = Java.use("android.opengl.EGL14");
            var origEglQueryString = EGL14.eglQueryString.overload("android.opengl.EGLDisplay", "int");
            origEglQueryString.implementation = function (display, name) {
                var result = origEglQueryString.call(this, display, name);
                try {
                    if (name === 0x3053 && result) {  // EGL_VENDOR
                        if (result.indexOf("Google") !== -1 || result.indexOf("Android") !== -1) {
                            return glVendor;
                        }
                    }
                } catch (e) { }
                return result;
            };
            console.log("[AntiDetect] EGL spoofed ✓");
        } catch (e) {
            console.log("[AntiDetect] EGL hook error: " + e);
        }

        // =============================================
        // 13. BATTERY STATE SPOOFING
        // =============================================
        try {
            var BatteryManager = Java.use("android.os.BatteryManager");
            var fakeLevel = 45 + Math.floor(Math.random() * 40);
            var origBattProp = BatteryManager.getIntProperty.overload("int");

            origBattProp.implementation = function (propId) {
                if (propId === 6) {  // BATTERY_PROPERTY_STATUS -> DISCHARGING
                    return 3;
                }
                if (propId === 4) {  // BATTERY_PROPERTY_CAPACITY
                    return fakeLevel;
                }
                return origBattProp.call(this, propId);
            };
            console.log("[AntiDetect] Battery spoofed ✓");
        } catch (e) {
            console.log("[AntiDetect] Battery hook error: " + e);
        }

        // =============================================
        // 14. WIFI / BLUETOOTH MAC SPOOFING
        // =============================================
        try {
            var WifiInfo = Java.use("android.net.wifi.WifiInfo");
            WifiInfo.getMacAddress.overload().implementation = function () {
                var spoofedMac = id.wifiMac || "94:65:2D:4A:7C:1E";
                return spoofedMac.toUpperCase();
            };
            console.log("[AntiDetect] WiFi MAC spoofed ✓");
        } catch (e) {
            console.log("[AntiDetect] WiFi MAC hook error: " + e);
        }

        try {
            var BluetoothAdapter = Java.use("android.bluetooth.BluetoothAdapter");
            BluetoothAdapter.getAddress.overload().implementation = function () {
                var spoofedMac = id.bluetoothMac || "00:1A:7D:5B:82:3F";
                return spoofedMac.toUpperCase();
            };
            console.log("[AntiDetect] Bluetooth MAC spoofed ✓");
        } catch (e) {
            console.log("[AntiDetect] Bluetooth MAC hook error: " + e);
        }

        // =============================================
        // 15. CONNECTIVITY CHECK
        // =============================================
        try {
            var NetworkInfo = Java.use("android.net.NetworkInfo");
            NetworkInfo.getType.overload().implementation = function () {
                return 1;  // WIFI
            };
            NetworkInfo.getTypeName.overload().implementation = function () {
                return "WIFI";
            };
            console.log("[AntiDetect] Network type spoofed ✓");
        } catch (e) {
            console.log("[AntiDetect] Network hook error: " + e);
        }

        console.log("[AntiDetect] All hooks installed successfully! ✓");
    });
}

if (typeof setImmediate === "function") {
    setImmediate(_installAntiDetectHooks);
} else {
    _installAntiDetectHooks();
}
