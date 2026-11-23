"""Which ``ro.*`` properties the spoofer must never write - and why.

Android resolves HAL modules, native libraries, framework resource paths and
boot/security policy from a handful of ``ro.*`` properties, both while the
system boots and every time a process starts. Writing an identity-shaped value
into one of those keys does not "hide the emulator" - it makes the platform
look for files that do not exist on the emulator's x86_64 image and abort the
consumer process:

    ro.product.board = zuma      -> hw_get_module_by_class() looks for
                                   /vendor/lib64/hw/<class>.zuma.so   (ENOENT)
    ro.hardware      = zuma      -> libEGL looks for libEGL_zuma.so,
                                   libvulkan looks for vulkan.zuma.so
    ro.product.device = husky    -> framework overlay/resource paths resolve
                                   to /product/husky/... which is absent
    ro.build.characteristics     -> Settings/SystemUI feature gating; flipping
    = default                      it from "emulator" breaks Android 14
    ro.product.cpu.abilist       -> zygote/linker search for ARM libraries on
                                   an x86_64 system and bootloop

On Android 14 the first consumer a user notices is the Settings app: it dies on
launch ("Settings keeps stopping"). That is the crash this module exists to
prevent, and it is why the *system* keeps these keys at their native values.

The spoof still looks perfect to apps, because the hook scripts return the
spoofed value for exactly these keys *inside the app process*
(``frida_toolkit/scripts/native_hooks.js`` and ``anti_detect.js`` intercept
``__system_property_get``, ``Build.*``, ``GLES20``/``EGL14`` and friends) -
never at the system level.

Every write path (boot ``-prop`` flags, ``resetprop`` batches, ``setprop``
fallbacks and the ``build.prop`` rewrite) filters through :func:`filter_props`
so a stale saved prop set, an older device record or a future edit cannot
reintroduce the crash.
"""

from __future__ import annotations

#: Partitions that carry their own ``ro.product.<partition>.*`` block.
PARTITIONS = ("system", "system_ext", "product", "vendor", "odm", "bootimage")

#: The emulator images' native value for ``ro.build.characteristics``.
CHARACTERISTICS_NATIVE = "emulator"

#: Properties that must ALWAYS keep their native (emulator) value.
NEVER_SET = frozenset(
    {
        # --- libhardware HAL resolution -------------------------------------
        # hw_get_module_by_class() falls back to ro.hardware / ro.product.board
        # and builds the file name "<class>.<value>.so"; a phone chipset name
        # makes every HAL module lookup fail with ENOENT.
        "ro.hardware",
        "ro.boot.hardware",
        "ro.hardware.chipname",
        "ro.product.board",
        "ro.boot.board",
        "ro.board.platform",
        # --- framework resource / overlay paths -----------------------------
        # "/product/<device>/overlay" and friends; a retail codename points at
        # directories that do not exist on a generic x86_64 image.
        "ro.product.device",
        "ro.build.product",
        # --- Settings / SystemUI feature gating (Android 14) -----------------
        "ro.build.characteristics",
        # --- ABI + zygote ----------------------------------------------------
        # The image ships x86_64 libraries. Claiming arm64 here makes zygote and
        # the linker hunt for ARM .so files and bootloops the device. ABI
        # spoofing is done per app by spec_hooks.js / native_hooks.js.
        "ro.product.cpu.abi",
        "ro.product.cpu.abi2",
        "ro.product.cpu.abilist",
        "ro.product.cpu.abilist32",
        "ro.product.cpu.abilist64",
        "ro.zygote",
        "ro.dalvik.vm.isa.arm",
        "ro.dalvik.vm.isa.arm64",
        "ro.dalvik.vm.native.bridge",
        # --- boot / verified-boot state --------------------------------------
        # Read by the framework and by SafetyNet-style probes; rewriting them
        # either crashes the boot-state consumer or is instantly inconsistent.
        "ro.boot.verifiedbootstate",
        "ro.boot.flash.locked",
        "ro.boot.veritymode",
        "ro.boot.vbmeta.digest",
        "ro.boot.vbmeta.device_state",
    }
) | frozenset(f"ro.product.{part}.device" for part in PARTITIONS)

#: Prefixes that are just as load-bearing as the exact keys above.
#: ``ro.hardware.egl`` / ``ro.hardware.vulkan`` are used as exact shared
#: library filename suffixes (``libEGL_<value>.so``, ``vulkan.<value>.so``), so
#: pointing them at "adreno"/"arm" makes RenderThread fail to load a driver and
#: crashes every app's UI.
NEVER_SET_PREFIXES = ("ro.hardware.",)

#: Only safe *after* the boot has completed: ``init.ranchu.rc``, the EGL loader
#: and qemu-props probe these during early boot, so they may be flipped by the
#: runtime spoofer but must never be passed as an ``emulator -prop`` flag.
POST_BOOT_ONLY = frozenset(
    {
        "ro.kernel.qemu",
        "ro.kernel.qemu.gles",
        "ro.boot.qemu",
    }
)


def is_never_set(key: str) -> bool:
    """True when *key* must be left at its native value, whatever the profile."""
    key = (key or "").strip()
    if not key:
        return False
    if key in NEVER_SET:
        return True
    return any(key.startswith(prefix) for prefix in NEVER_SET_PREFIXES)


def is_boot_unsafe(key: str) -> bool:
    """True when *key* must not be handed to the emulator as a boot ``-prop``."""
    return is_never_set(key) or key in POST_BOOT_ONLY


def filter_props(props, boot_time: bool = False):
    """Split *props* into ``(safe, dropped)``.

    ``boot_time=True`` also drops :data:`POST_BOOT_ONLY` keys, which is what a
    launch-time ``-prop`` flag needs; at runtime those are applied after the
    system has finished booting.
    """
    safe = {}
    dropped = {}
    for key, value in (props or {}).items():
        name = str(key).strip()
        if not name:
            continue
        bad = is_boot_unsafe(name) if boot_time else is_never_set(name)
        if bad:
            dropped[name] = value
        else:
            safe[name] = value
    return safe, dropped


#: Fragments of an emulator image's *native* value for the keys that must never
#: be guessed from an identity. Used to tell "an older version wrote a phone
#: codename here" apart from "this device simply has the real value".
_NATIVE_HINTS = {
    "ro.product.board": ("goldfish", "ranchu", "emu", "gce_", "cutf"),
    "ro.product.device": ("generic", "ranchu", "emu", "sdk_", "goldfish", "cutf"),
    "ro.build.product": ("generic", "ranchu", "emu", "sdk_", "goldfish", "cutf"),
    "ro.board.platform": ("goldfish", "ranchu", "emu", ""),
}


def looks_native(key: str, value: str) -> bool:
    """True when *value* is plausibly this image's own (unspoofed) value.

    Lets the repair pass stay quiet about keys that are already correct instead
    of reporting every guarded key on every run.
    """
    key = (key or "").strip()
    value = (value or "").strip()
    low = value.lower()
    if key in ("ro.hardware", "ro.boot.hardware") or key.startswith("ro.hardware."):
        return any(tag in low for tag in ("ranchu", "goldfish", "vsoc", "emulation", "emulator"))
    if key in ("ro.product.cpu.abi", "ro.product.cpu.abi2", "ro.product.cpu.abilist",
               "ro.product.cpu.abilist32", "ro.product.cpu.abilist64"):
        return "x86" in low
    if key == "ro.build.characteristics":
        return CHARACTERISTICS_NATIVE in low
    if key == "ro.boot.verifiedbootstate":
        return low in ("green", "verified")
    if key == "ro.boot.flash.locked":
        return low in ("1", "true")
    if key in _NATIVE_HINTS:
        return any(hint in low for hint in _NATIVE_HINTS[key])
    return False


def describe(keys) -> str:
    """Human-readable list for logs / the dashboard."""
    keys = sorted({str(k) for k in (keys or ()) if k})
    if not keys:
        return "none"
    shown = ", ".join(keys[:6])
    return shown + (f" (+{len(keys) - 6} more)" if len(keys) > 6 else "")


def native_iprop_backup_path(build_prop_path: str) -> str:
    """Where the untouched copy of *build_prop_path* is kept on the device.

    ``HardwareSpoofer._rewrite_build_props`` backs up every ``build.prop`` it
    edits here; :func:`spoof.hardware_spoof.HardwareSpoofer._restore_dangerous_props`
    reads the same file to recover the *exact* native value of a guarded key
    that an older version overwrote.
    """
    return (
        "/data/local/antidetect-buildprop-backup/"
        + str(build_prop_path).lstrip("/").replace("/", "_")
    )
