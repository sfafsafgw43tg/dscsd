"""
Sensor Simulator - Simulate realistic sensor data to bypass detection
"""
import random
import math
import time
import subprocess
import threading
import config


class SensorSimulator:
    """Simulate realistic sensor data for anti-detection"""
    
    def __init__(self, device_serial="emulator-5554"):
        self.device_serial = device_serial
        self.adb = config.ADB_PATH
        self._running = False
        self._stop_event = threading.Event()
        self._thread = None
    
    def _adb(self, command):
        """Execute ADB command"""
        cmd = [self.adb, "-s", self.device_serial] + command
        try:
            subprocess.run(cmd, capture_output=True, timeout=5)
        except Exception:
            pass
    
    def start_simulation(self, mode="idle"):
        """Start continuous sensor simulation"""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._simulate_loop, args=(mode,), daemon=True)
        self._thread.start()
    
    def stop_simulation(self):
        """Stop sensor simulation immediately."""
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def stop(self):
        """Alias for ``stop_simulation`` (used by DeviceManager lifecycle cleanup)."""
        self.stop_simulation()
    
    def _simulate_loop(self, mode):
        """Main simulation loop (1 Hz batched updates to avoid console/port exhaustion)."""
        while self._running and not self._stop_event.is_set():
            if mode == "idle":
                # Phone lying on table - slight vibrations
                self.simulate_idle()
            elif mode == "pocket":
                # Phone in pocket - movement patterns
                self.simulate_pocket()
            elif mode == "hand":
                # Phone in hand - slight shake
                self.simulate_in_hand()

            if self._stop_event.wait(1.0):
                break
    
    def simulate_idle(self):
        """Simulate phone lying still on table (single batched console call)."""
        ax = random.gauss(0, 0.05)
        ay = random.gauss(0, 0.05)
        az = random.gauss(9.81, 0.1)

        gx = random.gauss(0, 0.001)
        gy = random.gauss(0, 0.001)
        gz = random.gauss(0, 0.001)

        mx = random.gauss(25, 2)
        my = random.gauss(-10, 2)
        mz = random.gauss(-40, 2)

        self._send_console([
            f"sensor set acceleration:{ax:.4f}:{ay:.4f}:{az:.4f}",
            f"sensor set gyroscope:{gx:.4f}:{gy:.4f}:{gz:.4f}",
            f"sensor set magnetic-field:{mx:.4f}:{my:.4f}:{mz:.4f}",
        ], timeout=1)
    
    def simulate_pocket(self):
        """Simulate phone in pocket while walking (single batched console call)."""
        t = time.time()
        ax = random.gauss(0, 0.5) + 0.3 * math.sin(t * 4)
        ay = random.gauss(0, 0.3)
        az = random.gauss(9.81, 0.5) + 0.5 * math.sin(t * 4)

        gx = random.gauss(0, 0.05)
        gy = random.gauss(0, 0.02)
        gz = random.gauss(0, 0.03)

        self._send_console([
            f"sensor set acceleration:{ax:.4f}:{ay:.4f}:{az:.4f}",
            f"sensor set gyroscope:{gx:.4f}:{gy:.4f}:{gz:.4f}",
        ], timeout=1)
    
    def simulate_in_hand(self):
        """Simulate phone held in hand (single batched console call)."""
        ax = random.gauss(0, 0.2)
        ay = random.gauss(0, 0.2)
        az = random.gauss(9.81, 0.3)

        gx = random.gauss(0, 0.02)
        gy = random.gauss(0, 0.02)
        gz = random.gauss(0, 0.01)

        self._send_console([
            f"sensor set acceleration:{ax:.4f}:{ay:.4f}:{az:.4f}",
            f"sensor set gyroscope:{gx:.4f}:{gy:.4f}:{gz:.4f}",
        ], timeout=1)
    
    def _console_port(self):
        """Emulator console port derived from the ADB serial.

        ``emulator-5554`` -> console 5554 (ADB 5555). Previously every
        simulator hardcoded 5554, so a second emulator (5556) never got
        sensor/battery updates.
        """
        try:
            return int(str(self.device_serial).split("-")[1])
        except (IndexError, ValueError):
            return 5554

    def _send_console(self, commands, timeout=2):
        """Send commands to the emulator console (with auth if present)."""
        import socket

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect(("localhost", self._console_port()))
                try:
                    s.recv(4096)  # Welcome banner
                except Exception:
                    pass
                # Modern emulator requires auth token for console commands.
                try:
                    token_path = config.EMULATOR_CONSOLE_AUTH_TOKEN
                    if token_path.exists():
                        token = token_path.read_text(encoding="utf-8", errors="ignore").strip()
                        if token:
                            s.send(f"auth {token}\n".encode())
                            try:
                                s.recv(4096)
                            except Exception:
                                pass
                except Exception:
                    pass
                for cmd in commands:
                    s.sendall(f"{cmd}\n".encode())
                    try:
                        s.recv(4096)
                    except Exception:
                        pass
                try:
                    s.sendall(b"quit\n")
                except Exception:
                    pass
        except Exception:
            pass

    def _send_sensor_data(self, sensor_type, x, y, z):
        """Send sensor data via emulator console.

        Correct syntax is ``sensor set <name>:<x>:<y>:<z>`` (colon-joined,
        no space). The old code sent ``sensor set <name> <x>:<y>:<z>``
        which the console rejects.
        """
        sensor_map = {
            "accelerometer": "acceleration",
            "gyroscope": "gyroscope",
            "magnetometer": "magnetic-field",
        }
        sensor_name = sensor_map.get(sensor_type, sensor_type)
        self._send_console(
            [f"sensor set {sensor_name}:{x:.4f}:{y:.4f}:{z:.4f}"], timeout=1
        )

    def set_location(self, latitude, longitude, altitude=0):
        """Set GPS location"""
        self._send_console([f"geo fix {longitude} {latitude} {altitude}"])

    def simulate_battery(self, level=75, charging=False):
        """Simulate battery state"""
        status = "charging" if charging else "discharging"
        self._send_console(
            [f"power capacity {level}", f"power status {status}"]
        )


# Predefined sensor profiles for common activities
SENSOR_PROFILES = {
    "idle": {
        "description": "Phone lying on flat surface",
        "accelerometer": {"x": 0, "y": 0, "z": 9.81, "noise": 0.05},
        "gyroscope": {"x": 0, "y": 0, "z": 0, "noise": 0.001},
    },
    "pocket_walk": {
        "description": "Phone in pocket while walking",
        "accelerometer": {"x": 0, "y": 0, "z": 9.81, "noise": 1.0, "pattern": "sin"},
        "gyroscope": {"x": 0, "y": 0, "z": 0, "noise": 0.1},
    },
    "hand_browsing": {
        "description": "Phone held in hand, browsing",
        "accelerometer": {"x": 0, "y": 3, "z": 8.5, "noise": 0.3},
        "gyroscope": {"x": 0, "y": 0, "z": 0, "noise": 0.02},
    },
    "car_driving": {
        "description": "Phone in car mount",
        "accelerometer": {"x": 0, "y": 0, "z": 9.81, "noise": 0.5},
        "gyroscope": {"x": 0, "y": 0, "z": 0, "noise": 0.1},
    },
}
