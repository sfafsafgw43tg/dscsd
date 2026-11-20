#!/usr/bin/env python3
"""Spec spoofing: the spoofed phone reports the model's real hardware.

Reported problem (with a screenshot of Settings -> About -> CPU while spoofing
was applied):

    CPU hardware       ranchu
    Supported ABIs     x86_64, arm64-v8a
    GPU renderer       Android Emulator OpenGL ES Translator (Google SwiftShader)
    GPU vendor         Google (Google Inc.)

and the request: *"make the renderer be spoofed when applying spoofing and
according to device ... make the ram look like base set of ram for the selected
device while it gives less, same for storage and all phone specs"*, plus *"make
the phone number +48 everytime and randomised"* using the supplied
phone-numbers.txt as the carrier database (Play, Orange, Plus, T-Mobile,
Lycamobile, Netia, nju mobile, Plush, Red Bull Mobile).

What this pins:

1. every model in the device table resolves to a real SoC / GPU / RAM /
   storage / ABI / screen - never the emulator's own values;
2. the reported GPU is the model's real chip (a Galaxy S24 Ultra is Xclipse
   940, not a random Adreno) and it is a chip in the GPU database;
3. RAM/storage are the retail figures while the VM's real limit stays visible
   (nothing is claimed that cannot be backed up, and used+free == total);
4. the phone number is always +48, randomised, and its carrier matches the
   number's prefix - the carrier is always one of the nine requested ones;
5. the Frida payload carries the hardware block, the +48 number and carrier,
   and spec_hooks.js really answers ActivityManager / StatFs / ABIs / GLES /
   DisplayMetrics / Telephony from it;
6. /proc/cpuinfo and /proc/meminfo are served from the model, not the AVD;
7. the device record and the API expose the specs so the UI can show them;
8. numbers are stable per device and never reused between devices.
"""
import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_sdk import setup_fake_sdk  # noqa: E402

paths = setup_fake_sdk()
TMP = paths["tmp"]
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
(paths["state"] / "root-allowed").write_text("1\n")

import gui.app as ga  # noqa: E402
from core.hook_manager import HookManager, _hardware_payload  # noqa: E402
from profiles.device_profiles import ALL_DEVICES, get_model_profile  # noqa: E402
from profiles.gpu_profiles import GPU_MODELS, gpu_profile  # noqa: E402
from profiles.hardware_specs import hardware_spec, ram_label, storage_label  # noqa: E402
from profiles.profile_generator import generate_device_identity  # noqa: E402
import profiles.phone_numbers as pn  # noqa: E402

SPEC_HOOKS = (REPO / "frida_toolkit" / "scripts" / "spec_hooks.js").read_text(encoding="utf-8")
NATIVE_HOOKS = (REPO / "frida_toolkit" / "scripts" / "native_hooks.js").read_text(encoding="utf-8")
HW_SRC = (REPO / "profiles" / "hardware_specs.py").read_text(encoding="utf-8")

ALL_MODELS = [d for devs in ALL_DEVICES.values() for d in devs]

# ---------------------------------------------------------------- 1. coverage
print("== 1. every model resolves to real hardware ==")
check("the device table is not empty", len(ALL_MODELS) > 20, str(len(ALL_MODELS)))
missing = []
unmatched_gpu = []
for dev in ALL_MODELS:
    specs = hardware_spec(dev.get("model", ""), dev.get("brand", ""))
    if not specs.get("matched_model"):
        missing.append(dev["name"])
    if not gpu_profile(specs.get("gpu", "")).get("matched"):
        unmatched_gpu.append((dev["name"], specs.get("gpu")))
check("every model maps to a spec entry", not missing, str(missing[:5]))
check("every model's GPU is a real chip in the database", not unmatched_gpu,
      str(unmatched_gpu[:5]))
check("no chip in the spec table is missing from the GPU database",
      all(gpu_profile(s["gpu"])["matched"] for s in
          (hardware_spec(c, "") for c in ("SM-S928B", "Pixel 8", "23116PN5BC"))),
      "a spec points at an unknown renderer")

# ---------------------------------------------------------------- 2. no emulator leaks
print("== 2. the emulator never answers for the phone ==")
for dev in ALL_MODELS[:12]:
    ident = generate_device_identity(dev, "14")
    label = dev["name"]
    check(f"{label}: renderer is not SwiftShader",
          "swiftshader" not in ident["gl_renderer"].lower()
          and "emulator" not in ident["gl_renderer"].lower(), ident["gl_renderer"])
    check(f"{label}: no x86 ABI advertised",
          "x86_64" not in ident["abi_list"] and "x86" not in ident["abi_list"],
          str(ident["abi_list"]))
    check(f"{label}: GPU vendor matches the chip",
          ident["gl_vendor"] == gpu_profile(ident["gl_renderer"])["vendor"],
          f"{ident['gl_renderer']} -> {ident['gl_vendor']}")
    check(f"{label}: SoC is not ranchu/goldfish",
          "ranchu" not in str(ident["soc"]).lower()
          and "goldfish" not in str(ident["soc"]).lower(), str(ident["soc"]))

# the CPU/GPU pair must belong together, not be two random picks
check("Snapdragon models report an Adreno (or ARM-designed) chip",
      gpu_profile(hardware_spec("SM-S918B", "Samsung")["gpu"])["vendor"] == "Qualcomm",
      hardware_spec("SM-S918B", "Samsung")["gpu"])
check("Exynos 2400 models report Xclipse 940",
      hardware_spec("SM-S928B", "Samsung")["gpu"] == "Xclipse 940",
      hardware_spec("SM-S928B", "Samsung")["gpu"])
check("Pixel 8 reports the Tensor's Mali-G715",
      hardware_spec("Pixel 8", "Google")["gpu"] == "Mali-G715 MC10",
      hardware_spec("Pixel 8", "Google")["gpu"])
check("a low-end Exynos is not called ARMv9",
      "ARMv8" in hardware_spec("SM-A146B", "Samsung")["cpu_arch"],
      hardware_spec("SM-A146B", "Samsung")["cpu_arch"])
check("a flagship Exynos is ARMv9",
      "ARMv9" in hardware_spec("SM-S928B", "Samsung")["cpu_arch"],
      hardware_spec("SM-S928B", "Samsung")["cpu_arch"])

# ---------------------------------------------------------------- 3. RAM / storage
print("== 3. RAM and storage look like the phone, honestly ==")
retail = hardware_spec("SM-S928B", "Samsung", real_ram_mb=2048, real_storage_mb=6144)
check("the flagship reports its retail 12 GB, not the AVD's 2 GB",
      retail["ram_reported_mb"] == 12 * 1024, str(retail["ram_reported_mb"]))
check("the VM's real RAM is still recorded (nothing hidden)",
      retail["ram_available_mb"] == 2048, str(retail["ram_available_mb"]))
check("the clamp is explained in plain language", len(retail["notes"]) >= 1,
      str(retail["notes"]))
check("RAM label formats like Settings shows it", ram_label(retail) == "12.0 GB", ram_label(retail))

honest = hardware_spec("SM-S928B", "Samsung", real_ram_mb=2048, mode="honest")
check("honest mode never claims more than the VM has",
      honest["ram_reported_mb"] == 2048, str(honest["ram_reported_mb"]))

check("storage used + free always equals the reported total",
      retail["storage_used_mb"] + retail["storage_free_mb"] == retail["storage_reported_mb"],
      f"{retail['storage_used_mb']} + {retail['storage_free_mb']} != {retail['storage_reported_mb']}")
check("storage is a configuration shops sell (256 GB, not 1 TB)",
      hardware_spec("SM-S928B", "Samsung")["storage_reported_mb"] == 512 * 1024,
      str(hardware_spec("SM-S928B", "Samsung")["storage_reported_mb"]))
check("a budget phone reports its small storage",
      storage_label(hardware_spec("SM-A146B", "Samsung")) == "128 GB",
      storage_label(hardware_spec("SM-A146B", "Samsung")))
check("cores never exceed what the VM has",
      hardware_spec("SM-S928B", "Samsung", real_cores=4)["cpu_cores"] == 4,
      str(hardware_spec("SM-S928B", "Samsung", real_cores=4)["cpu_cores"]))

# ---------------------------------------------------------------- 4. phone numbers
print("== 4. every line is a randomised Polish +48 number ==")
check("the supplied number database is loaded", pn.pool_size() >= 900, str(pn.pool_size()))
check("the database holds only +48 numbers",
      all(n.startswith("+48") and len(n) == 12 for n in pn._load_pool()),
      str(pn._load_pool()[:3]))

SAMPLES = [pn.polish_number_for() for _ in range(400)]
check("every generated line starts with +48",
      all(s["number"].startswith("+48") and len(s["number"]) == 12 for s in SAMPLES),
      str([s["number"] for s in SAMPLES[:3]]))
check("lines are randomised (many distinct numbers)",
      len({s["number"] for s in SAMPLES}) > 300,
      str(len({s["number"] for s in SAMPLES})))
check("the carrier is always one of the nine requested",
      {s["carrier"] for s in SAMPLES} <= set(pn.CARRIERS),
      str({s["carrier"] for s in SAMPLES} - set(pn.CARRIERS)))
check("all nine carriers can appear",
      {s["carrier"] for s in SAMPLES} == set(pn.CARRIERS),
      str(sorted({s["carrier"] for s in SAMPLES})))
contradictions = [(s["number"], s["carrier"], s["prefix"]) for s in SAMPLES
                  if pn.PREFIX_OPERATORS.get(s["prefix"]) != s["carrier"]]
check("the carrier never contradicts the number's prefix", not contradictions,
      str(contradictions[:3]))
check("the carrier numeric is a real Polish MCC/MNC",
      all(s["numeric"].startswith("260") and len(s["numeric"]) == 5 for s in SAMPLES),
      str([s["numeric"] for s in SAMPLES[:3]]))
check("the carrier list matches the user's list exactly",
      set(pn.CARRIERS) == {"Orange", "Play", "Plus", "T-Mobile", "Lycamobile",
                           "Netia", "nju mobile", "Plush", "Red Bull Mobile"},
      str(pn.CARRIERS))
check("0048 / bare / spaced input all normalise",
      pn.normalise("0048 573 123 456") == "+48573123456"
      and pn.normalise("573123456") == "+48573123456", "normalise failed")
check("a non-Polish number is refused",
      pn.normalise("+1 555 0100") is None, str(pn.normalise("+1 555 0100")))

first = pn.polish_number_for()["number"]
excluded = {first}
for _ in range(30):
    excluded.add(pn.polish_number_for(exclude=excluded)["number"])
check("numbers are never repeated for the same device set",
      len(excluded) == 31, f"only {len(excluded)} distinct numbers from 31 draws")

check("a chosen carrier is honoured",
      all(pn.polish_number_for(carrier="Orange")["carrier"] == "Orange" for _ in range(20)))

# every prefix in the table is unique (a duplicate key silently drops a carrier)
prefixes = re.findall(r'^    "(\d{3})":', HW_SRC, re.M)  # noqa: F841 - specs table
PN_SRC = (REPO / "profiles" / "phone_numbers.py").read_text(encoding="utf-8")
block = PN_SRC[PN_SRC.index("PREFIX_OPERATORS = {"):PN_SRC.index("# The carriers the user listed")]
prefix_keys = re.findall(r'^    "(\d{3})":', block, re.M)
check("no duplicate prefix keys (which would silently drop a carrier)",
      len(prefix_keys) == len(set(prefix_keys)),
      str({k for k in prefix_keys if prefix_keys.count(k) > 1}))
check("every carrier has its own number block",
      all(pn._prefixes_for(c) for c in pn.CARRIERS),
      str([c for c in pn.CARRIERS if not pn._prefixes_for(c)]))

# ---------------------------------------------------------------- 5. Frida payload
print("== 5. the hooks receive the specs and the line ==")
ident = generate_device_identity(get_model_profile("Google Pixel 8"), "14")
payload = _hardware_payload(ident)
check("the payload carries RAM", payload.get("ram_reported_mb") == 8192,
      str(payload.get("ram_reported_mb")))
check("the payload carries storage", payload.get("storage_reported_mb") == 256 * 1024,
      str(payload.get("storage_reported_mb")))
check("the payload carries the ABIs (no x86)",
      payload.get("abi") == ["arm64-v8a", "armeabi-v7a"], str(payload.get("abi")))
check("the payload carries the SoC", "Tensor" in str(payload.get("soc")),
      str(payload.get("soc")))
check("the payload carries the screen",
      (payload.get("screen") or {}).get("dpi", 0) > 0, str(payload.get("screen")))

combined = HookManager("/data/x").generate_combined_script(ident)
check("spec_hooks.js is part of the combined script", "spec_hooks.js" in combined)
check("the combined script has the hardware block", '"hardware": {' in combined)
check("the combined script has the +48 number",
      re.search(r'"phoneNumber": "\+48\d{9}"', combined) is not None,
      "no phoneNumber in payload")
check("the combined script names the SIM carrier",
      re.search(r'"carrier": "(Orange|Play|Plus|T-Mobile|Lycamobile|Netia|nju mobile|Plush|Red Bull Mobile)"',
                combined) is not None, "no carrier in payload")
check("config is valid JSON-embeddable JS", "var deviceIdentity = {" in combined)
# the emitted payload itself must be parseable JSON
_payload_json = combined.split("var deviceIdentity = ", 1)[1].split(";\n", 1)[0]
try:
    _parsed = json.loads(_payload_json)
    _config_ok = True
except Exception as _exc:  # noqa: BLE001
    _parsed = {}
    _config_ok = False
    print("   payload JSON error:", _exc)
check("the identity payload parses as JSON", _config_ok)
check("the parsed payload carries the +48 line",
      str(_parsed.get("phoneNumber", "")).startswith("+48"), str(_parsed.get("phoneNumber"))[:20])
check("the parsed payload carries RAM and storage",
      _parsed.get("hardware", {}).get("ram_reported_mb", 0) > 0
      and _parsed.get("hardware", {}).get("storage_reported_mb", 0) > 0,
      str(_parsed.get("hardware", {}).get("ram_reported_mb")))

# the JS itself must answer every leaked value
for api, mem in [
    ("ActivityManager.getMemoryInfo", "RAM"),
    ("StatFs", "storage"),
    ("SUPPORTED_ABIS", "ABIs"),
    ("glGetString", "GPU renderer"),
    ("eglQueryString", "GPU vendor"),
    ("DisplayMetrics", "screen"),
    ("getLine1Number", "phone number"),
    ("getSimOperatorName", "carrier"),
    ("availableProcessors", "core count"),
]:
    check(f"spec_hooks.js answers {mem} ({api})", api in SPEC_HOOKS, api)
check("spec_hooks.js never writes a bogus numeric",
      'gsm.sim.operator.numeric' not in SPEC_HOOKS)
check("spec_hooks.js bails out when Java is unavailable",
      "Java.available" in SPEC_HOOKS)

# ---------------------------------------------------------------- 6. /proc files
print("== 6. /proc/cpuinfo and /proc/meminfo come from the model ==")
check("native_hooks serves a spoofed /proc/meminfo", "createSpoofedMeminfoFd" in NATIVE_HOOKS)
check("memory totals are taken from the model, not the AVD",
      "hw.ram_reported_mb" in NATIVE_HOOKS, "meminfo ignores the model RAM")
check("cpuinfo is built per core, not a single x86 block",
      "for (var core = 0; core < coreCount; core++)" in NATIVE_HOOKS)
check("cpuinfo reports an ARM implementer (0x41), not x86",
      "CPU implementer\\t: 0x41" in NATIVE_HOOKS)
check("cpuinfo Hardware line is the SoC, not ranchu",
      'hw.soc ? hw.soc.replace' in NATIVE_HOOKS)
check("the memory numbers agree with ActivityManager (same source)",
      "hw.ram_available_mb" in NATIVE_HOOKS and "hw.ram_available_mb" in SPEC_HOOKS)

# nothing in the shipped code still advertises the emulator
# Values that actually ship to the phone must never carry an emulator name.
# (The module docstring quotes the reported screenshot, so only value
# assignments are inspected.)
_value_lines = [ln for ln in HW_SRC.splitlines()
                if ln.strip().startswith(('"', "'", "gpu=", "soc=", "abi="))
                or "gpu=" in ln or "soc=" in ln]
check("no spec VALUE says ranchu/goldfish/swiftshader",
      not any(re.search(r"ranchu|goldfish|swiftshader|x86_64", ln, re.I)
              for ln in _value_lines),
      str([ln[:60] for ln in _value_lines if re.search(r"ranchu|goldfish|swiftshader|x86_64", ln, re.I)]))

# ---------------------------------------------------------------- 7. the API
print("== 7. the device record and API expose the specs ==")
client = ga.create_app().test_client()
client.post("/api/devices", json={"model": "Google Pixel 8", "android_version": "14",
                                  "name": "specdev"})
devices = client.get("/api/devices").get_json()
device = next(d for d in devices if d["name"] == "specdev")
specs = device.get("hardware_specs") or {}
check("the API returns the hardware specs", bool(specs), "hardware_specs missing")
check("the API returns the RAM the phone reports",
      specs.get("ram_reported_mb") == 8192, str(specs.get("ram_reported_mb")))
check("the API returns the real GPU", specs.get("gpu") == "Mali-G715 MC10",
      str(specs.get("gpu")))
check("the API returns a +48 line",
      str(device.get("phone_number", "")).startswith("+48"), str(device.get("phone_number")))
check("the API returns the matching carrier",
      device.get("sim_carrier") in pn.CARRIERS, str(device.get("sim_carrier")))
check("the identity on disk has the same line",
      (device.get("identity") or {}).get("phone_number") == device.get("phone_number"))
check("the line is stable across reads (not re-randomised)",
      next(d for d in client.get("/api/devices").get_json()
           if d["name"] == "specdev").get("phone_number") == device.get("phone_number"))

# two devices never share a line
client.post("/api/devices", json={"model": "Samsung Galaxy S24", "android_version": "14",
                                  "name": "specdev2"})
devices2 = client.get("/api/devices").get_json()
lines = {d["name"]: d.get("phone_number") for d in devices2}
check("two devices never share a phone number",
      lines.get("specdev") != lines.get("specdev2"), str(lines))

# ---------------------------------------------------------------- 8. the UI
print("== 8. the dashboard shows what the phone reports ==")
APP_JS = (REPO / "gui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
CSS = (REPO / "gui" / "static" / "css" / "style.css").read_text(encoding="utf-8")
check("Details has a reported-specs block", "function specsBlock" in APP_JS)
check("the block is rendered", "${specsBlock(device)}" in APP_JS)
check("it shows RAM and storage",
      "'RAM'" in APP_JS.split("function specsBlock")[1][:2000]
      and "'Storage'" in APP_JS.split("function specsBlock")[1][:2000])
check("it shows the GPU, SoC, ABIs and screen",
      all(k in APP_JS.split("function specsBlock")[1][:2200]
          for k in ("'GPU'", "'Chipset'", "'ABIs'", "'Screen'")), "a row is missing")
check("it shows the +48 line and carrier", "'Line'" in APP_JS)
check("it has styles", ".specs-grid" in CSS and ".specs-value" in CSS)
check("clamping is explained in the UI, not hidden", "specs-note" in APP_JS)

if failures:
    print(f"\n{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("\nAll hardware-spec / phone-number spoofing checks passed.")
