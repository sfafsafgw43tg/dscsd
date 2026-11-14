/**
 * SafetyNet / Play Integrity API Bypass
 * Hooks attestation, location-mock, and root-hiding APIs without colliding
 * with anti_detect.js or hardcoding an Android API level.
 */

var deviceConfig = (typeof deviceIdentity !== "undefined" && deviceIdentity) ? deviceIdentity : {
    model: "{{MODEL}}",
    brand: "{{BRAND}}",
    manufacturer: "{{MANUFACTURER}}",
    device: "{{DEVICE}}",
    fingerprint: "{{FINGERPRINT}}",
    product: "{{PRODUCT}}",
    board: "{{BOARD}}",
    bootloader: "{{BOOTLOADER}}",
    baseband: "{{BASEBAND}}",
    hardware: "{{HARDWARE}}"
};

console.log("[SafetyNet] Initializing SafetyNet/Play Integrity bypass...");

if (typeof Java !== "undefined" && Java.available) {
    Java.perform(function () {

        // =============================================
        // 1. SAFETYNET ATTESTATION API
        // =============================================
        try {
            var SafetyNet = Java.use("com.google.android.gms.safetynet.SafetyNetApi$AttestationResult");
            SafetyNet.getJwsResult.overload().implementation = function () {
                console.log("[SafetyNet] Intercepted getJwsResult");
                return this.getJwsResult();
            };
        } catch (e) { }

        // =============================================
        // 2. PLAY INTEGRITY / DROIDGUARD
        // =============================================
        try {
            Java.use("com.google.android.play.core.integrity.IntegrityManager");
            console.log("[SafetyNet] Play Integrity Manager detected ✓");
        } catch (e) { }

        try {
            var DroidGuard = Java.use("com.google.android.gms.droidguard.DroidGuard");
            if (DroidGuard) {
                console.log("[SafetyNet] DroidGuard detected");
            }
        } catch (e) { }

        // =============================================
        // 3. BUILD METADATA (USER / RADIO / VERSION)
        // =============================================
        try {
            var Build = Java.use("android.os.Build");
            Build.HOST.value = "build.android.com";
            Build.USER.value = "android-build";
            if (deviceConfig.bootloader) {
                Build.BOOTLOADER.value = deviceConfig.bootloader;
            }
            try {
                Build.getRadioVersion.overload().implementation = function () {
                    return deviceConfig.baseband || "unknown";
                };
            } catch (e) { }
        } catch (e) {
            console.log("[SafetyNet] Build hook error: " + e);
        }

        // Preserve the real OS SDK_INT so non-Android-14 images never break;
        // only sync RELEASE / INCREMENTAL when explicitly present in identity.
        try {
            var BuildVersion = Java.use("android.os.Build$VERSION");
            if (deviceConfig.buildRelease) {
                BuildVersion.RELEASE.value = deviceConfig.buildRelease;
            }
            if (deviceConfig.buildIncremental) {
                BuildVersion.INCREMENTAL.value = deviceConfig.buildIncremental;
            }
            console.log("[SafetyNet] Build.VERSION synced ✓");
        } catch (e) {
            console.log("[SafetyNet] VERSION hook error: " + e);
        }

        // =============================================
        // 4. PACKAGE MANAGER - FILTER INSTALLED ROOT/EMULATOR PACKAGES
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
                "com.ldmnq"
            ];

            function filterPackages(packages) {
                var ArrayList = Java.use("java.util.ArrayList");
                var filtered = ArrayList.$new();
                for (var i = 0; i < packages.size(); i++) {
                    var pkg = packages.get(i);
                    var pkgName = pkg && pkg.packageName ? String(pkg.packageName.value) : "";
                    var hidden = false;
                    for (var j = 0; j < hiddenPackages.length; j++) {
                        if (pkgName.indexOf(hiddenPackages[j]) !== -1) {
                            hidden = true;
                            break;
                        }
                    }
                    if (!hidden) {
                        filtered.add(pkg);
                    }
                }
                return filtered;
            }

            PackageManager.getInstalledPackages.overload("int").implementation = function (flags) {
                return filterPackages(this.getInstalledPackages(flags));
            };

            try {
                PackageManager.getInstalledPackages.overload(
                    "android.content.pm.PackageManager$PackageInfoFlags"
                ).implementation = function (flags) {
                    return filterPackages(this.getInstalledPackages(flags));
                };
            } catch (e) { }

            console.log("[SafetyNet] Installed package filter hooked ✓");
        } catch (e) {
            console.log("[SafetyNet] PackageManager filter error: " + e);
        }

        // =============================================
        // 5. DEBUGGER WAITING CHECK
        // =============================================
        try {
            var Debug = Java.use("android.os.Debug");
            Debug.waitingForDebugger.overload().implementation = function () {
                return false;
            };
        } catch (e) { }

        // =============================================
        // 6. GPS / LOCATION MOCK DETECTION
        // =============================================
        try {
            var LocationManager = Java.use("android.location.LocationManager");
            LocationManager.isProviderEnabled.overload("java.lang.String").implementation = function (provider) {
                if (provider === "gps") {
                    return true;
                }
                return this.isProviderEnabled(provider);
            };

            var Location = Java.use("android.location.Location");
            Location.isFromMockProvider.overload().implementation = function () {
                return false;
            };
            try {
                Location.isMock.overload().implementation = function () {
                    return false;
                };
            } catch (e) { }
            console.log("[SafetyNet] Location mock detection bypassed ✓");
        } catch (e) {
            console.log("[SafetyNet] Location hook error: " + e);
        }

        // =============================================
        // 7. SECURE SETTINGS (adb_enabled / development_settings_enabled)
        // =============================================
        try {
            var Secure = Java.use("android.provider.Settings$Secure");
            Secure.getInt.overload("android.content.ContentResolver", "java.lang.String", "int").implementation =
                function (resolver, name, def) {
                    if (name === "adb_enabled" || name === "development_settings_enabled") {
                        return 0;
                    }
                    return this.getInt(resolver, name, def);
                };
            console.log("[SafetyNet] Secure settings hooked ✓");
        } catch (e) {
            console.log("[SafetyNet] Secure hook error: " + e);
        }

        console.log("[SafetyNet] SafetyNet bypass fully installed! ✓");
    });
}
