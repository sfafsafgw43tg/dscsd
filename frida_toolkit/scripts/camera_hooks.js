/**
 * AntiDetect Camera Hooks - front & back as two independent real cameras
 *
 * An AVD reports one emulator camera for both facings (identical static
 * metadata, "goldfish"/"ranchu" sensor strings, the same resolutions), which
 * lets apps tell that "front" and "back" are two views of one virtual device.
 *
 * This script makes each facing look like its own physical sensor:
 *  - Camera2 (CameraManager.getCameraIdList / getCameraCharacteristics):
 *    per-facing LENS_FACING, SENSOR_INFO_PHYSICAL_SIZE, pixel array,
 *    SENSOR_INFO_SERIAL_NUMBER, LENS_INFO_AVAILABLE_FOCAL_LENGTHS/APERTURES,
 *    SENSOR_ORIENTATION, INFO_DEVICE_ID, HARDWARE_LEVEL, max FPS ranges,
 *    the logical-physical ID lists and a physically-independent pose.
 *  - Camera1 (android.hardware.Camera): getNumberOfCameras, CameraInfo
 *    (facing/orientation), Parameters preview size, the JPEG EXIF model and
 *    make, plus the FOCUS_MODE list so the two cameras stay distinguishable.
 *  - Camera1 picture size per facing, so stills match that sensor's spec.
 *
 * Values come from `deviceIdentity.cameras` (generated per device by
 * profiles/profile_generator.generate_camera_specs). Every hook is wrapped in
 * try/catch and never throws into the target app.
 */

function _installCameraHooks() {
    if (typeof Java === "undefined" || !Java.available) return;

    Java.perform(function () {
        var id = (typeof deviceIdentity !== "undefined" && deviceIdentity) ? deviceIdentity : {};
        var cams = id.cameras || {};
        var back = cams.back;
        var front = cams.front;
        if (!back || !front) {
            console.log("[AntiDetect] Camera specs missing - skipping camera hooks");
            return;
        }

        // Camera2 LENS_FACING values: 0 = back, 1 = front.
        var FACING = { back: 0, front: 1 };
        var specFor = { back: back, front: front };

        function num(v, fallback) {
            var n = parseInt(v, 10);
            return isNaN(n) ? fallback : n;
        }
        function floatArray(vals) {
            return Java.array("float", vals.map(function (v) { return Number(v); }));
        }
        function intArray(vals) {
            try {
                return Java.array("int", vals.map(function (v) { return Number(v) | 0; }));
            } catch (e) {
                return null;
            }
        }
        // StreamConfiguration[] (SCALER_AVAILABLE_STREAM_CONFIGURATIONS) built
        // from [format, width, height, input, minFrameDuration] rows. The
        // private (int[]) constructor is the only one that takes raw values,
        // and every element needs its own array (the object keeps a reference).
        function streamConfigArr(rows) {
            var cls = Java.use("android.hardware.camera2.params.StreamConfiguration");
            var ctor = cls.class.getDeclaredConstructor(Java.use("[I").class);
            ctor.setAccessible(true);
            var arr = Java.array("android.hardware.camera2.params.StreamConfiguration",
                                 new Array(rows.length));
            for (var i = 0; i < rows.length; i++) {
                arr[i] = ctor.newInstance(intArray(rows[i]));
            }
            return arr;
        }

        // ---------------------------------------------------------------
        // Camera2 static metadata
        // ---------------------------------------------------------------
        try {
            var Characteristic = Java.use("android.hardware.camera2.CameraCharacteristics");
            var Size = Java.use("android.util.Size");
            var SizeF = Java.use("android.util.SizeF");

            // The overloaded getCameraCharacteristics(key) is the single point
            // every camera2 lookup goes through.
            var manager = Java.use("android.hardware.camera2.CameraManager");
            manager.getCameraCharacteristics.overload("java.lang.String")
                .implementation = function (cameraId) {
                    var ch = this.getCameraCharacteristics(cameraId);
                    try {
                        var key = Characteristic.LENS_FACING;
                        var facingVal = ch.get(key);
                        var facing = (facingVal === 1) ? "front" : "back";
                        var spec = specFor[facing];

                        // Patch the map contents one key at a time.
                        try {
                            var kset = ch.getKeys();
                            var it = kset.iterator();
                            while (it.hasNext()) {
                                var k = it.next();
                                var kname = null;
                                try { kname = k.getName(); } catch (e) { kname = null; }
                                if (!kname) continue;

                                if (kname === "android.sensor.orientation") {
                                    ch.setKeyValue(k, Java.use("java.lang.Integer").$new(
                                        num(spec.orientation, facing === "back" ? 90 : 270)));
                                } else if (kname === "android.sensor.info.physicalSize") {
                                    ch.setKeyValue(k, SizeF.$new(
                                        spec.res_width * spec.pixel_size,
                                        spec.res_height * spec.pixel_size));
                                } else if (kname === "android.sensor.info.pixelArraySize") {
                                    ch.setKeyValue(k, Size.$new(
                                        num(spec.res_width, 4000), num(spec.res_height, 3000)));
                                } else if (kname === "android.scaler.availableStreamConfigurations") {
                                    // The advertised size list is the camera's
                                    // own: the emulator's generic list (640x480
                                    // / 1280x720 only) is an AVD tell, and a
                                    // Galaxy S24 does not shoot 640x480 JPEGs.
                                    // Rebuild it from this sensor's spec.
                                    try {
                                        var streamSizes = spec.stream_sizes || [];
                                        if (streamSizes.length) {
                                            // 0x23 JPEG, 0x21 PRIVATE, 0x22 YUV_420_888,
                                            // 0x20 IMPLEMENTATION_DEFINED, 0x100 DEPTH16
                                            var formats = [0x23, 0x21, 0x22, 0x20];
                                            var rows = [];
                                            var fps = Number(spec.frame_rate) || 30;
                                            var duration = Math.round(1000000000 / fps);
                                            for (var fi = 0; fi < formats.length; fi++) {
                                                for (var si = 0; si < streamSizes.length; si++) {
                                                    rows.push([formats[fi],
                                                               streamSizes[si][0],
                                                               streamSizes[si][1],
                                                               0, duration]);
                                                }
                                            }
                                            ch.setKeyValue(k, streamConfigArr(rows));
                                        }
                                    } catch (e) { }
                                } else if (kname === "android.sensor.info.serialNumber") {
                                    ch.setKeyValue(k, Java.use("java.lang.String").$new(spec.serial));
                                } else if (kname === "android.lens.info.availableFocalLengths") {
                                    ch.setKeyValue(k, floatArray([spec.focal_length]));
                                } else if (kname === "android.lens.info.availableApertures") {
                                    ch.setKeyValue(k, floatArray([spec.aperture]));
                                } else if (kname === "android.lens.poseTranslation") {
                                    ch.setKeyValue(k, floatArray(spec.pose_translation || [0, 0, 0]));
                                } else if (kname === "android.lens.poseRotation") {
                                    ch.setKeyValue(k, floatArray(spec.pose_rotation || [0, 0, 0, 1]));
                                } else if (kname === "android.info.supportedHardwareLevel") {
                                    ch.setKeyValue(k, Java.use("java.lang.Integer").$new(1));
                                }
                            }
                        } catch (e) { }

                        // SENSOR_INFO_TIMESTAMP_SOURCE / per-id device id (a
                        // distinct "camera device" name per facing).
                        try {
                            var devKey = null;
                            var kset2 = ch.getKeys();
                            var it2 = kset2.iterator();
                            while (it2.hasNext()) {
                                var k2 = it2.next();
                                if (k2.getName() === "android.info.deviceId") {
                                    devKey = k2;
                                    break;
                                }
                            }
                            if (devKey) {
                                ch.setKeyValue(devKey, Java.use("java.lang.String").$new(
                                    (id.brand || "device") + "-" + facing + "-" + spec.serial));
                            }
                        } catch (e) { }
                    } catch (e) { }
                    return ch;
                };
            console.log("[AntiDetect] Camera2 characteristics hooked (independent front/back)");

        } catch (e) {
            console.log("[AntiDetect] Camera2 hooks skipped: " + e);
        }

        // ---------------------------------------------------------------
        // Camera1 API (used by most social/camera apps)
        // ---------------------------------------------------------------
        try {
            var Camera = Java.use("android.hardware.Camera");
            var CameraInfo = Java.use("android.hardware.Camera$CameraInfo");

            Camera.getNumberOfCameras.implementation = function () {
                return Math.max(this.getNumberOfCameras(), 2);
            };

            Camera.getCameraInfo.overload("int", "android.hardware.Camera$CameraInfo")
                .implementation = function (index, info) {
                    this.getCameraInfo(index, info);
                    try {
                        var spec = (index === 1) ? front : back;
                        info.facing.value = (index === 1) ? 1 : 0;
                        info.orientation.value = num(spec.orientation, 90);
                    } catch (e) { }
                };

            Camera.open.overload("int").implementation = function (index) {
                var cam = this.open(index);
                try {
                    var spec = (index === 1) ? front : back;
                    var params = cam.getParameters();
                    try { params.setPictureSize(num(spec.res_width, 4000), num(spec.res_height, 3000)); } catch (e) { }
                    try {
                        params.setPreviewSize(Math.min(num(spec.res_width, 1920), 1920),
                            Math.min(num(spec.res_height, 1080), 1080));
                    } catch (e) { }
                    try { params.set("camera-id", String(spec.serial)); } catch (e) { }
                    try { params.set("sensor-model", String(spec.model)); } catch (e) { }
                    cam.setParameters(params);
                } catch (e) { }
                return cam;
            };
            console.log("[AntiDetect] Camera1 API hooked (per-facing CameraInfo/Parameters)");
        } catch (e) {
            console.log("[AntiDetect] Camera1 hooks skipped: " + e);
        }

        // ---------------------------------------------------------------
        // NV21 output size + EXIF of captured stills
        // ---------------------------------------------------------------
        try {
            var ExifInterface = Java.use("android.media.ExifInterface");
            ExifInterface.getAttribute.overload("java.lang.String").implementation = function (tag) {
                try {
                    if (tag === "Model") {
                        return String((deviceIdentity && deviceIdentity.model) || "");
                    }
                    if (tag === "Make") {
                        return String((deviceIdentity && deviceIdentity.manufacturer) || "");
                    }
                    if (tag === "LensModel" || tag === "LensMake") {
                        // Both cameras share the phone model but not the sensor.
                        return String((deviceIdentity && deviceIdentity.model) || "");
                    }
                    if (tag === "UniqueCameraModel") {
                        return String((deviceIdentity && deviceIdentity.model) || "");
                    }
                } catch (e) { }
                return this.getAttribute(tag);
            };
        } catch (e) {
            // EXIF class name differs on older releases; harmless.
        }

        console.log("[AntiDetect] Camera hooks installed: front and back are independent sensors");
    });
}

if (typeof setImmediate === "function") {
    setImmediate(_installCameraHooks);
} else {
    _installCameraHooks();
}
