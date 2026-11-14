/**
 * AntiDetect Frida Script - Comprehensive emulator detection bypass
 * Hooks Android Java APIs to spoof device identity and hide emulator traces.
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

if (typeof Java !== "undefined" && Java.available) {
    Java.perform(function () {
        var id = (typeof deviceIdentity !== "undefined" && deviceIdentity) ? deviceIdentity : config;

        // =============================================
        // 1. BUILD CLASS SPOOFING (all fields + getSerial)
        // =============================================
        try {
            var Build = Java.use("android.os.Build");

            if (id.model) Build.MODEL.value = id.model;
            if (id.brand) Build.BRAND.value = id.brand;
            if (id.manufacturer) Build.MANUFACTURER.value = id.manufacturer;
            if (id.fingerprint) Build.FINGERPRINT.value = id.fingerprint;
            if (id.device) Build.DEVICE.value = id.device;
            if (id.board) Build.BOARD.value = id.board;
            if (id.hardware || id.board) Build.HARDWARE.value = id.hardware || id.board;
            if (id.productName || id.product || id.device) {
                Build.PRODUCT.value = id.productName || id.product || id.device;
            }
            if (id.serial) Build.SERIAL.value = id.serial;
            if (id.bootloader) Build.BOOTLOADER.value = id.bootloader;
            if (id.buildId) Build.ID.value = id.buildId;
            if (id.buildDisplayId || id.buildId) {
                Build.DISPLAY.value = id.buildDisplayId || id.buildId;
            }

            Build.TAGS.value = id.buildTags || "release-keys";
            Build.TYPE.value = id.buildType || "user";
            Build.HOST.value = "build.android.com";

            try {
                Build.getSerial.overload().implementation = function () {
                    return id.serial || this.getSerial();
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
            Secure.getString.overload("android.content.ContentResolver", "java.lang.String").implementation =
                function (resolver, name) {
                    if (name === "android_id" && id.androidId) {
                        return id.androidId;
                    }
                    if (name === "bluetooth_name" && id.model) {
                        return id.model;
                    }
                    return this.getString(resolver, name);
                };
            console.log("[AntiDetect] Settings.Secure spoofed ✓");
        } catch (e) {
            console.log("[AntiDetect] Settings.Secure hook error: " + e);
        }

        // =============================================
        // 4. FILE EXISTENCE CHECKS
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

            File.exists.overload().implementation = function () {
                var path = this.getAbsolutePath();
                if (path) {
                    for (var i = 0; i < emulatorFiles.length; i++) {
                        if (path === emulatorFiles[i] || path.indexOf(emulatorFiles[i]) !== -1) {
                            return false;
                        }
                    }
                }
                return this.exists();
            };

            console.log("[AntiDetect] File checks bypassed ✓");
        } catch (e) {
            console.log("[AntiDetect] File hook error: " + e);
        }

        // =============================================
        // 5. SYSTEM PROPERTIES (unified across core + partition props)
        // =============================================
        try {
            var SystemProperties = Java.use("android.os.SystemProperties");

            var propsToSpoof = {
                "ro.kernel.qemu": "0",
                "ro.boot.qemu": "0",
                "ro.hardware": id.hardware || id.board || "qcom",
                "ro.boot.hardware": id.hardware || id.board || "qcom",
                "init.svc.qemu-props": "",
                "qemu.hw.mainkeys": "",
                "qemu.sf.lcd_density": "",
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

            SystemProperties.get.overload("java.lang.String").implementation = function (key) {
                if (Object.prototype.hasOwnProperty.call(propsToSpoof, key)) {
                    return propsToSpoof[key];
                }
                return this.get(key);
            };

            SystemProperties.get.overload("java.lang.String", "java.lang.String").implementation = function (key, def) {
                if (Object.prototype.hasOwnProperty.call(propsToSpoof, key)) {
                    return propsToSpoof[key];
                }
                return this.get(key, def);
            };

            try {
                SystemProperties.getInt.overload("java.lang.String", "int").implementation = function (key, def) {
                    if (key === "ro.kernel.qemu" || key === "ro.boot.qemu") {
                        return 0;
                    }
                    return this.getInt(key, def);
                };
            } catch (e) { }

            console.log("[AntiDetect] SystemProperties spoofed ✓");
        } catch (e) {
            console.log("[AntiDetect] SystemProperties hook error: " + e);
        }

        // =============================================
        // 6. SENSORS (Hide emulator sensors)
        // =============================================
        try {
            var Sensor = Java.use("android.hardware.Sensor");

            Sensor.getName.overload().implementation = function () {
                var name = this.getName();
                if (name && (name.toLowerCase().indexOf("goldfish") !== -1 ||
                    name.toLowerCase().indexOf("qemu") !== -1)) {
                    return "LSM6DS3 Accelerometer";
                }
                return name;
            };

            Sensor.getVendor.overload().implementation = function () {
                var vendor = this.getVendor();
                if (vendor && (vendor.toLowerCase().indexOf("goldfish") !== -1 ||
                    vendor.toLowerCase().indexOf("qemu") !== -1)) {
                    return "STMicroelectronics";
                }
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

            function isHiddenPkg(pkg) {
                if (!pkg) return false;
                for (var i = 0; i < hiddenPackages.length; i++) {
                    if (pkg.indexOf(hiddenPackages[i]) !== -1) return true;
                }
                return false;
            }

            PackageManager.getPackageInfo.overload("java.lang.String", "int").implementation = function (pkg, flags) {
                if (isHiddenPkg(pkg)) {
                    throw Java.use("android.content.pm.PackageManager$NameNotFoundException").$new();
                }
                return this.getPackageInfo(pkg, flags);
            };

            // Android 13+ (API 33+) PackageInfoFlags overload
            try {
                PackageManager.getPackageInfo.overload(
                    "java.lang.String",
                    "android.content.pm.PackageManager$PackageInfoFlags"
                ).implementation = function (pkg, flags) {
                    if (isHiddenPkg(pkg)) {
                        throw Java.use("android.content.pm.PackageManager$NameNotFoundException").$new();
                    }
                    return this.getPackageInfo(pkg, flags);
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

            var execStr = Runtime.exec.overload("java.lang.String");
            execStr.implementation = function (cmd) {
                if (cmd && rootCmdRe.test(String(cmd))) {
                    throw Java.use("java.io.IOException").$new("Permission denied");
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
                        throw Java.use("java.io.IOException").$new("Permission denied");
                    }
                }
                return execArr.call(this, cmdArr);
            };

            console.log("[AntiDetect] Debug/Root checks bypassed ✓");
        } catch (e) {
            console.log("[AntiDetect] Debug hook error: " + e);
        }

        // =============================================
        // 9. SETTINGS APP UI SPOOFING (null-safe)
        // =============================================
        var StringClass = Java.use("java.lang.String");

        try {
            var TextView = Java.use("android.widget.TextView");

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

            TextView.setText.overload("java.lang.CharSequence").implementation = function (text) {
                if (text) {
                    var str = text.toString();
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
                        return this.setText(StringClass.$new(str));
                    }
                }
                return this.setText(text);
            };

            console.log("[AntiDetect] TextView setText hooked ✓");
        } catch (e) {
            console.log("[AntiDetect] TextView hook error: " + e);
        }

        try {
            var Resources = Java.use("android.content.res.Resources");

            Resources.getString.overload("int").implementation = function (resId) {
                var res = this.getString(resId);
                if (!res) return res;
                if (res === "About emulated device") return "About phone";
                if (res === "Emulated device" && id.model) return id.model;
                if (res.indexOf("sdk_gphone") !== -1 && id.model) return id.model;
                if (res.indexOf("generic_x86") !== -1 && id.device) return id.device;
                if (res.indexOf("ranchu") !== -1 && id.board) return id.board;
                if (res.indexOf("goldfish") !== -1 && id.board) return id.board;
                return res;
            };

            Resources.getText.overload("int").implementation = function (resId) {
                var res = this.getText(resId);
                if (!res) return res;
                var str = res.toString();
                if (str === "About emulated device") return StringClass.$new("About phone");
                if (str === "Emulated device" && id.model) return StringClass.$new(id.model);
                if (str.indexOf("sdk_gphone") !== -1 && id.model) return StringClass.$new(id.model);
                return res;
            };

            console.log("[AntiDetect] Resources hooks installed ✓");
        } catch (e) {
            console.log("[AntiDetect] Resources hook error: " + e);
        }

        // =============================================
        // 10. DEVICE NAME + ADB SETTINGS SPOOFING
        // =============================================
        try {
            var Global = Java.use("android.provider.Settings$Global");
            Global.getString.overload("android.content.ContentResolver", "java.lang.String").implementation =
                function (resolver, name) {
                    if (name === "device_name" && id.model) {
                        return id.model;
                    }
                    if (name === "emulator_device_name") {
                        return "";
                    }
                    return this.getString(resolver, name);
                };

            // Anti-detection without breaking adb: emulator management needs
            // adb_enabled ON, but apps probing Settings.Global see "off".
            Global.getInt.overload("android.content.ContentResolver", "java.lang.String").implementation =
                function (resolver, name) {
                    if (name === "adb_enabled" || name === "development_settings_enabled") {
                        return 0;
                    }
                    return this.getInt(resolver, name);
                };
            Global.getInt.overload("android.content.ContentResolver", "java.lang.String", "int").implementation =
                function (resolver, name, def) {
                    if (name === "adb_enabled" || name === "development_settings_enabled") {
                        return 0;
                    }
                    return this.getInt(resolver, name, def);
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
            GLES20.glGetString.overload("int").implementation = function (name) {
                var result = this.glGetString(name);
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
            EGL14.eglQueryString.overload("android.opengl.EGLDisplay", "int").implementation =
                function (display, name) {
                    var result = this.eglQueryString(display, name);
                    if (name === 0x3053 && result) {  // EGL_VENDOR
                        if (result.indexOf("Google") !== -1 || result.indexOf("Android") !== -1) {
                            return glVendor;
                        }
                    }
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

            BatteryManager.getIntProperty.overload("int").implementation = function (propId) {
                if (propId === 6) {  // BATTERY_PROPERTY_STATUS -> DISCHARGING
                    return 3;
                }
                if (propId === 4) {  // BATTERY_PROPERTY_CAPACITY
                    return fakeLevel;
                }
                return this.getIntProperty(propId);
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
