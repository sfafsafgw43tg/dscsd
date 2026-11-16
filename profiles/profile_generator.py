"""
Profile Generator - Generate unique device identities with valid checksums
"""
import random
import re
import string
import hashlib
from datetime import datetime, timedelta


def generate_imei():
    """Generate valid IMEI with correct Luhn checksum"""
    # TAC (Type Allocation Code) - first 8 digits
    tac_list = [
        "35291505",  # Samsung
        "35694210",  # Samsung
        "86776502",  # Xiaomi
        "86771305",  # Xiaomi
        "35332407",  # Google Pixel
        "35260100",  # OnePlus
        "86697903",  # Huawei
    ]
    tac = random.choice(tac_list)
    
    # Serial number - next 6 digits
    serial = ''.join([str(random.randint(0, 9)) for _ in range(6)])
    
    # Calculate Luhn checksum
    imei_without_check = tac + serial
    check_digit = calculate_luhn_checksum(imei_without_check)
    
    return imei_without_check + str(check_digit)


def calculate_luhn_checksum(number_string):
    """Calculate Luhn checksum digit"""
    digits = [int(d) for d in number_string]
    odd_digits = digits[-1::-2]
    even_digits = digits[-2::-2]
    
    checksum = sum(odd_digits)
    for d in even_digits:
        checksum += sum(divmod(d * 2, 10))
    
    return (10 - (checksum % 10)) % 10


def generate_serial_number(brand="Samsung"):
    """Generate realistic serial number based on brand"""
    if brand.lower() == "samsung":
        # Samsung: R5CT + 8 chars
        prefix = random.choice(["R5CT", "RF8R", "R3CM", "R9UE"])
        chars = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        return prefix + chars
    elif brand.lower() in ["xiaomi", "redmi", "poco"]:
        # Xiaomi: numbers/letters mix
        return ''.join(random.choices(string.ascii_uppercase + string.digits, k=16))
    elif brand.lower() == "google":
        # Pixel: alphanumeric
        return ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))
    elif brand.lower() == "oneplus":
        # OnePlus format
        return "NB" + ''.join(random.choices(string.digits, k=10))
    else:
        return ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))


def generate_android_id():
    """Generate Android ID (16 hex characters)"""
    return ''.join(random.choices('0123456789abcdef', k=16))


def generate_mac_address():
    """Generate random MAC address"""
    # Use valid OUI prefixes for major vendors
    oui_list = [
        "00:1A:7D",  # Samsung
        "64:A2:F9",  # Samsung
        "9C:5C:F9",  # Xiaomi
        "28:6C:07",  # Xiaomi
        "F4:F5:E8",  # Google
        "94:65:2D",  # OnePlus
    ]
    oui = random.choice(oui_list)
    nic = ':'.join(['%02x' % random.randint(0, 255) for _ in range(3)])
    return f"{oui}:{nic}"


def generate_wifi_ssid():
    """Generate realistic nearby WiFi SSIDs"""
    prefixes = ["NETGEAR", "Linksys", "TP-LINK", "ASUS", "Home", "Apt", "Wifi", "5G"]
    suffixes = ["_5G", "_Guest", "_2.4G", "-5GHz", ""]
    
    ssids = []
    for _ in range(random.randint(3, 8)):
        prefix = random.choice(prefixes)
        suffix = random.choice(suffixes)
        number = random.randint(1, 999)
        ssids.append(f"{prefix}{number}{suffix}")
    
    return ssids


def generate_gsf_id():
    """Generate Google Services Framework ID"""
    return ''.join(random.choices('0123456789abcdef', k=16))


def generate_advertising_id():
    """Generate Google Advertising ID (UUID format)"""
    return '-'.join([
        ''.join(random.choices('0123456789abcdef', k=8)),
        ''.join(random.choices('0123456789abcdef', k=4)),
        '4' + ''.join(random.choices('0123456789abcdef', k=3)),
        random.choice(['8', '9', 'a', 'b']) + ''.join(random.choices('0123456789abcdef', k=3)),
        ''.join(random.choices('0123456789abcdef', k=12)),
    ])


def generate_build_id():
    """Generate Android build ID"""
    prefixes = ["UP1A", "UD1A", "UKQ1", "TP1A", "TQ3A"]
    return f"{random.choice(prefixes)}.{random.randint(230101, 241231)}.{random.randint(1, 99):03d}"


def generate_build_number(brand):
    """Generate brand-specific build number"""
    if brand.lower() == "samsung":
        return f"S{random.randint(900, 928)}BXXS{random.randint(1, 9)}AXL{random.randint(1, 9)}"
    elif brand.lower() in ["xiaomi", "redmi", "poco"]:
        return f"V{random.randint(14, 16)}.0.{random.randint(1, 30)}.0.UNACNXM"
    elif brand.lower() == "google":
        return f"{random.randint(10000000, 12000000)}"
    else:
        return f"RKQ1.{random.randint(200101, 241231)}.002"


def generate_gpu_renderer(brand, model_name):
    """Generate realistic GPU renderer string for device"""
    gpu_map = {
        "Samsung": {
            "S24": "Mali-G720",
            "S23": "Adreno (TM) 740",
            "S22": "Xclipse 920",
            "A54": "Mali-G68",
            "default": "Adreno (TM) 660"
        },
        "Xiaomi": {
            "14": "Adreno (TM) 750",
            "13": "Adreno (TM) 740",
            "default": "Adreno (TM) 660"
        },
        "Google": {
            "Pixel 8": "Mali-G715",
            "Pixel 7": "Mali-G710",
            "default": "Mali-G710"
        },
        "OnePlus": {
            "12": "Adreno (TM) 750",
            "11": "Adreno (TM) 740",
            "default": "Adreno (TM) 660"
        }
    }
    
    brand_gpus = gpu_map.get(brand, {"default": "Adreno (TM) 660"})
    
    for key, renderer in brand_gpus.items():
        if key != "default" and key.lower() in model_name.lower():
            return renderer
    
    return brand_gpus.get("default", "Adreno (TM) 660")


def generate_gpu_vendor(renderer):
    """Get GPU vendor from renderer string"""
    if "Adreno" in renderer:
        return "Qualcomm"
    elif "Mali" in renderer:
        return "ARM"
    elif "Xclipse" in renderer:
        return "Samsung"
    else:
        return "Qualcomm"


def generate_battery_info(brand, model_name):
    """Generate realistic battery specifications"""
    battery_map = {
        "flagship": {"capacity": 5000, "technology": "Li-poly", "voltage": 4350},
        "midrange": {"capacity": 4500, "technology": "Li-ion", "voltage": 4200},
        "budget": {"capacity": 4000, "technology": "Li-ion", "voltage": 4100}
    }
    
    # Determine tier
    if any(x in model_name.lower() for x in ["ultra", "pro", "plus", "fold", "flip"]):
        tier = "flagship"
    elif any(x in model_name.lower() for x in ["a54", "a34", "note", "nord"]):
        tier = "midrange"
    else:
        tier = "flagship"  # Default to flagship
    
    specs = battery_map[tier]
    return {
        "capacity_mah": specs["capacity"],
        "technology": specs["technology"],
        "voltage_mv": specs["voltage"],
        "health": "Good",
        "temperature": round(random.uniform(25, 32), 1)
    }


def generate_cpu_info(brand, board):
    """Generate CPU information"""
    cpu_map = {
        "kalama": {"model": "Snapdragon 8 Gen 2", "cores": 8, "hardware": "qcom"},
        "pineapple": {"model": "Snapdragon 8 Gen 3", "cores": 8, "hardware": "qcom"},
        "zuma": {"model": "Google Tensor G3", "cores": 9, "hardware": "zuma"},
        "gs201": {"model": "Google Tensor G2", "cores": 8, "hardware": "gs201"},
        "s5e9945": {"model": "Exynos 2400", "cores": 10, "hardware": "exynos"},
        "s5e9925": {"model": "Exynos 2200", "cores": 8, "hardware": "exynos"},
        "mt6985": {"model": "Dimensity 9200+", "cores": 8, "hardware": "mt6985"},
    }
    
    info = cpu_map.get(board, {"model": "Snapdragon 8 Gen 1", "cores": 8, "hardware": "qcom"})
    return info


def generate_bootloader(brand, model):
    """Generate bootloader version"""
    if brand.lower() == "samsung":
        return f"{model}XXS{random.randint(1, 9)}AW{random.choice(['A', 'B', 'C'])}{random.randint(1, 9)}"
    elif brand.lower() in ["xiaomi", "redmi", "poco"]:
        return f"V{random.randint(14, 16)}.0.{random.randint(1, 20)}.0"
    elif brand.lower() == "google":
        return f"slider-{random.randint(14, 16)}.0.0"
    elif brand.lower() == "oneplus":
        return f"{random.randint(14, 16)}.0.0.0"
    else:
        return "unknown"


def generate_baseband(brand):
    """Generate baseband/modem version"""
    if brand.lower() == "samsung":
        return f"S{random.randint(900, 928)}BXXS{random.randint(1, 9)}AW{random.choice(['A', 'B'])}{random.randint(1, 3)}"
    elif brand.lower() in ["xiaomi", "redmi", "poco"]:
        return f"MPSS.DE.{random.randint(3, 5)}.0.{random.randint(1, 3)}-{random.randint(1, 9)}"
    elif brand.lower() == "google":
        return f"g5300q-230525-{random.randint(230601, 241231)}-B-{random.randint(10000000, 12000000)}"
    else:
        return "unknown"


# ----------------------------------------------------------------------------
# Per-camera hardware specs (front & back are independent cameras)
# ----------------------------------------------------------------------------
# Apps that enumerate cameras (CameraManager.getCameraIdList, Camera2
# characteristics, Camera1 getNumberOfCameras) can tell two "cameras" apart by
# their static metadata, and they can tell an AVD from a phone because both
# cameras report the same emulator sensor. Each camera therefore gets its own
# generated spec: a distinct sensor model/serial, orientation, resolution,
# focal length, aperture and frame rate - plausible for the device's brand.
CAMERA_FACING_KEYS = {"back": "back", "front": "front"}

# Fallback sensors, used only when the requested model is not in
# ``profiles.device_profiles.CAMERA_PROFILES`` (custom/unknown models). They
# are picked per facing so front and back still differ.
_CAMERA_SENSORS = {
    "back": [
        ("SONY IMX890", (4096, 3072), 1.0, 6.7, 1.8, 90),   # 12.6 MP flagship main
        ("SONY IMX766", (4096, 3072), 1.0, 6.4, 1.8, 90),
        ("SAMSUNG S5KGN3", (4000, 3000), 1.0, 6.9, 1.8, 90),
        ("SAMSUNG ISOCELL HM6", (4080, 3060), 1.0, 6.5, 1.9, 90),
        ("SONY IMX882", (4096, 3072), 1.1, 6.6, 1.9, 90),
    ],
    "front": [
        ("SONY IMX663", (3280, 2464), 1.2, 3.2, 2.2, 270),  # 8.1 MP selfie
        ("SAMSUNG S5KGD2", (3264, 2448), 1.2, 3.1, 2.2, 270),
        ("SONY IMX616", (3264, 2448), 1.4, 3.0, 2.2, 270),
        ("SAMSUNG S5K3P9", (3280, 2464), 1.0, 3.3, 2.4, 270),
        ("SONY IMX471", (3264, 2448), 1.4, 3.1, 2.4, 270),
    ],
}

# Vintage: a build date ~1-3 years before "today" for the device.
CAMERA_BUILD_EPOCH_DAYS = (400, 1100)

# Full-resolution stream sizes an app may ask Camera2 for, keyed by the
# sensor's aspect ratio. A phone reports the sizes its own sensor can produce.
_FULL_SIZES = {
    "4:3": [(4000, 3000), (3264, 2448), (2592, 1944), (1920, 1440), (1280, 960), (640, 480)],
    "16:9": [(1920, 1080), (1280, 720), (640, 360)],
}


def _sensor_spec(brand, board, model, facing, rng):
    """(sensor, (width, height), pixel, focal, aperture, orientation) for a facing.

    The selected phone model decides the hardware: a Galaxy S24 Ultra reports
    its own ISOCELL HP2SX main sensor, not the generic emulator camera and not
    a random one from a shared pool. Unknown models fall back to the pool.
    """
    from profiles.device_profiles import get_camera_profile

    entry = get_camera_profile(model) or get_camera_profile(
        f"{brand} {model}" if brand else model
    )
    if entry and entry.get(facing):
        return entry[facing]
    return rng.choice(_CAMERA_SENSORS[facing])


def generate_camera_specs(brand="Samsung", board="unknown", model="Unknown"):
    """Two independent, internally consistent camera specs (``back``/``front``).

    The sensors/resolutions come from the *selected phone model*
    (``profiles.device_profiles.CAMERA_PROFILES``) so the cameras describe the
    spoofed device - a Galaxy S24 does not report the AVD's synthetic camera.
    Only the identifiers (serial, build date) are generated.

    Returned per camera:
      ``model`` (sensor), ``serial``, ``res_width``/``res_height``,
      ``focal_length``, ``aperture``, ``pixel_size``, ``orientation``,
      ``frame_rate``, ``stream_sizes``, ``pose`` (translation/rotation) and
      ``build_date``.
    """
    rng = random.Random(f"{brand}|{board}|{model}|cameras")
    today = datetime.now()
    specs = {}
    for facing in ("back", "front"):
        sensor, (width, height), pixel, focal, aperture, orientation = _sensor_spec(
            brand, board, model, facing, rng
        )
        serial = "".join(rng.choices(string.ascii_uppercase + string.digits, k=12))
        build_day = today - timedelta(days=rng.randint(*CAMERA_BUILD_EPOCH_DAYS))
        # Stream sizes: the full sensor resolution, then the standard smaller
        # sizes of the same aspect ratio (sorted largest first, like a phone).
        aspect = "16:9" if abs(width / height - 16 / 9) < 0.02 else "4:3"
        sizes = [(width, height)] + [
            (w, h) for w, h in _FULL_SIZES[aspect]
            if (w, h) != (width, height) and w * h < width * height
        ]
        # The camera module sits on the same board; keep the ids distinct.
        specs[facing] = {
            "facing": facing,
            "model": sensor,
            "serial": serial,
            "res_width": width,
            "res_height": height,
            "stream_sizes": sizes,
            "pixel_size": pixel,
            "focal_length": focal,
            "aperture": aperture,
            "orientation": orientation,
            "frame_rate": rng.choice([30, 30, 30, 60]),
            "nv21_width": width,
            "nv21_height": height,
            "pose_translation": (
                [0.0, -0.02, 0.0] if facing == "back" else [rng.choice([-0.05, 0.05]), 0.04, 0.0]
            ),
            "pose_rotation": (
                [0.0, 0.0, 0.0, 1.0] if facing == "back"
                else [0.0, 1.0, 0.0, 0.0]  # front sensors are mirrored
            ),
            "build_date": build_day.strftime("%Y-%m-%d"),
        }
    # Guarantee independence even if a profile repeats a sensor on both facings.
    if specs["back"]["model"] == specs["front"]["model"]:
        specs["front"]["model"] = (
            "SAMSUNG S5KGD2" if specs["back"]["model"].startswith("SONY") else "SONY IMX663"
        )
    if specs["back"]["serial"] == specs["front"]["serial"]:
        specs["front"]["serial"] = "".join(
            rng.choices(string.ascii_uppercase + string.digits, k=12)
        )
    return specs


_FP_RE = re.compile(
    r"([^/]+)/([^/]+)/([^/:]+):([^/:]+)/([^/:]+)/([^/:]+):([^/:]+)/([\w.-]+)")


def _parse_profile_fingerprint(fp):
    """Split a build fingerprint: {release, build_id, incremental, ...}."""
    m = _FP_RE.match(fp or "")
    if not m:
        return {}
    brand, product, device, release, build_id, incremental, btype, tags = m.groups()
    return {
        "brand": brand, "product": product, "device": device,
        "release": release, "build_id": build_id,
        "incremental": incremental, "build_type": btype, "build_tags": tags,
    }


def generate_device_identity(model_profile, android_version="14"):
    """Generate complete device identity based on model profile"""
    brand = model_profile.get("brand", "Samsung")
    model = model_profile.get("model", "Unknown")
    model_name = model_profile.get("name", model)
    board = model_profile.get("board", "unknown")
    
    # Get GPU info: one coherent chip -> renderer, vendor, EGL vendor, driver
    # version, extension string and GL limits all agree with each other.
    from profiles.gpu_profiles import gpu_extension_string, gpu_profile

    gl_renderer = generate_gpu_renderer(brand, model_name)
    gpu = gpu_profile(gl_renderer)
    gl_vendor = gpu["vendor"]
    gl_limits = gpu["limits"]
    
    # Get battery info
    battery = generate_battery_info(brand, model_name)
    
    # Get CPU info
    cpu = generate_cpu_info(brand, board)

    # A pinned profile fingerprint is the authoritative build description.
    _fp_parts = _parse_profile_fingerprint(model_profile.get("fingerprint", ""))
    _fp_build_id = _fp_parts.get("build_id")
    _fp_incremental = _fp_parts.get("incremental")
    _fp_release = _fp_parts.get("release")
    ip_number = _fp_incremental
    
    identity = {
        # Hardware IDs
        "imei": generate_imei(),
        "imei2": generate_imei(),  # Dual SIM
        "serial": generate_serial_number(brand),
        "android_id": generate_android_id(),
        "mac_address": generate_mac_address(),
        "wifi_mac": generate_mac_address(),
        "bluetooth_mac": generate_mac_address(),
        
        # Google IDs
        "gsf_id": generate_gsf_id(),
        "advertising_id": generate_advertising_id(),
        
        # Device info from profile
        "brand": brand,
        "manufacturer": brand,
        "model": model,
        "device": model_profile.get("device", "generic"),
        "board": board,
        "fingerprint": model_profile.get("fingerprint", ""),
        "product": model_profile.get("device", "generic"),
        
        # Build info. When the model profile pins a fingerprint, its build id /
        # release / incremental describe the *real* build for that device, so
        # reuse them instead of generating a random build id next to a pinned
        # fingerprint (two different build numbers for one device is a tell -
        # and a fingerprint that says Android 14 while the device runs 15).
        "build_id": _fp_build_id or generate_build_id(),
        "build_number": ip_number or generate_build_number(brand),
        "build_incremental": _fp_incremental or None,
        # The running image decides the release the device will report; the
        # fingerprint keeps its own (real) release, so the two never disagree.
        "android_version": android_version,
        "fingerprint_release": _fp_release,
        
        # GPU info (self-consistent: see profiles/gpu_profiles.py)
        "gl_renderer": gpu["model"],
        "gl_vendor": gl_vendor,
        "gl_version": f"OpenGL ES 3.2 {gpu['driver']}",
        "egl_vendor": gpu["egl_vendor"],
        "gl_extensions": gpu_extension_string(gpu["model"]),
        "gl_limits": {
            "max_texture_size": gl_limits["texture"],
            "max_viewport_dims": [gl_limits["viewport"], gl_limits["viewport"]],
            "max_vertex_attribs": gl_limits["attribs"],
            "max_texture_units": gl_limits["texture_units"],
            "max_renderbuffer_size": gl_limits["renderbuffer"],
            "max_combined_texture_units": gl_limits["combined_units"],
            "glsl_version": gl_limits["glsl"],
        },
        "gpu_name": gpu["model"],
        "gpu_hw": gpu["hw"],
        "gpu_score": gpu["score"],
        "gpu_year": gpu["year"],
        
        # Battery info
        "battery_capacity": battery["capacity_mah"],
        "battery_technology": battery["technology"],
        "battery_health": battery["health"],
        
        # CPU info
        "cpu_model": cpu["model"],
        "cpu_cores": cpu["cores"],
        "cpu_hardware": cpu["hardware"],
        
        # Bootloader & Baseband
        "bootloader": generate_bootloader(brand, model),
        "baseband": generate_baseband(brand),
        
        # Network info
        "operator": random.choice(["Vodafone", "T-Mobile", "AT&T", "O2", "Orange"]),
        "operator_code": random.choice(["310260", "310410", "310120", "23415", "20801"]),
        
        # Nearby WiFi (for fingerprinting)
        "nearby_wifi": generate_wifi_ssid(),
        
        # Camera hardware (front & back are independent sensors - not two
        # views of the same emulator camera). Regenerated with the identity.
        "camera_specs": generate_camera_specs(brand, board, model),

        # Timestamps
        "first_boot": int(datetime.now().timestamp()) - random.randint(86400 * 30, 86400 * 365),
        "generated_at": datetime.now().isoformat(),
    }
    
    return identity

