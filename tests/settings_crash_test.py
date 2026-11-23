#!/usr/bin/env python3
"""Regression test: spoofing must not crash the Android Settings app.

The reported bug: after applying spoofing on Android 14, opening **Settings**
on the phone killed it ("Settings keeps stopping"). The cause was never the
identity values themselves - it was *which* keys carried them.

A handful of ``ro.*`` properties are load-bearing rather than descriptive:

    ro.product.board / ro.board.platform   -> HAL module file names
    ro.hardware / ro.hardware.<class>      -> HAL + EGL/Vulkan .so suffixes
    ro.product.device / ro.build.product   -> framework resource paths
    ro.product.<partition>.device          -> same, per partition
    ro.build.characteristics               -> Settings/SystemUI gating
    ro.product.cpu.abilist*                -> zygote/linker ABI

Writing an identity value into one of those makes the platform look for files
the emulator does not ship, and the first process a user notices dying is
Settings. ``spoof/prop_policy.py`` now guards them at every write path:

  * launch-time ``emulator -prop`` flags  (spoof/system_props.py)
  * runtime ``setprop`` re-assertion      (spoof/system_spoof.py)
  * the ``resetprop`` / ``build.prop``    (spoof/hardware_spoof.py)

and ``HardwareSpoofer._repair_toxic_props`` heals a device that an *older*
version already poisoned, which is the half that actually un-crashes Settings
for an existing install.
"""
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_sdk import setup_fake_sdk  # noqa: E402

paths = setup_fake_sdk()
TMP = paths["tmp"]
STATE = paths["state"]
LOG = paths["log"]
os.environ["HOME"] = str(TMP / "home")
(TMP / "home").mkdir(exist_ok=True)
for key in ("ANDROID_SDK_ROOT", "ANDROID_HOME", "ANDROID_SDK_HOME"):
    os.environ[key] = str(paths["sdk"])

failures = []


def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(label)


import config  # noqa: E402

config.DATA_DIR = TMP / "data"
config.SAVED_DEVICES_DIR = TMP / "devices"
config.LOGS_DIR = TMP / "logs"
for d in (config.DATA_DIR, config.SAVED_DEVICES_DIR, config.LOGS_DIR):
    d.mkdir(parents=True, exist_ok=True)

from spoof.prop_policy import (  # noqa: E402
    NEVER_SET,
    filter_props,
    is_boot_unsafe,
    is_never_set,
)

# Keys that crashed Settings, plus the ABI/HAL ones that break the boot.
CRASHERS = (
    "ro.product.board",
    "ro.board.platform",
    "ro.product.device",
    "ro.build.product",
    "ro.build.characteristics",
    "ro.hardware",
    "ro.boot.hardware",
    "ro.hardware.egl",
    "ro.hardware.vulkan",
    "ro.product.cpu.abilist",
    "ro.product.cpu.abilist64",
)
for part in ("system", "system_ext", "product", "vendor", "odm"):
    CRASHERS += (f"ro.product.{part}.device",)

print("== 1. the policy guards every load-bearing key ==")
for key in CRASHERS:
    check(f"{key} is guarded", is_never_set(key))
for key in ("ro.product.model", "ro.product.brand", "ro.product.manufacturer",
            "ro.product.name", "ro.product.system.model", "ro.build.id",
            "ro.build.fingerprint", "ro.serialno", "ro.baseband"):
    check(f"{key} stays spoofable", not is_never_set(key))
check("ro.kernel.qemu is allowed at runtime but not at boot",
      not is_never_set("ro.kernel.qemu") and is_boot_unsafe("ro.kernel.qemu"))
check("filter_props(boot_time=True) drops post-boot keys",
      "ro.kernel.qemu" not in filter_props({"ro.kernel.qemu": "0"}, boot_time=True)[0])

# ---------------------------------------------------------------- 2. boot props
print("\n== 2. launch -prop flags never carry a load-bearing key ==")
from spoof.system_props import generate_props_config, get_adb_spoof_commands  # noqa: E402
from profiles.device_profiles import get_model_profile  # noqa: E402
from profiles.profile_generator import generate_device_identity  # noqa: E402

identity = generate_device_identity(get_model_profile("Google Pixel 8 Pro"))
boot_text = generate_props_config(identity, "google", identity["model"])
boot_keys = {
    line.split("=", 1)[0].strip()
    for line in boot_text.splitlines()
    if "=" in line
}
check("boot props are generated at all", bool(boot_keys), repr(boot_text[:200]))
leaked = [k for k in boot_keys if is_boot_unsafe(k)]
check("no load-bearing key in the -prop flags", not leaked, str(leaked))
check("identity props are still passed (model/brand/fingerprint)",
      {"ro.product.model", "ro.product.brand", "ro.build.fingerprint"} <= boot_keys,
      str(sorted(boot_keys)))
check("the retail codename is NOT passed as ro.product.device",
      "ro.product.device" not in boot_keys)
check("the SoC codename is NOT passed as ro.product.board",
      "ro.product.board" not in boot_keys)

# A hand-edited / legacy saved prop set must be filtered too.
legacy = "\n".join([
    "ro.product.model=Pixel 8 Pro",
    "ro.product.device=husky",
    "ro.product.board=zuma",
    "ro.build.characteristics=default",
    "ro.hardware=zuma",
])
safe, dropped = filter_props(
    dict(line.split("=", 1) for line in legacy.splitlines()), boot_time=True
)
check("legacy prop set is filtered down to the cosmetic keys",
      safe == {"ro.product.model": "Pixel 8 Pro"} and len(dropped) == 4,
      f"safe={safe} dropped={sorted(dropped)}")

adb_cmds = get_adb_spoof_commands(identity)
check("runtime setprop helper never touches ro.product.device",
      not any("ro.product.device" in cmd for cmd in adb_cmds), str(adb_cmds))
check("runtime setprop helper keeps the cosmetic identity",
      any("ro.product.model" in cmd for cmd in adb_cmds))

# --------------------------------------------- 3. SystemSpoofer (the leak that
#                                                undid the hardware fix)
print("\n== 3. SystemSpoofer cannot re-poison the device ==")
from spoof.system_spoof import SystemSpoofer  # noqa: E402

spoofer = SystemSpoofer(device_serial="emulator-5998")


class RecordingSpoofer(SystemSpoofer):
    """Applies nothing; records every key the class would have set."""

    def __init__(self, serial):
        super().__init__(serial)
        self.set_keys = []

    def _adb(self, command, timeout=30, retry=True):
        if len(command) >= 4 and command[0] == "shell" and command[1] == "setprop":
            self.set_keys.append(command[2])
        return ""


recorder = RecordingSpoofer("emulator-5998")
recorder._apply_build_props(identity)
check("SystemSpoofer sets something (it still does the runtime pass)",
      bool(recorder.set_keys), str(recorder.set_keys))
bad = [k for k in recorder.set_keys if is_never_set(k)]
check("SystemSpoofer writes NO load-bearing key", not bad, str(bad))
check("SystemSpoofer no longer forces ro.build.characteristics=default",
      "ro.build.characteristics" not in recorder.set_keys)
check("SystemSpoofer no longer writes ro.product.device",
      "ro.product.device" not in recorder.set_keys)
check("SystemSpoofer still re-asserts the cosmetic identity",
      "ro.product.model" in recorder.set_keys, str(recorder.set_keys))

# --------------------------------------------------- 4. the on-device repair
print("\n== 4. a device poisoned by an older version is repaired ==")
from spoof.hardware_spoof import HardwareSpoofer  # noqa: E402

serial = "emulator-5997"
(STATE / f"{serial}.state").write_text("device\n")
(STATE / f"{serial}.boot").write_text("1\n")
(STATE / f"{serial}.ziegote_pid").write_text("1001\n")
(STATE / "root-allowed").write_text("1\n")
(STATE / "resetprop-installed").write_text("450000\n")

# What an older version left behind: the crash-inducing identity values.
poisoned = {
    "ro.hardware": "zuma",
    "ro.boot.hardware": "zuma",
    "ro.product.board": "zuma",
    "ro.product.device": "husky",
    "ro.build.product": "husky",
    "ro.build.characteristics": "default",
    "ro.product.model": "Pixel 8 Pro",
}
(STATE / f"{serial}.resetprops").write_text(
    "".join(f"{k}={v}\n" for k, v in poisoned.items()), encoding="utf-8"
)


def getprop(ser, key):
    out = subprocess.run(
        [config.ADB_PATH, "-s", ser, "shell", "getprop", key],
        capture_output=True, text=True, timeout=20,
    ).stdout
    return out.strip()


check("the poisoned state is readable before the fix",
      getprop(serial, "ro.hardware") == "zuma"
      and getprop(serial, "ro.build.characteristics") == "default")

# The real device answers a whole `getprop` dump in one call; parsing that is
# what keeps the repair cheap (~1 round-trip instead of ~30).
_dump_probe = HardwareSpoofer(device_serial=serial)
_dump_probe._adb = lambda cmd, timeout=30: (
    "[ro.product.model]: [Pixel 8 Pro]\n"
    "[ro.hardware]: [zuma]\n"
    "[ro.product.board]: [zuma]\n"
    "[ro.build.characteristics]: [default]\n"
    "[ro.product.device]: []\n"
    "[ro.unrelated.key]: [x]\n"
)
_parsed = _dump_probe._probe_guarded_props()
check("a single `getprop` dump is parsed for the guarded keys",
      _parsed == {"ro.hardware": "zuma", "ro.product.board": "zuma",
                  "ro.build.characteristics": "default"},
      str(_parsed))

healer = HardwareSpoofer(device_serial=serial)
healer._root_done = True
repaired = healer._repair_toxic_props()

check("the repair reports what it fixed", bool(repaired), str(repaired))
check("ro.hardware is back to the emulator HAL (no more zuma .so lookups)",
      getprop(serial, "ro.hardware") == "ranchu", getprop(serial, "ro.hardware"))
check("ro.boot.hardware is back to the emulator HAL",
      getprop(serial, "ro.boot.hardware") == "ranchu",
      getprop(serial, "ro.boot.hardware"))
check("ro.product.device no longer points at a missing resource path",
      getprop(serial, "ro.product.device") == "generic_x86_64",
      getprop(serial, "ro.product.device"))
# ro.build.product has no safe default (it must mirror ro.product.device), so it
# is only repaired from a real build.prop backup - never guessed. The repair has
# to say so out loud rather than silently claim success.
check("an unrepairable key is reported, not silently 'fixed'",
      bool(healer.unresolved_toxic), str(healer.unresolved_toxic))
check("ro.build.characteristics is back to the native 'emulator'",
      getprop(serial, "ro.build.characteristics") == "emulator",
      getprop(serial, "ro.build.characteristics"))
check("the spoofed identity survives the repair",
      getprop(serial, "ro.product.model") == "Pixel 8 Pro",
      getprop(serial, "ro.product.model"))
check("zygote was reloaded so Settings restarts on the healed props",
      (STATE / f"{serial}.zygote_restarts").exists())

check("a second repair pass is a no-op (nothing left to fix)",
      healer._repair_toxic_props() == [])

# With a build.prop backup present the remaining key IS recoverable, and the
# next pass (e.g. the next Spoof click, or the next launch) picks it up.

backup_dir = STATE / "buildprop-backup"
backup_dir.mkdir(exist_ok=True)
(backup_dir / "system_build.prop").write_text(
    "ro.product.device=generic_x86_64\n"
    "ro.build.product=generic_x86_64\n"
    "ro.hardware=ranchu\n",
    encoding="utf-8",
)

# Re-poison ro.build.product so there is something left to repair.
with open(STATE / f"{serial}.resetprops", "a", encoding="utf-8") as fh:
    fh.write("ro.build.product=husky\n")

healer2 = HardwareSpoofer(device_serial=serial)
healer2._root_done = True
check("the backup supplies the native value for the unrepairable key",
      healer2._native_value_for("ro.build.product") == "generic_x86_64",
      repr(healer2._native_value_for("ro.build.product")))
repaired2 = healer2._repair_toxic_props()
check("ro.build.product is repaired once the original build.prop is known",
      "ro.build.product" in repaired2, str(repaired2))
check("ro.build.product no longer points at a missing resource path",
      getprop(serial, "ro.build.product") == "generic_x86_64",
      getprop(serial, "ro.build.product"))

# ------------------------------------------- 5. build.prop rewrite is guarded
print("\n== 5. the build.prop rewrite cannot reintroduce the poison ==")
hw_source = (REPO / "spoof" / "hardware_spoof.py").read_text(encoding="utf-8")
rewrite = hw_source.split("def _rewrite_build_props", 1)[1].split("def spoof_imei", 1)[0]
check("_rewrite_build_props filters through prop_policy",
      "filter_props(all_updates)" in rewrite)
check("_rewrite_build_props logs what it refuses to write",
      "Not writing guarded props" in rewrite)
check("_repair_toxic_props runs before the 'already spoofed' early return",
      hw_source.index("self._repair_toxic_props(restart_zygote=False)")
      < hw_source.index("if not mismatched:"))
_fast_path = hw_source.split("if not mismatched:", 1)[1].split("report[\"method\"] = \"runtime setprop\"", 1)[1].split("\n\n", 1)[0]
check("the fast path still bounces zygote when it healed something",
      "report[\"repaired_props\"] = repaired" in _fast_path
      and "_restart_zygote_and_wait()" in _fast_path, repr(_fast_path[:300]))

sys_source = (REPO / "spoof" / "system_spoof.py").read_text(encoding="utf-8")
check("SystemSpoofer filters its prop dict",
      "filter_props(props)" in sys_source)

# ------------------------------------- 6. the real launch command, end to end
print("\n== 6. the actual launch command carries no guarded prop ==")
import re  # noqa: E402

from core.device_manager import DeviceManager  # noqa: E402

(STATE / "root-allowed").write_text("1\n")
mgr = DeviceManager()
mgr.create_device("Google Pixel 8 Pro", android_version="14", name="crash_e2e")
LOG.write_text("")
mgr.launch_device("crash_e2e", enable_frida=False, wait_for_boot=True)

LAUNCH = [
    line for line in LOG.read_text().splitlines()
    if "fake emulator" in line and "-avd" in line
]
check("the emulator was actually launched", bool(LAUNCH), str(LAUNCH)[:200])
launch_cmd = LAUNCH[0] if LAUNCH else ""
flag_keys = [p.split("=", 1)[0] for p in re.findall(r"-prop (\S+)", launch_cmd)]
check("the launch really passes identity props", len(flag_keys) >= 10, str(len(flag_keys)))
leaked_flags = [k for k in flag_keys if is_boot_unsafe(k)]
check("NO guarded prop reaches the emulator at boot",
      not leaked_flags, str(leaked_flags))
for key in ("ro.product.model", "ro.product.brand", "ro.build.fingerprint",
            "ro.serialno", "ro.baseband"):
    check(f"the launch still spoofs {key}", key in flag_keys)
check("the launch does not claim the retail codename as ro.product.device",
      "ro.product.device" not in flag_keys)
check("the launch does not claim the SoC codename as ro.product.board",
      "ro.product.board" not in flag_keys)

serial_e2e = mgr.get_device("crash_e2e").get("serial")

# The device must end up spoofed AND with its guarded keys native, which is the
# combination Settings needs to survive.
def gp(key):
    return subprocess.run(
        [config.ADB_PATH, "-s", serial_e2e, "shell", "getprop", key],
        capture_output=True, text=True, timeout=20,
    ).stdout.strip()


# ------------------------------- 7. the per-app hooks must still engage
print("\n== 7. hooks still serve the spoofed values to apps ==")
native_js = (REPO / "frida_toolkit" / "scripts" / "native_hooks.js").read_text(
    encoding="utf-8"
)
probe = native_js.split("var nativeAlreadySpoofed = false;", 1)[1].split(
    "if (nativeAlreadySpoofed)", 1
)[0]
# The "already spoofed natively, skip the hook" shortcut only looked at
# model/serial/qemu. With the guarded keys now kept native, that shortcut would
# fire and leave apps reading ro.hardware=ranchu - so it must check them too.
for key in ("ro.hardware", "ro.product.device", "ro.product.board",
            "ro.build.characteristics"):
    check(f"the hook-skip probe inspects {key}", key in probe)
check("the hook-skip probe still requires the spoofed model",
      "ro.product.model" in probe)
check("native_hooks still answers ro.hardware for apps",
      '"ro.hardware": targetBoard' in native_js)
check("native_hooks still answers ro.product.device for apps",
      'setIfPresent("ro.product.device", id.device)' in native_js)

saved_e2e = mgr.get_device("crash_e2e")
check("the device is reported as spoofed", saved_e2e.get("spoof_applied") is True,
      str(saved_e2e.get("spoof_error")))
check("after launch, ro.product.model is the spoofed model",
      gp("ro.product.model") == "Pixel 8 Pro", gp("ro.product.model"))
check("after launch, ro.hardware is still the emulator HAL",
      gp("ro.hardware") == "ranchu", gp("ro.hardware"))
check("after launch, ro.build.characteristics is still 'emulator'",
      gp("ro.build.characteristics") in ("emulator", ""),
      gp("ro.build.characteristics"))
mgr.stop_device("crash_e2e")

if failures:
    print(f"\n{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("\nAll Settings-crash regression checks passed.")
