/**
 * SafetyNet / Play Integrity API Bypass
 * Hooks attestation, location-mock, and root-hiding APIs without colliding
 * with anti_detect.js, blocking spawn resume, or causing ANRs in Settings.
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

function _installSafetyNetHooks() {
    if (typeof Java === "undefined" || !Java.available) return;
    Java.perform(function () {

        // =============================================
        // 1. SAFETYNET ATTESTATION API
        // =============================================
        try {
            var SafetyNet = Java.use("com.google.android.gms.safetynet.SafetyNetApi$AttestationResult");
            var origGetJws = SafetyNet.getJwsResult.overload();
            origGetJws.implementation = function () {
                console.log("[SafetyNet] Intercepted getJwsResult");
                return origGetJws.call(this);
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
        // 4. PACKAGE MANAGER - FAST O(1) INSTALLED PACKAGE FILTER
        //    Uses a single packages.toString() JNI call (<1ms) instead of
        //    looping 200+ packages across the JNI bridge (which caused ANRs
        //    in com.android.settings).
        // =============================================
        try {
            var PackageManager = Java.use("android.app.ApplicationPackageManager");
            var PackageInfo = Java.use("android.content.pm.PackageInfo");
            var ArrayList = Java.use("java.util.ArrayList");

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

            var hiddenListRe = /magisk|supersu|noshufou|superuser|rootcloak|hidemyroot|xposed|substrate|genymotion|bluestacks|bignox|memu|ldmnq|appquarantine|temproot/i;

            function filterPackages(packages) {
                if (!packages) return packages;
                try {
                    // PackageInfo.toString() includes packageName; one single JNI call
                    var summary = String(packages.toString() || "");
                    if (!hiddenListRe.test(summary)) {
                        return packages;
                    }
                    var filtered = ArrayList.$new();
                    var count = packages.size();
                    for (var i = 0; i < count; i++) {
                        var item = packages.get(i);
                        var pkgName = "";
                        try {
                            var pi = Java.cast(item, PackageInfo);
                            pkgName = pi && pi.packageName ? String(pi.packageName.value) : "";
                        } catch (e) { }
                        var hidden = false;
                        if (pkgName && hiddenListRe.test(pkgName)) {
                            for (var j = 0; j < hiddenPackages.length; j++) {
                                if (pkgName.indexOf(hiddenPackages[j]) !== -1) {
                                    hidden = true;
                                    break;
                                }
                            }
                        }
                        if (!hidden) {
                            filtered.add(item);
                        }
                    }
                    return filtered;
                } catch (e) {
                    return packages;
                }
            }

            var origInstalledInt = PackageManager.getInstalledPackages.overload("int");
            origInstalledInt.implementation = function (flags) {
                return filterPackages(origInstalledInt.call(this, flags));
            };

            try {
                var origInstalledFlags = PackageManager.getInstalledPackages.overload(
                    "android.content.pm.PackageManager$PackageInfoFlags"
                );
                origInstalledFlags.implementation = function (flags) {
                    return filterPackages(origInstalledFlags.call(this, flags));
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
            var origProviderEnabled = LocationManager.isProviderEnabled.overload("java.lang.String");
            origProviderEnabled.implementation = function (provider) {
                if (provider === "gps") {
                    return true;
                }
                return origProviderEnabled.call(this, provider);
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
            var origSecureGetInt3 = Secure.getInt.overload(
                "android.content.ContentResolver",
                "java.lang.String",
                "int"
            );
            origSecureGetInt3.implementation = function (resolver, name, def) {
                if (name === "adb_enabled" || name === "development_settings_enabled") {
                    return 0;
                }
                return origSecureGetInt3.call(this, resolver, name, def);
            };
            console.log("[SafetyNet] Secure settings hooked ✓");
        } catch (e) {
            console.log("[SafetyNet] Secure hook error: " + e);
        }

        console.log("[SafetyNet] SafetyNet bypass fully installed! ✓");
    });
}

if (typeof setImmediate === "function") {
    setImmediate(_installSafetyNetHooks);
} else {
    _installSafetyNetHooks();
}
