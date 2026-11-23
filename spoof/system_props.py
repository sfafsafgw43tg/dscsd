"""
System Properties Spoofing - Modify ro.* properties to hide emulator
"""


def generate_props_config(identity, brand, model):
    """Generate emulator config.ini properties for anti-detection"""
    props = []
    
    # Device identity properties
    props.append(f"ro.product.model={identity['model']}")
    props.append(f"ro.product.brand={identity['brand']}")
    props.append(f"ro.product.manufacturer={identity['manufacturer']}")
    props.append(f"ro.product.device={identity['device']}")
    props.append(f"ro.product.board={identity['board']}")
    props.append(f"ro.product.name={identity['product']}")
    
    # Partition specific spoofing
    for part in ['system', 'vendor', 'odm', 'product', 'system_ext']:
        props.append(f"ro.product.{part}.model={identity['model']}")
        props.append(f"ro.product.{part}.brand={identity['brand']}")
        props.append(f"ro.product.{part}.manufacturer={identity['manufacturer']}")
        props.append(f"ro.product.{part}.device={identity['device']}")
        props.append(f"ro.product.{part}.name={identity['product']}")

    # Build properties
    props.append(f"ro.build.fingerprint={identity['fingerprint']}")
    props.append(f"ro.build.id={identity['build_id']}")
    props.append(f"ro.build.display.id={identity['build_number']}")
    props.append(f"ro.build.version.incremental={identity['build_number']}")
    props.append(f"ro.build.description={identity['product']}-user {identity['android_version']} {identity['build_id']} {identity['build_number']} release-keys")
    
    # Boot-time `-prop` flags must NEVER set ro.kernel.qemu=0,
    # ro.kernel.qemu.gles=0, ro.hardware, or ro.boot.hardware: setting those
    # before init completes breaks init.ranchu.rc, libEGL host GPU detection,
    # and /vendor/lib64/hw/*.ranchu.so HAL module loading. HardwareSpoofer
    # applies ro.hardware / ro.kernel.qemu / ro.boot.qemu safely AFTER boot
    # via resetprop-rs + soft zygote reload.

    # Hardware identifiers
    props.append(f"ro.serialno={identity['serial']}")
    props.append(f"ro.boot.serialno={identity['serial']}")
    props.append(f"ro.bootloader={identity.get('bootloader', 'unknown')}")
    props.append(f"ro.baseband={identity.get('baseband', 'unknown')}")
    props.append(f"gsm.version.baseband={identity.get('baseband', 'unknown')}")
    
    # Network - Polish carrier with correct MCC+MNC (260xx)
    carrier = identity.get('sim_carrier') or identity.get('operator', '')
    numeric = identity.get('sim_operator_numeric', '') or identity.get('operator_numeric', '')
    if numeric and not str(numeric).startswith('260'):
        numeric = '260' + str(identity.get('operator_code', '01')).rjust(2, '0')
    props.append(f"gsm.sim.operator.alpha={carrier}")
    props.append(f"gsm.operator.alpha={carrier}")
    props.append(f"gsm.sim.operator.iso-country=pl")
    props.append(f"gsm.operator.iso-country=pl")
    if numeric:
        props.append(f"gsm.sim.operator.numeric={numeric}")
        props.append(f"gsm.operator.numeric={numeric}")

    # CPU ABI list is NOT set via boot-time -prop flags: telling zygote the
    # system supports only arm64-v8a at boot causes it to look for ARM native
    # libraries on an x86_64 system, which crashes zygote and bootloops.
    # ABI spoofing is done ONLY at runtime via Frida hooks
    # (Build.SUPPORTED_ABIS + __system_property_get interceptor in hooked apps).

    return '\n'.join(props)


def get_adb_spoof_commands(identity):
    """Get ADB commands to spoof properties at runtime"""
    commands = []
    
    # Set system properties
    props = {
        "ro.product.model": identity["model"],
        "ro.product.brand": identity["brand"],
        "ro.product.manufacturer": identity["manufacturer"],
        "ro.product.device": identity["device"],
        "ro.serialno": identity["serial"],
        "ro.kernel.qemu": "0",
        "ro.bootimage.build.fingerprint": identity["fingerprint"],
        "ro.build.fingerprint": identity["fingerprint"],
    }
    
    for key, value in props.items():
        commands.append(f"setprop {key} {value}")
    
    return commands


EMULATOR_DETECTION_PROPS = [
    # Properties commonly checked for emulator detection
    "ro.kernel.qemu",
    "ro.hardware",
    "ro.boot.qemu",
    "init.svc.qemu-props",
    "qemu.sf.lcd_density",
    "qemu.hw.mainkeys",
    "ro.bootimage.build.fingerprint",
    "ro.build.characteristics",
    "ro.product.cpu.abilist",
    "gsm.version.baseband",
    "gsm.version.ril-impl",
]

EMULATOR_INDICATORS = {
    # Files that indicate emulator
    "files": [
        "/system/bin/qemu-props",
        "/system/lib/libc_malloc_debug_qemu.so",
        "/dev/socket/qemud",
        "/dev/qemu_pipe",
        "/system/bin/androVM-props",
        "/system/bin/microvirt-prop",
        "/system/lib/libdroid4x.so",
        "/data/youwave_id",
        "/dev/vboxguest",
        "/dev/vboxuser",
    ],
    # Processes that indicate emulator
    "processes": [
        "qemu-props",
        "qemud",
        "goldfish",
    ],
    # Package names of known emulators
    "packages": [
        "com.bluestacks",
        "com.bignox.app",
        "com.memu",
        "com.ldmnq",
        "com.kaopu.gameassistant",
    ],
}
