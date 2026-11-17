"""
AntiDetect Emulator - Web GUI Dashboard

Launch-path fixes vs. original:
- ``POST /api/devices/<name>/launch`` ran the full launch/boot wait inside
  the request, so the browser request timed out (or looked frozen) and the
  real failure was lost. Launching now runs in a background thread and the
  UI polls ``GET /api/devices/<name>/status`` for progress, errors and the
  emulator log tail.
- Antidetect / Frida endpoints used ``SystemSpoofer()``/``HookManager()``
  with the hardcoded ``emulator-5554`` default, so they hit the wrong
  device whenever more than one emulator was running. They now use the
  serial recorded for that device.
- Added ``GET /api/sdk`` so the dashboard can explain a missing SDK.
"""
from datetime import datetime
import threading
import time
from pathlib import Path

from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit

socketio = SocketIO()

# name -> {"state": "starting"|"booting"|"running"|"error", "error": str|None}
_launch_state = {}
_launch_lock = threading.Lock()

# Background SDK install state (see POST /api/sdk/install).
_sdk_install = {"running": False, "lines": [], "error": None, "ready": None}
_sdk_install_lock = threading.Lock()

# Background frida-server install state (see POST /api/frida/install).
_frida_install = {"running": False, "lines": [], "error": None, "ok": None}
_frida_install_lock = threading.Lock()


def _set_state(name, **kwargs):
    with _launch_lock:
        state = _launch_state.setdefault(name, {"state": "idle", "error": None})
        state.update(kwargs)


def create_app():
    """Create Flask application"""
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "antidetect-secret-key"

    socketio.init_app(app, cors_allowed_origins="*")

    # Import here to avoid circular imports
    from core.device_manager import DeviceManager, LaunchCancelledError
    from profiles.device_profiles import get_all_models, search_models
    import config

    manager = DeviceManager()

    def _device_serial(device):
        """Serial of the *running* emulator for this device (None if not up)."""
        try:
            return manager.get_device_serial(device["name"])
        except Exception:
            return None

    _restore_state = {"running": False, "done": False, "results": []}

    def _restore_pending_in_background(reason="startup"):
        """Re-apply spoofing/hooks that an app or emulator restart lost.

        Runs in a daemon thread so the first dashboard load is never blocked,
        and only once per app run (the dashboard also exposes it as an
        explicit action and on every stale-device card).
        """
        if _restore_state["running"]:
            return
        _restore_state["running"] = True

        def run():
            try:
                print(f"[GUI] Checking for devices that need their spoofing/hooks "
                      f"restored ({reason}) ...")
                # autorecover also re-evaluates the airplane gate, so a device
                # found unspoofed on a live boot has its radios closed here
                # (not only when the spoof was applied last).
                results = manager.autorecover_all()
                _restore_state["results"] = results
                for result in results:
                    socketio.emit("restore_finished", result)
                if results:
                    print(f"[GUI] Restored {sum(1 for r in results if r.get('success'))} "
                          f"of {len(results)} device(s).")
            except Exception as exc:  # never break the dashboard
                print(f"[GUI] Background restore skipped: {exc}")
            finally:
                _restore_state["running"] = False
                _restore_state["done"] = True

        threading.Thread(target=run, daemon=True, name="antidetect-restore").start()

    @app.route("/")
    def index():
        """Main dashboard page"""
        _restore_pending_in_background()
        return render_template("index.html")

    @app.route("/api/devices")
    def get_devices():
        """Get all devices (each with the raw launch-state so the UI can show
        'Starting…' while the automatic spoof/hooks chain still runs)."""
        devices = manager.list_devices()
        with _launch_lock:
            snapshot = {k: dict(v) for k, v in _launch_state.items()}
        for device in devices:
            state = (snapshot.get(device["name"]) or {}).get("state", "idle")
            # A chain state with no live chain is stale - ignore it, otherwise
            # the card shows "Starting…" (and a Stop button) for a device the
            # user already closed.
            if state in ("starting", "booting") and not manager._launch_thread_alive(device["name"]):
                state = "idle"
            device["launch_state"] = state
            if state in ("starting", "booting"):
                device["starting"] = True
                # The card draws the live boot panel from this list.
                device["boot_progress"] = manager.launch_progress(device["name"])
        return jsonify(devices)

    @app.route("/api/devices", methods=["POST"])
    def create_device():
        """Create a device - only on an explicit, complete request.

        A device is never conjured up to fill an empty dashboard: the model has
        to come from the New Device dialog (or the API caller), so the first
        phone in the list is always one somebody chose.
        """
        data = request.get_json(silent=True) or {}
        model_name = str(data.get("model") or "").strip()
        if not model_name:
            return jsonify({
                "success": False,
                "error": (
                    "Pick a phone model first - the app never creates a device "
                    "on its own. Use the New Device dialog or "
                    "`python main.py create -m \"Samsung Galaxy S24\"`."
                ),
            }), 400
        # Camera defaults saved in ⚙️ Settings apply when the form leaves
        # the camera fields empty.
        saved = config.load_settings()
        camera_front = (data.get("camera_front") or "").strip() or saved.get("camera_front")
        camera_back = (data.get("camera_back") or "").strip() or saved.get("camera_back")
        try:
            device = manager.create_device(
                model=model_name,
                android_version=str(data.get("android_version", "14")),
                name=data.get("name"),
                proxy=data.get("proxy"),
                camera_front=camera_front,
                camera_back=camera_back,
                image_variant=data.get("image_variant"),
                custom_image=(data.get("custom_image") or "").strip() or None,
                auto_root=data.get("auto_root"),
            )
            response = {
                "success": True,
                "device": device,
                # The resolved profile can differ from the request (e.g. a
                # hardware code was used, or the name was normalized) - say so.
                "model": device.get("model"),
                "requested_model": device.get("requested_model"),
            }
            if device.get("requested_model") and device.get("requested_model") != device.get("model"):
                response["model_note"] = (
                    f"Selected '{device['requested_model']}' -> device {device['model']}"
                )
            if device.get("rootable") is False:
                response["warning"] = (
                    "Play Store images do not allow 'adb root', so frida-server and "
                    "property spoofing will not work on this device. Use the "
                    "'Google APIs (root)' image for Frida."
                )
            if device.get("camera_warning"):
                response["camera_warning"] = device.pop("camera_warning")
            return jsonify(response)
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 400

    @app.route("/api/sdk")
    def get_sdk_status():
        """Android SDK diagnostics"""
        return jsonify(config.check_sdk())

    # ------------------------------------------------------------------
    # Settings: Android SDK selection + component installation
    # ------------------------------------------------------------------
    @app.route("/api/settings")
    def get_settings():
        """Current app settings and SDK diagnostics."""
        from gui.native_dialogs import desktop_available, get_desktop_window
        info = config.check_sdk()
        info.update({
            "settings": config.load_settings(),
            "avd_home": str(config.AVD_HOME),
            "desktop_available": desktop_available(),
            "desktop_mode": get_desktop_window() is not None,
        })
        return jsonify(info)

    @app.route("/api/settings/cameras", methods=["POST"])
    def set_default_cameras():
        """Save the default camera sources used for newly created devices.

        Accepts ``emulated``, ``virtualscene``, ``webcam0``/``webcam1``/...
        (the host webcam passed into the emulator) or ``none``.
        """
        from core.device_manager import (
            CAMERA_DEFAULT_BACK, CAMERA_DEFAULT_FRONT, validate_camera_mode,
        )
        data = request.get_json(silent=True) or {}
        try:
            front = validate_camera_mode(
                data.get("camera_front"), CAMERA_DEFAULT_FRONT, "front camera")
            back = validate_camera_mode(
                data.get("camera_back"), CAMERA_DEFAULT_BACK, "back camera")
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        config.save_settings({"camera_front": front, "camera_back": back})
        return jsonify({
            "success": True,
            "camera_front": front,
            "camera_back": back,
            "message": f"Default cameras saved (front: {front}, back: {back})",
        })

    @app.route("/api/settings/auto-root", methods=["POST"])
    def set_auto_root():
        """Toggle automatic rooting (adb root after every boot)."""
        data = request.get_json(silent=True) or {}
        enabled = bool(data.get("auto_root", True))
        config.save_settings({"auto_root": enabled})
        return jsonify({
            "success": True,
            "auto_root": enabled,
            "message": ("Devices will be rooted automatically after boot"
                        if enabled else
                        "Automatic rooting disabled - spoofing/Frida will "
                        "request root on demand instead"),
        })

    @app.route("/api/settings/sdk", methods=["POST"])
    def set_sdk_directory():
        """Save a new Android SDK directory and re-resolve all tool paths."""
        data = request.get_json(silent=True) or {}
        path = (data.get("path") or "").strip().strip('"')
        if not path:
            return jsonify({"success": False, "error": "No path given"}), 400
        try:
            info = config.set_sdk_root(path)
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        return jsonify({"success": True, "sdk": info, "message": info.get("message", "")})

    @app.route("/api/settings/pick-sdk", methods=["POST"])
    def pick_sdk_directory():
        """Open a native folder picker on the machine running the app."""
        from gui.native_dialogs import pick_sdk_directory as pick
        data = request.get_json(silent=True) or {}
        initial = data.get("initial") or config.ANDROID_SDK_ROOT_STR or None
        try:
            path = pick(initialdir=initial)
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 500
        if not path:
            return jsonify({"success": False, "error": "No folder selected (or no GUI toolkit available)."}), 200
        return jsonify({"success": True, "path": path})

    @app.route("/api/sdk/install", methods=["POST"])
    def install_sdk_components():
        """Install platform-tools/emulator/system image in the background."""
        from core.sdk_setup import SdkSetup, SdkSetupError

        data = request.get_json(silent=True) or {}
        android_version = str(data.get("android_version", "14"))
        sdk_path = (data.get("path") or "").strip() or None
        image_variant = (data.get("image_variant") or "").strip() or None

        with _sdk_install_lock:
            if _sdk_install["running"]:
                return jsonify({"success": True, "state": "installing",
                                "message": "Installation already running"})
            _sdk_install.update({"running": True, "lines": [], "error": None, "ready": None})

        def progress(message):
            with _sdk_install_lock:
                _sdk_install["lines"].append(str(message))

        def run():
            try:
                if sdk_path:
                    config.set_sdk_root(sdk_path)
                setup_tool = SdkSetup(progress=progress)
                info = setup_tool.full_setup(android_version=android_version,
                                             image_variant=image_variant)
                with _sdk_install_lock:
                    _sdk_install["ready"] = bool(info.get("ready"))
                    if not info.get("ready"):
                        _sdk_install["error"] = "Missing: " + "; ".join(info.get("problems", []))
            except (SdkSetupError, ValueError) as exc:
                with _sdk_install_lock:
                    _sdk_install["error"] = str(exc)
                    _sdk_install["ready"] = False
            except Exception as exc:  # never leave the UI spinning
                with _sdk_install_lock:
                    _sdk_install["error"] = f"{type(exc).__name__}: {exc}"
                    _sdk_install["ready"] = False
            finally:
                with _sdk_install_lock:
                    _sdk_install["running"] = False
                socketio.emit("sdk_install_done", {"ready": _sdk_install["ready"]})

        threading.Thread(target=run, daemon=True).start()
        return jsonify({"success": True, "state": "installing"})

    @app.route("/api/sdk/install/status")
    def sdk_install_status():
        """Progress of the background SDK installation."""
        with _sdk_install_lock:
            state = {
                "running": _sdk_install["running"],
                "error": _sdk_install["error"],
                "ready": _sdk_install["ready"],
                "log": "\n".join(_sdk_install["lines"][-200:]),
            }
        state["sdk"] = config.check_sdk()
        return jsonify(state)

    @app.route("/api/cameras")
    def get_camera_options():
        """Camera sources offered by the emulator."""
        from core.device_manager import CAMERA_DEFAULT_BACK, CAMERA_DEFAULT_FRONT

        options = [
            {"value": "emulated", "label": "Emulated (synthetic)"},
            {"value": "virtualscene", "label": "Virtual scene (3D room)"},
            {"value": "webcam0", "label": "Host webcam0 (real camera)"},
            {"value": "webcam1", "label": "Host webcam1"},
            {"value": "usb0", "label": "External USB camera (both facings)"},
            {"value": "none", "label": "No camera"},
        ]
        # Report the host webcams this machine actually has, so the dashboard
        # can offer real independent cameras instead of a guess.
        webcams, webcam_error = [], None
        try:
            import platform
            if platform.system() == "Windows":
                webcam_error = "host webcam detection is Linux/macOS only here"
            else:
                import glob
                devices = sorted(glob.glob("/dev/video*"))
                webcams = [f"webcam{i}" for i, _ in enumerate(devices)]
                if not webcams:
                    webcam_error = "no /dev/video* devices found"
        except Exception as exc:
            webcam_error = str(exc)
        return jsonify({
            "front_default": CAMERA_DEFAULT_FRONT,
            "back_default": CAMERA_DEFAULT_BACK,
            "options": options,
            "webcams": webcams,
            "webcam_error": webcam_error,
            # Independent-sensor identity is spoofed by camera_hooks.js for
            # every device, whatever the sources are.
            "independent_identity": True,
        })

    @app.route("/api/devices/<name>/camera", methods=["POST"])
    def set_device_cameras(name):
        """Change front/back cameras of an existing device."""
        data = request.get_json(silent=True) or {}
        front = data.get("front")
        back = data.get("back")
        if front in ("", None) and back in ("", None):
            return jsonify({"success": False, "error": "Provide front and/or back"}), 400
        try:
            result = manager.set_device_cameras(
                name,
                camera_front=None if front in ("", None) else front,
                camera_back=None if back in ("", None) else back,
            )
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        result["success"] = True
        if result.get("camera_warning"):
            result["warning"] = result["camera_warning"]
        return jsonify(result)

    @app.route("/api/devices/<name>/launch", methods=["POST"])
    def launch_device(name):
        """Launch a device (asynchronously) and return immediately."""
        device = manager.get_device(name)
        if not device:
            return jsonify({"success": False, "error": f"Device not found: {name}"}), 404

        with _launch_lock:
            current = _launch_state.get(name, {})
            if current.get("state") in ("starting", "booting"):
                # A chain state without a chain behind it used to swallow the
                # click: the card stayed "Stopped", the request answered
                # success, and nothing ever launched. Only trust the state
                # while the chain is actually alive.
                if manager._launch_thread_alive(name):
                    return jsonify({
                        "success": True,
                        "state": current["state"],
                        "message": "Launch already in progress",
                    })
                _launch_state[name] = {"state": "idle", "error": None}
                current = {}

        data = request.get_json(silent=True) or {}
        enable_frida = bool(data.get("frida", False))
        wait_for_boot = bool(data.get("wait", True))
        _set_state(name, state="starting", error=None)
        # Fresh progress record per attempt: a previous failure or a finished
        # boot must never be shown as this launch's state.
        manager.begin_launch_progress(name)
        manager.report_launch_progress(
            name, "preparing", "Launch requested - preparing the emulator"
        )

        def _on_hooks_done(dev_name, applied, error):
            socketio.emit(
                "hooks_updated",
                {"name": dev_name, "applied": bool(applied), "error": error},
            )

        # Claim the launch epoch *before* the thread starts: a Stop that lands
        # while the thread is still starting up must cancel this chain, not be
        # silently overwritten by it.
        launch_token = manager.begin_launch_token(name)

        def run():
            try:
                manager.set_launch_error(name, None)  # clear previous failure
                if wait_for_boot:
                    _set_state(name, state="booting")
                manager.launch_device(
                    name,
                    enable_frida=enable_frida,
                    wait_for_boot=wait_for_boot,
                    on_hooks_complete=_on_hooks_done,
                    launch_token=launch_token,
                )
                _set_state(name, state="running", error=None)
                manager.set_launch_finished(name, "running")
                socketio.emit("device_launched", {"name": name})
            except LaunchCancelledError:
                # Stop was pressed while this chain was still booting. That is
                # not a failure to report, it is what the user asked for.
                manager.clear_launch_progress(name)
                _set_state(name, state="idle", error=None)
                manager.set_launch_finished(name, "stopped")
            except Exception as exc:  # surfaced to the UI, not swallowed
                manager.fail_launch_progress(name, str(exc))
                _set_state(name, state="error", error=str(exc))
                manager.set_launch_finished(name, "error")
                # Persist so the dashboard card keeps showing why it failed
                # (the toast alone vanishes after a few seconds).
                manager.set_launch_error(name, str(exc))
                socketio.emit("device_launch_failed", {"name": name, "error": str(exc)})
            finally:
                # Whatever happened, this chain is over: forget it so the next
                # Launch click is never answered with "already in progress".
                manager.set_launch_finished(name, "idle")

        manager.set_launch_running(name)
        threading.Thread(target=run, daemon=True).start()
        return jsonify({"success": True, "state": "starting"})

    @app.route("/api/devices/<name>/status")
    def launch_status(name):
        """Launch progress, last error, and emulator log tail for a device."""
        device = manager.get_device(name)
        if not device:
            return jsonify({"error": "Device not found"}), 404
        with _launch_lock:
            state = dict(_launch_state.get(name, {"state": "idle", "error": None}))
            # Never report a boot that has no chain behind it: a leftover
            # "booting" made the card look busy forever and ate Launch clicks.
            if (state.get("state") in ("starting", "booting")
                    and not manager._launch_thread_alive(name)):
                state = {"state": "idle", "error": None}
                _launch_state[name] = dict(state)
        running = manager._is_running(name)
        # Raw chain state: launch_device may still be auto-rooting / spoofing
        # even after the emulator is up - clients need to see the difference.
        state["launch_state"] = state.get("state", "idle")
        if running and state["launch_state"] in ("idle", "starting", "booting"):
            state["state"] = "running"
        state.update({
            "name": name,
            "running": running,
            "hooks_in_flight": manager.hooks_in_flight(name),
            "hooks_applied": device.get("hooks_applied"),
            "spoof_applied": device.get("spoof_applied"),
            "auto_spoof": device.get("auto_spoof"),
            "auto_hooks": device.get("auto_hooks"),
            "runtime_stale": device.get("runtime_stale"),
            "runtime_stale_reason": device.get("runtime_stale_reason"),
            "needs_restore": device.get("needs_restore"),
            "hooks_error": device.get("hooks_error"),
            "spoof_error": device.get("spoof_error"),
            # Airplane gate: the radios stay off while the spoof is not
            # verified (an unspoofed guest broadcasts the emulator identity).
            "airplane_mode": device.get("airplane_mode"),
            "airplane_required": device.get("airplane_required"),
            "airplane_gate": device.get("airplane_gate"),
            "airplane_error": device.get("airplane_error"),
            "gpu_mode": device.get("gpu_mode"),
            "serial": _device_serial(device),
            "log_tail": manager.get_log_tail(name, lines=40),
            # Step-by-step boot progress: which phase is running, the sentence
            # describing it, elapsed seconds and a 0-100 percentage.
            "boot_progress": manager.launch_progress(name),
        })
        return jsonify(state)

    @app.route("/api/devices/<name>/stop", methods=["POST"])
    def stop_device(name):
        """Stop a device (force-cleans a lingering emulator when asked)."""
        data = request.get_json(silent=True) or {}
        force = bool(data.get("force", False))
        try:
            manager.stop_device(name)
            # Stop also cancels a launch that was still booting; clearing the
            # chain state here is what makes the next Launch click work
            # instead of being answered with "Launch already in progress".
            manager.set_launch_finished(name, "stopped")
            manager.clear_launch_progress(name)
            _set_state(name, state="idle", error=None)
            manager.set_launch_error(name, None, error_key="stop_error")
            return jsonify({"success": True, "state": "idle"})
        except Exception as e:
            # A lingering emulator is the classic "grey Launch button" cause.
            # Record it under its own key (a stop failure must not look like a
            # launch failure, and neither may erase the other) so the card
            # keeps explaining itself and offers a retry.
            manager.set_launch_error(name, str(e), error_key="stop_error")
            freed = False
            still_running = True
            if force:
                try:
                    freed = manager._force_release(name)
                except Exception:
                    freed = False
                try:
                    still_running = manager._is_running(name)
                except Exception:
                    still_running = not freed
                if freed and not still_running:
                    manager.set_launch_finished(name, "stopped")
                    _set_state(name, state="idle", error=None)
                    manager.set_launch_error(name, None, error_key="stop_error")
            if freed and not still_running:
                return jsonify({
                    "success": True,
                    "force_attempted": force,
                    "state": "idle",
                    "message": "Emulator freed with a force stop.",
                })
            return jsonify({
                "success": False,
                "force_attempted": force,
                "state": "running" if still_running else "idle",
                "still_running": bool(still_running),
                # Keep the chain state honest too: a device that is still up
                # is not "starting", and one that is gone is not "booting".
                "error": str(e),
                "message": (
                    "The emulator is still running after a force stop. Close "
                    "the other emulator holding that port (Android Studio?), "
                    "or run `python main.py doctor`."
                ),
            }), 409

    @app.route("/api/devices/<name>", methods=["DELETE"])
    def delete_device(name):
        """Delete a device"""
        try:
            manager.delete_device(name)
            _launch_state.pop(name, None)
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 400

    @app.route("/api/models")
    def get_models():
        """Get all available phone models"""
        models = get_all_models()
        return jsonify(models)

    @app.route("/api/models/search")
    def search_phone_models():
        """Search phone models"""
        query = request.args.get("q", "")
        results = search_models(query)
        return jsonify(results)

    @app.route("/api/android-versions")
    def get_android_versions():
        """Get supported Android versions"""
        versions = []
        for ver, info in config.ANDROID_VERSIONS.items():
            versions.append({
                "version": ver,
                "api": info["api"],
                "codename": info["codename"],
                "installed": config.is_system_image_installed(info),
            })
        return jsonify(versions)

    @app.route("/api/device/<name>/identity")
    def get_device_identity(name):
        """Get device identity details (including both camera sensors)"""
        device = manager.get_device(name)
        if device:
            identity = dict(device.get("identity") or {})
            if not identity.get("camera_specs") and device.get("camera_specs"):
                identity["camera_specs"] = device["camera_specs"]
            identity["camera_sources"] = device.get("camera") or {}
            return jsonify(identity)
        return jsonify({"error": "Device not found"}), 404

    # =========================================
    # ANTIDETECT API ENDPOINTS
    # =========================================

    @app.route("/api/status/adb")
    def get_adb_status():
        """Get ADB connection status"""
        import subprocess
        try:
            result = subprocess.run(
                [config.ADB_PATH, "devices", "-l"],
                capture_output=True, text=True, timeout=10
            )
            lines = (result.stdout or "").strip().split("\n")[1:]  # Skip header
            devices = []
            for line in lines:
                if line.strip():
                    parts = line.split()
                    if len(parts) >= 2:
                        devices.append({
                            "serial": parts[0],
                            "status": parts[1]
                        })
            # Only 'device' is a usable connection - 'offline'/'unauthorized'
            # entries used to light the status card green during adbd restarts.
            online = [d for d in devices if d["status"] == "device"]
            return jsonify({
                "connected": len(online) > 0,
                "online": len(online),
                "devices": devices,
                "adb_path": config.ADB_PATH,
            })
        except Exception as e:
            return jsonify({
                "connected": False,
                "error": str(e),
                "adb_path": config.ADB_PATH,
            })

    @app.route("/api/status/frida")
    def get_frida_status():
        """Frida server status for the given (or auto-detected) device."""
        from frida_toolkit.frida_server_manager import FridaServerManager

        serial = (request.args.get("serial") or "").strip()
        if not serial:
            devices = manager.list_devices()
            running = [d for d in devices if d.get("running")]
            for device in running + devices:
                candidate = _device_serial(device)
                if candidate:
                    serial = candidate
                    break
        if not serial:
            from core import adb

            serial = adb.detect_serial()

        fm = FridaServerManager(serial or None)
        info = fm.diagnose()
        info["success"] = True
        return jsonify(info)

    @app.route("/api/frida/install", methods=["POST"])
    def install_frida():
        """Download/push/start frida-server in the background."""
        from frida_toolkit.frida_server_manager import FridaServerManager

        data = request.get_json(silent=True) or {}
        serial = (data.get("serial") or "").strip() or None
        local_file = (data.get("local_path") or "").strip() or None

        if not serial:
            devices = manager.list_devices()
            for device in [d for d in devices if d.get("running")] + devices:
                serial = _device_serial(device)
                if serial:
                    break

        if not serial:
            # Fail fast instead of starting a doomed background install
            # (that left the UI spinning on "Installing...").
            return jsonify({
                "success": False,
                "error": "No running device found. Launch an emulator first, "
                         "then Install frida-server onto it.",
            })

        with _frida_install_lock:
            if _frida_install["running"]:
                return jsonify({"success": True, "state": "installing",
                                "message": "Installation already running"})
            _frida_install.update({"running": True, "lines": [], "error": None, "ok": None})

        def progress(message):
            with _frida_install_lock:
                _frida_install["lines"].append(str(message))

        def run():
            ok = False
            message = ""
            try:
                fm = FridaServerManager(serial, progress=progress)
                ok, message = fm.install(local_file=local_file)
            except Exception as exc:  # never leave the UI spinning
                ok = False
                message = f"{type(exc).__name__}: {exc}"
            finally:
                with _frida_install_lock:
                    _frida_install["ok"] = ok
                    _frida_install["error"] = None if ok else (message or "Installation failed")
                    _frida_install["running"] = False
                socketio.emit("frida_install_done", {"ok": ok})

        threading.Thread(target=run, daemon=True).start()
        return jsonify({"success": True, "state": "installing", "serial": serial})

    @app.route("/api/frida/install/status")
    def frida_install_status():
        """Progress of the background frida-server installation."""
        from frida_toolkit.frida_server_manager import FridaServerManager

        serial = (request.args.get("serial") or "").strip() or None
        with _frida_install_lock:
            state = {
                "running": _frida_install["running"],
                "ok": _frida_install["ok"],
                "error": _frida_install["error"],
                "log": "\n".join(_frida_install["lines"][-200:]),
            }
        if not state["running"]:
            state["diagnostics"] = FridaServerManager(serial).diagnose()
        return jsonify(state)

    @app.route("/api/frida/pick-server", methods=["POST"])
    def pick_frida_server():
        """Pick an already-downloaded frida-server binary from disk."""
        from gui.native_dialogs import pick_file

        try:
            path = pick_file(title="Select the frida-server binary")
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 500
        if not path:
            return jsonify({"success": False, "error": "No file selected."})
        return jsonify({"success": True, "path": path})

    # ------------------------------------------------------------------
    # Custom system images (detected in the SDK or imported from a folder)
    # ------------------------------------------------------------------
    @app.route("/api/system-images")
    def list_system_images():
        """Built-in variants + every image physically present in the SDK."""
        builtin = []
        for variant, label in (("google_apis", "Google APIs (root capable - Frida & spoofing work)"),
                               ("google_apis_playstore", "Google APIs + Play Store (no root)")):
            builtin.append({
                "kind": "variant",
                "variant": variant,
                "label": label,
                "rootable": variant != "google_apis_playstore",
            })
        custom = []
        for pkg in config.installed_image_packages():
            try:
                parsed = config.parse_image_package(pkg)
            except ValueError:
                continue
            try:
                version = config.android_version_for_api(parsed["api"])
            except ValueError:
                version = None  # API unknown to our models table; still listed
            custom.append({
                "kind": "custom",
                "package": pkg,
                "label": f"API {parsed['api']} · {parsed['tag']} · {parsed['abi']}"
                         + (f" (Android {version})" if version else ""),
                "api": parsed["api"],
                "tag": parsed["tag"],
                "abi": parsed["abi"],
                "android_version": version,
                "rootable": config.image_is_rootable(pkg),
            })
        return jsonify({"builtin": builtin, "custom": custom})

    @app.route("/api/system-images/pick-folder", methods=["POST"])
    def pick_image_folder():
        """Native folder picker for a directory containing system.img."""
        from gui.native_dialogs import pick_sdk_directory

        try:
            path = pick_sdk_directory(
                title="Select the folder containing system.img (e.g. an extracted image)",
            )
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 500
        if not path:
            return jsonify({"success": False, "error": "No folder selected."})
        return jsonify({"success": True, "path": path})

    @app.route("/api/system-images/import", methods=["POST"])
    def import_system_image():
        """Copy a folder containing system.img into the SDK's system-images.

        Body: {source_dir, api_level, tag, abi} — destination becomes
        system-images;android-<api>;<tag>;<abi>, which avdmanager accepts.
        """
        import shutil as _shutil

        data = request.get_json(silent=True) or {}
        source = Path(str(data.get("source_dir") or ""))
        try:
            api = int(data.get("api_level") or "")
        except (TypeError, ValueError):
            return jsonify({"success": False,
                            "error": "Pick the API level this image matches."}), 400
        tag = str(data.get("tag") or "").strip()
        abi = str(data.get("abi") or "").strip().lower()
        if tag not in ("google_apis", "default", "google_apis_playstore",
                       "android-desktop"):
            return jsonify({"success": False,
                            "error": f"Tag must be one of google_apis/default/"
                                     f"android-desktop/google_apis_playstore (got {tag or '?'})"}), 400
        if abi not in ("x86_64", "x86"):
            return jsonify({"success": False,
                            "error": "Only x86_64/x86 images run on the PC emulator"}), 400
        if not source.is_dir():
            return jsonify({"success": False,
                            "error": f"Folder not found: {source}"}), 400
        img = source / "system.img"
        try:
            valid = img.exists() and img.stat().st_size > 0
        except OSError:
            valid = False
        if not valid:
            return jsonify({"success": False,
                            "error": f"{source} has no usable system.img - "
                                     "point at the image's root folder "
                                     "(the one containing system.img, ramdisk.img, ...)"}), 400

        if config.ANDROID_SDK_ROOT is None:
            return jsonify({
                "success": False,
                "error": "Android SDK root is not configured yet. Set or install the SDK in Settings first.",
            }), 400

        dest = config.ANDROID_SDK_ROOT / "system-images" / f"android-{api}" / tag / abi
        if (dest / "system.img").exists():
            return jsonify({"success": False,
                            "error": f"That image slot already exists ({dest}); "
                                     "a system.img is already there."}), 409
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                # directory shell left over from a partial import - start clean
                _shutil.rmtree(dest)
            total = sum(f.stat().st_size for f in source.rglob("*") if f.is_file())
            _shutil.copytree(source, dest)
            copied = dest / "system.img"
            if not copied.exists() or copied.stat().st_size == 0:
                raise IOError("system.img did not survive the copy")
        except Exception as exc:
            return jsonify({"success": False, "error": f"Import failed: {exc}"}), 500

        package = f"system-images;android-{api};{tag};{abi}"
        return jsonify({
            "success": True,
            "package": package,
            "message": f"Imported ~{total // (1024 * 1024)} MB as {package}",
        })

    @app.route("/api/doctor")
    def doctor_report():
        """Full health check (also available as `python main.py doctor`)."""
        from core.doctor import run_doctor

        checks, worst = run_doctor()
        return jsonify({"success": True, "worst": worst, "checks": checks})

    @app.route("/api/adb/repair", methods=["POST"])
    def repair_adb():
        """Heal an offline device / restart the local adb server.

        ``adb root`` restarts adbd and leaves the device ``offline`` for a few
        seconds; a stale adb server (e.g. started by Android Studio with a
        different platform-tools version) can leave it stuck there. This runs
        ``adb reconnect`` and, if needed, restarts the adb server.
        """
        from core import adb

        data = request.get_json(silent=True) or {}
        serial = (data.get("serial") or "").strip() or None
        if not serial:
            for device in manager.list_devices():
                serial = _device_serial(device)
                if serial:
                    break
        if not serial:
            serial = adb.detect_serial()

        ok, detail = adb.recover(serial, timeout=45)
        state = adb.get_state(serial) if serial else None
        booted, ready_state, ready_detail = False, state, detail
        if serial and state == "device":
            booted, ready_state, ready_detail = adb.wait_until_ready(serial, timeout=60)
        adb_text, adb_version = adb.version()
        return jsonify({
            "success": bool(ok and state == "device" and booted),
            "serial": serial,
            "state": ready_state or state,
            "booted": bool(booted),
            "detail": ready_detail or detail,
            "adb_version": adb_text,
            "devices": adb.device_listing(),
        })

    @app.route("/api/adb/status")
    def adb_status():
        """adb version + raw device listing (diagnostics)."""
        from core import adb

        text, parsed = adb.version()
        return jsonify({
            "path": config.ADB_PATH,
            "available": adb.adb_available(),
            "version": text,
            "version_tuple": list(parsed) if parsed else None,
            "devices": adb.devices(),
            "listing": adb.device_listing(),
        })

    @app.route("/api/devices/<name>/diagnose")
    def diagnose_device(name):
        """Why doesn't adb see this device's emulator? (pid/console/adb/log)."""
        if not manager._load_device(name):
            return jsonify({"error": "Device not found"}), 404
        return jsonify(manager.diagnose_device(name))

    def _still_starting(name):
        """409 when a launch (and its automatic spoof+hooks) is in flight -
        two overlapping spoof passes used to push properties concurrently
        and double-restart adbd, wasting a minute or worse."""
        with _launch_lock:
            state = (_launch_state.get(name) or {}).get("state")
        if state in ("starting", "booting"):
            return jsonify({
                "success": False,
                "error": f"'{name}' is still launching - wait for the automatic "
                         "spoof/hooks to finish, then retry.",
            }), 409
        return None

    @app.route("/api/devices/<name>/apply-antidetect", methods=["POST"])
    def apply_antidetect(name):
        """Apply system + hardware spoofing to the running emulator."""
        from core import adb
        from spoof.hardware_spoof import HardwareSpoofer
        from spoof.system_spoof import SystemSpoofer

        device = manager.get_device(name)
        if not device:
            return jsonify({"success": False, "error": "Device not found"}), 404

        blocking = _still_starting(name)
        if blocking is not None:
            return blocking

        data = request.get_json(silent=True) or {}
        wait_seconds = int(data.get("wait", 60))

        serial = _device_serial(device)
        if not serial:
            diag = manager.diagnose_device(name)
            if diag["pid_alive"] and not diag["console_avd"] and not diag["consoles_found"]:
                # Process alive, but no emulator console answers: it is stuck
                # (error dialog, GPU/accel failure, ...) rather than running.
                hint = (
                    f"A process for '{name}' is alive (pid {diag['pid']}) but no "
                    "emulator console responds - it is stuck before boot. Check the "
                    f"log: {diag['log_path']}. Last lines: "
                    + " | ".join(diag["log_tail"].splitlines()[-3:])
                )
            elif diag["consoles_found"]:
                found = ", ".join(f"port {p} (AVD {a})" for p, a in diag["consoles_found"].items())
                hint = (f"Found emulator console(s): {found} - none runs the AVD "
                        f"'{diag['avd_name']}'. ")
            else:
                hint = "No emulator process, console or adb device found - it is not running. "
            return jsonify({
                "success": False,
                "error": (
                    f"No running emulator matches '{name}' "
                    f"(AVD {diag['avd_name'] or '?'}). "
                    f"adb reports: {diag['adb_describe']}. " + hint
                    + "Launch the device from the dashboard (or let START.bat do it), "
                      "wait for the home screen, then retry."
                ),
            }), 409

        # Wait for the emulator to boot - and heal it when adb reports
        # "offline". That state is normal for a few seconds right after
        # `adb root` (adbd restarts) and used to abort spoofing immediately.
        ready, state, detail = adb.wait_until_ready(serial, timeout=max(30, wait_seconds))
        if not ready:
            adb_text, _ = adb.version()
            return jsonify({
                "success": False,
                "serial": serial,
                "state": state,
                "error": (
                    f"{serial} is not ready to spoof (state: {state or 'not connected'}"
                    + (f" - {detail}" if detail else "")
                    + "). "
                    + ("Accept the USB debugging prompt on the device."
                       if state == "unauthorized" else
                       "Press 'Repair ADB' in Settings, then retry. If it keeps "
                       "happening, the platform-tools in this SDK are probably too "
                       "old for the system image - update 'platform-tools'. adb says: "
                       + adb.device_listing().replace(chr(10), " | "))
                    + (f" [{adb_text.splitlines()[0]}]" if adb_text else "")
                ),
            }), 409

        identity = device.get("identity", {})
        spoofer = HardwareSpoofer(device_serial=serial)
        try:
            # Apply HardwareSpoofer FIRST so any zygote reload or reboot
            # completes before SystemSpoofer writes runtime settings/props.
            report = spoofer.spoof_all(identity)
            SystemSpoofer(device_serial=serial).apply_all_spoofing(identity)
        except Exception as exc:
            return jsonify({"success": False, "serial": serial,
                            "error": f"Spoofing failed on {serial}: {exc}"}), 500

        failed = (report or {}).get("failed") or []
        live = manager._load_device(name) or device
        live["spoof_report"] = report or None
        # Remember that this device is spoofed *and* that it should stay
        # spoofed: the values live in RAM (resetprop) and would otherwise be
        # lost on the next emulator reboot / app restart with nothing to
        # restore them from.
        live["spoof_applied"] = not failed
        live["spoof_applied_at"] = datetime.now().isoformat()
        live["auto_spoof"] = True
        if spoofer.reloaded_runtime and live.get("hooks_applied"):
            # Zygote restart / reboot detached existing app Frida sessions.
            live["hooks_applied"] = False
        if failed:
            live["spoof_error"] = (
                f"Could not change props {sorted(failed)}; the rest is verified."
            )
        else:
            live.pop("spoof_error", None)
        manager._save_device(live)
        if not failed:
            manager._remember_boot_id(live, serial)
            # Verified: the spoofed identity is live, so the radios may come
            # back on (they were held off while the device was unspoofed).
            manager.enforce_airplane_gate(name, serial, force_off=True)
        else:
            # Not verified: hold the radios off instead of broadcasting the
            # emulator's own identity.
            manager.enforce_airplane_gate(name, serial)

        gpu = (report or {}).get("gpu") or {}
        summary = (f"All {len((report or {}).get('verified', {}))} props verified "
                   f"({(report or {}).get('method')})" if report else "")
        if gpu.get("renderer"):
            summary += f" GPU: {gpu['vendor']} {gpu['renderer']}"
        reload_note = (
            " Click Hooks if you need Frida re-attached after reload."
            if spoofer.reloaded_runtime else ""
        )
        if failed:
            return jsonify({
                "success": True, "serial": serial, "spoof_report": report,
                "gpu": gpu or None,
                "message": f"Antidetect applied; BUT props {sorted(failed)} could not "
                           "be changed on this image (Frida hooks still cover apps).",
            })
        return jsonify({
            "success": True, "serial": serial, "spoof_report": report,
            "gpu": gpu or None,
            "message": f"Antidetect applied to {serial}! {summary}.{reload_note}",
        })

    @app.route("/api/devices/<name>/apply-frida-hooks", methods=["POST"])
    def apply_frida_hooks(name):
        """Inject Frida hooks into Settings (and running GMS) on the emulator."""
        from core import adb
        from core.hook_manager import HookManager

        device = manager.get_device(name)
        if not device:
            return jsonify({"success": False, "error": "Device not found"}), 404

        blocking = _still_starting(name)
        if blocking is not None:
            return blocking
        if manager.hooks_in_flight(name):
            return jsonify({
                "success": False,
                "error": f"Hooks for '{name}' are already being applied in the "
                         "background - wait for them to finish.",
            }), 409

        serial = _device_serial(device)
        if not serial:
            diag = manager.diagnose_device(name)
            hint = ""
            if diag["pid_alive"] and not diag["console_avd"] and not diag["consoles_found"]:
                hint = (f" A process for '{name}' is alive but stuck before boot "
                        f"(no emulator console answers) - check {diag['log_path']}.")
            elif diag["consoles_found"]:
                found = ", ".join(f"port {p} (AVD {a})" for p, a in diag["consoles_found"].items())
                hint = f" Emulator console(s) alive: {found}."
            return jsonify({
                "success": False,
                "error": (
                    f"No running emulator matches '{name}'. "
                    f"adb reports: {diag['adb_describe']}." + hint
                ),
            }), 409

        hooks = HookManager(device_serial=serial)
        if not hooks.start_frida_server():
            return jsonify({
                "success": False,
                "serial": serial,
                "error": (
                    f"Could not start frida-server on {serial}: "
                    + (hooks.last_error or "unknown error")
                ),
            })

        identity = device.get("identity", {})
        success = hooks.inject_system_wide(identity)

        if success:
            from core.hook_manager import active_sessions_for
            manager.start_simulators(name, serial, device.get("model", "Samsung Galaxy S24"))
            sessions = active_sessions_for(serial)
            try:
                live = manager._load_device(name)
                if live is not None:
                    live["hooks_applied"] = True
                    live["hooks_applied_at"] = datetime.now().isoformat()
                    live.pop("hooks_error", None)
                    # Intent: keep the hooks across reboots/restarts.
                    live["auto_hooks"] = True
                    manager._remember_boot_id(live, serial)
                    manager._save_device(live)
            except Exception:
                pass
            socketio.emit("hooks_updated", {"name": name, "applied": True, "error": None})
            return jsonify({
                "success": True,
                "serial": serial,
                "sessions": sessions,
                "message": (
                    f"Frida hooks active ({', '.join(sessions)})"
                    if sessions else
                    "Frida server ready for target apps (Settings kept native & crash-free)"
                ),
            })

        err_msg = "Failed to apply hooks: " + (
            hooks.last_error or "injection came back unsuccessful"
        )
        try:
            live = manager._load_device(name)
            if live is not None:
                live["hooks_applied"] = False
                live["hooks_error"] = err_msg
                manager._save_device(live)
        except Exception:
            pass
        socketio.emit("hooks_updated", {"name": name, "applied": False, "error": err_msg})
        return jsonify({
            "success": False,
            "serial": serial,
            "error": err_msg,
            "message": err_msg,
        })

    @app.route("/api/devices/<name>/restore", methods=["POST"])
    def restore_device_state(name):
        """Re-apply the spoofing/hooks this device asked to keep.

        The identity spoof lives in RAM (resetprop) and frida-server is a
        process, so an emulator reboot - or restarting the app - used to lose
        both with nothing to put them back. This endpoint restores exactly
        what was applied before, and the dashboard calls it automatically for
        every device found in a stale state.
        """
        if not manager._load_device(name):
            return jsonify({"success": False, "error": "Device not found"}), 404
        blocking = _still_starting(name)
        if blocking is not None:
            return blocking
        if manager.hooks_in_flight(name):
            return jsonify({
                "success": False,
                "error": f"Hooks for '{name}' are already being applied - wait for them to finish.",
            }), 409
        try:
            result = manager.restore_device(name)
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 500
        if result.get("success"):
            socketio.emit("hooks_updated", {
                "name": name, "applied": bool(result.get("hooks")), "error": None,
            })
        return jsonify(result)

    @app.route("/api/devices/<name>/restore-intent", methods=["POST"])
    def set_restore_intent(name):
        """Choose whether spoofing/hooks are re-applied automatically."""
        data = request.get_json(silent=True) or {}
        spoof = data.get("spoof")
        hooks = data.get("hooks")
        try:
            device = manager.set_restore_intent(
                name,
                spoof=None if spoof is None else bool(spoof),
                hooks=None if hooks is None else bool(hooks),
            )
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 404
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 500
        on = []
        off = []
        for label, wanted in (
            ("spoofing", manager.wants_spoof(device)),
            ("Frida hooks", manager.wants_hooks(device)),
        ):
            (on if wanted else off).append(label)
        message = "Restore on restart: " + (
            (f"{' + '.join(on)} on" if on else "")
            + ("; " if on and off else "")
            + (f"{' + '.join(off)} off" if off else "")
        )
        return jsonify({
            "success": True,
            "auto_spoof": device.get("auto_spoof"),
            "auto_hooks": device.get("auto_hooks"),
            "wants_spoof": manager.wants_spoof(device),
            "wants_hooks": manager.wants_hooks(device),
            "message": message,
        })

    @app.route("/api/restore-pending", methods=["POST"])
    def restore_pending():
        """Restore every running device whose spoofing/hooks were lost."""
        try:
            results = manager.restore_all_pending()
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 500
        return jsonify({
            "success": True,
            "restored": sum(1 for r in results if r.get("success")),
            "results": results,
            "message": (
                f"Restored {sum(1 for r in results if r.get('success'))} device(s)"
                if results else "Nothing to restore - every device is up to date."
            ),
        })

    @app.route("/api/devices/<name>/autorecover", methods=["POST"])
    def autorecover_device(name):
        """Re-apply whatever this device asked to keep (spoof + hooks + radios).

        This is the self-healing action behind the card's "↻ Recover" button and
        the automatic dashboards sweep - it never reports success for a value
        the device did not confirm.
        """
        if not manager._load_device(name):
            return jsonify({"success": False, "error": "Device not found"}), 404
        blocking = _still_starting(name)
        if blocking is not None:
            return blocking
        if manager.hooks_in_flight(name):
            return jsonify({
                "success": False,
                "error": f"Hooks for '{name}' are already being applied - wait for them to finish.",
            }), 409
        try:
            result = manager.autorecover(name)
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 500
        socketio.emit("hooks_updated", {
            "name": name, "applied": bool(result.get("hooks")),
            "error": result.get("hooks_error"),
        })
        return jsonify(result)

    @app.route("/api/devices/<name>/airplane", methods=["POST"])
    def set_airplane_mode(name):
        """Turn airplane mode on/off, or re-apply the automatic gate.

        ``{"auto": true}`` re-evaluates the gate (radios off while the spoof is
        not verified); ``{"enable": true|false}`` sets it explicitly.
        """
        if not manager._load_device(name):
            return jsonify({"success": False, "error": "Device not found"}), 404
        data = request.get_json(silent=True) or {}
        try:
            if data.get("auto"):
                state, message = manager.enforce_airplane_gate(name)
                ok = state != "unavailable"
            else:
                if "enable" not in data:
                    return jsonify({"success": False,
                                    "error": "Pass {'enable': true|false} or {'auto': true}."}), 400
                ok, message = manager.set_airplane_mode(name, bool(data["enable"]))
                state = "on" if data["enable"] else "off"
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 409
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 500
        device = manager.get_device(name) or {}
        return jsonify({
            "success": bool(ok),
            "state": state,
            "airplane_mode": device.get("airplane_mode"),
            "airplane_gate": device.get("airplane_gate"),
            "message": message,
        })

    @app.route("/api/devices/<name>/hook-app", methods=["POST"])
    def hook_target_app(name):
        """Inject anti-detection Frida hooks into any package on a running emulator."""
        from core.hook_manager import (
            HookManager,
            PROTECTED_SYSTEM_PACKAGES,
            active_sessions_for,
            renderer_sessions_for,
        )

        device = manager.get_device(name)
        if not device:
            return jsonify({"success": False, "error": "Device not found"}), 404

        data = request.get_json(silent=True) or {}
        package = str(data.get("package") or "").strip()
        spawn = bool(data.get("spawn", False))
        force = bool(data.get("force", False))
        if not package or " " in package or "/" in package:
            return jsonify({"success": False, "error": "Provide a valid Android package name (e.g. com.example.app)"}), 400

        if package in PROTECTED_SYSTEM_PACKAGES and not force:
            return jsonify({
                "success": False,
                "package": package,
                "error": (
                    f"Refusing to inject Frida into protected system app '{package}': "
                    "OS-level resetprop-rs + SystemSpoofer already spoof it natively, "
                    "and hooking system UI processes causes Android 14 crashes. "
                    "Hook your target third-party app instead (or pass force=true for "
                    "lightweight read-only Build sync)."
                ),
            }), 400

        serial = _device_serial(device)
        if not serial:
            return jsonify({"success": False, "error": f"Device '{name}' is not running."}), 409

        hooks = HookManager(device_serial=serial)
        ok = hooks.inject_package(
            package,
            device.get("identity", {}),
            spawn=spawn,
            allow_protected=force,
        )
        if ok:
            # Renderer/GPU processes are hooked by inject_package already (they
            # only exist once the app renders something, so report the count).
            renderers = renderer_sessions_for(serial, package)
            note = (
                f" ({len(renderers)} WebGL/GPU process(es) covered)"
                if renderers
                else " - open a page with WebGL, then re-inject to also cover the GPU process"
            )
            return jsonify({
                "success": True,
                "serial": serial,
                "package": package,
                "sessions": active_sessions_for(serial),
                "renderer_sessions": renderers,
                "message": f"Anti-detect hooks active in {package}{note}",
            })
        return jsonify({
            "success": False,
            "serial": serial,
            "package": package,
            "error": hooks.last_error or f"Could not hook {package}",
        }), 500

    @app.route("/api/devices/<name>/proxy", methods=["POST"])
    def update_device_proxy(name):
        """Validate, save, and (when running) live-apply or clear a device proxy."""
        data = request.get_json(silent=True) or {}
        proxy = data.get("proxy")
        try:
            result = manager.set_device_proxy(name, proxy)
            return jsonify({"success": True, **result})
        except ValueError as exc:
            status = 404 if "not found" in str(exc).lower() else 400
            return jsonify({"success": False, "error": str(exc)}), status
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 500

    @app.route("/api/devices/<name>/regenerate-identity", methods=["POST"])
    def regenerate_device_identity(name):
        """Generate a new unique device identity (IMEI, Android ID, serial, MAC, etc.)."""
        try:
            result = manager.regenerate_identity(name)
            return jsonify({"success": True, **result})
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 404
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 500

    @app.route("/api/devices/<name>/crash-log")
    def get_device_crash_log(name):
        """Return Android logcat crash buffer for a running emulator."""
        try:
            lines = max(10, min(500, int(request.args.get("lines", 80))))
        except (TypeError, ValueError):
            lines = 80
        try:
            text = manager.get_crash_log(name, lines=lines)
            return jsonify({"success": True, "name": name, "crash_log": text})
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 404

    @socketio.on("connect")
    def handle_connect():
        """Handle client connection"""
        emit("connected", {"status": "ok"})

    @socketio.on("refresh_devices")
    def handle_refresh():
        """Refresh device list"""
        devices = manager.list_devices()
        emit("devices_updated", devices)

    return app
