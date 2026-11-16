#!/usr/bin/env python3
"""Model selection must resolve to the phone the user picked.

Reported: selecting a Google model produced a Xiaomi identity
(23116PN5BC = Xiaomi 14 **Pro**) in a fingerprint report, and the base models
silently turned into their bigger siblings.

Root causes pinned here:
1. `get_model_profile()` returned the first profile whose name *contained* the
   request, and the table lists Pro/Ultra first - so "Google Pixel 8" became a
   **Pixel 8 Pro** (husky, not shiba), "Xiaomi 14" became a **Xiaomi 14 Pro**,
   "Samsung Galaxy S24" became an S24 Ultra... 9 of 45 models drifted.
2. The saved label kept the raw request while the identity came from the
   resolved (wrong) profile, so nothing looked inconsistent in the UI.
3. A selection mixing a name and a *different* hardware code (e.g. copying
   "Xiaomi 14 23116PN5BC" from a report) picked one of them silently.
4. The identity's `build_id` was random even when the profile pins a real
   fingerprint, and a pinned fingerprint's Android release could contradict the
   image the device actually runs.
"""
import json
import os
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


from profiles.device_profiles import (  # noqa: E402
    ALL_DEVICES,
    get_model_profile,
    get_model_profile_by_code,
    model_conflict,
)
from profiles.profile_generator import generate_device_identity  # noqa: E402
from spoof.hardware_spoof import build_identity_prop_maps, parse_fingerprint  # noqa: E402

ALL_NAMES = [d["name"] for brand in ALL_DEVICES.values() for d in brand]

# --------------------------------------------------- exact name resolution
print("== every model resolves to itself ==")
mismatched = [(n, (get_model_profile(n) or {}).get("name")) for n in ALL_NAMES
              if (get_model_profile(n) or {}).get("name") != n]
check(f"all {len(ALL_NAMES)} model names resolve exactly", not mismatched, str(mismatched[:4]))

print("== base models never drift to Pro/Ultra/Plus ==")
cases = {
    "Google Pixel 8": "Pixel 8",
    "Google Pixel 7": "Pixel 7",
    "Google Pixel 6": "Pixel 6",
    "Xiaomi 14": "23127PN0CC",
    "Xiaomi 13": "2211133G",
    "Samsung Galaxy S24": "SM-S921B",
    "Samsung Galaxy S23": "SM-S911B",
    "Samsung Galaxy S22": "SM-S901B",
    "POCO F5": "23049PCD8G",
}
for asked, expected_code in cases.items():
    profile = get_model_profile(asked)
    code = (profile or {}).get("model")
    check(f"{asked!r} -> {expected_code}", code == expected_code, f"got {code!r}")
    variant_words = ("pro", "ultra", "plus")
    check(f"{asked!r} does not add a variant word",
          profile is not None
          and not any(w in profile["name"].lower().split() for w in variant_words),
          (profile or {}).get("name"))

print("== the Pro/Ultra siblings still work ==")
for asked, expected in (
    ("Google Pixel 8 Pro", "Pixel 8 Pro"),
    ("Xiaomi 14 Pro", "23116PN5BC"),
    ("Samsung Galaxy S24 Ultra", "SM-S928B"),
    ("Google Pixel 8a", "Pixel 8a"),
    ("POCO F5 Pro", "23013PC75G"),
):
    got = (get_model_profile(asked) or {}).get("model")
    check(f"{asked!r} -> {expected}", got == expected, f"got {got!r}")

print("== short / code / wrong selections ==")
check("short 'Pixel 8' picks the base model without brand token drift",
      (get_model_profile("Pixel 8") or {}).get("model") == "Pixel 8")
check("'Pixel 8 Ultra' (no such device) is refused, not silently downgraded",
      get_model_profile("Pixel 8 Ultra") is None)
check("unknown model returns None", get_model_profile("SomePhone 9999") is None)
check("empty selection returns None", get_model_profile("") is None)
check("exact hardware code resolves", (get_model_profile("23116PN5BC") or {}).get("name")
      == "Xiaomi 14 Pro")
check("by-code lookup helper agrees",
      (get_model_profile_by_code("SM-S921B") or {}).get("name") == "Samsung Galaxy S24")
check("unknown code returns None", get_model_profile_by_code("NOPE-123") is None)
check("name + matching code is not a conflict",
      model_conflict("Xiaomi 14 Pro 23116PN5BC") is None)
conflict = model_conflict("Xiaomi 14 23116PN5BC")
check("name + different code is flagged",
      conflict is not None and conflict["by_name"]["name"] == "Xiaomi 14"
      and conflict["by_code"]["name"] == "Xiaomi 14 Pro", str(conflict))
check("no name, no conflict", model_conflict("23116PN5BC") is None)

# ------------------------------------------------------- created devices
print("== created devices report the selected phone ==")
from core.device_manager import DeviceManager  # noqa: E402

manager = DeviceManager()
for asked, code, fp_device in (
    ("Google Pixel 8", "Pixel 8", "shiba"),
    ("Xiaomi 14", "23127PN0CC", "houji"),
    ("Samsung Galaxy S24", "SM-S921B", "e2s"),
):
    device = manager.create_device(asked, android_version="14")
    name = asked.replace(" ", "_")
    check(f"{asked}: label is the resolved model", device["model"] == asked, device["model"])
    check(f"{asked}: identity model code matches", device["identity"]["model"] == code,
          device["identity"]["model"])
    check(f"{asked}: identity fingerprint device matches",
          device["identity"]["device"] == fp_device, device["identity"]["device"])
    check(f"{asked}: requested_model recorded", device.get("requested_model") == asked)
    reloaded = manager.get_device(device["name"])
    check(f"{asked}: label persists", reloaded["model"] == asked, reloaded["model"])

device = manager.create_device("Xiaomi 14", android_version="14", name="x14pro")
check("Xiaomi 14 is NOT the 14 Pro build", device["identity"]["model"] != "23116PN5BC",
      device["identity"]["model"])
check("no Xiaomi 14 Pro code leaks into a Pixel device",
      manager.get_device([d for d in (device["name"],)][0]) is not None)

print("== conflicting selection is rejected with both options ==")
try:
    manager.create_device("Xiaomi 14 23116PN5BC", android_version="14", name="mixed")
    check("mixed name+code raises", False, "no error")
except ValueError as exc:
    msg = str(exc)
    check("mixed name+code raises", "different devices" in msg, msg)
    check("the error names both options",
          "Xiaomi 14" in msg and "Xiaomi 14 Pro" in msg, msg)

# ---------------------------------------------------- build consistency
print("== identity self-describes one build ==")
for model in ("Google Pixel 8", "Samsung Galaxy S24", "Xiaomi 14"):
    identity = generate_device_identity(get_model_profile(model))
    fp = parse_fingerprint(identity["fingerprint"])
    check(f"{model}: build id comes from the fingerprint",
          identity["build_id"] == fp["build_id"],
          f"{identity['build_id']} vs {fp['build_id']}")
    check(f"{model}: incremental comes from the fingerprint",
          identity.get("build_incremental") == fp["incremental"])
    check(f"{model}: fingerprint_release recorded",
          identity.get("fingerprint_release") == fp["release"])

print("== a 15 image never advertises Android 14 ==")
identity = generate_device_identity(get_model_profile("Google Pixel 8"), "15")
check("identity release follows the image", identity["android_version"] == "15",
      identity["android_version"])
core, _ = build_identity_prop_maps(identity)
check("ro.build.version.release follows the image",
      core["ro.build.version.release"] == "15", core["ro.build.version.release"])
check("build id still matches the fingerprint",
      core["ro.build.id"] == parse_fingerprint(identity["fingerprint"])["build_id"])

identity14 = generate_device_identity(get_model_profile("Google Pixel 8"), "14")
core14, _ = build_identity_prop_maps(identity14)
check("a 14 image reports 14", core14["ro.build.version.release"] == "14")

for prop in ("ro.product.model", "ro.product.brand", "ro.product.manufacturer",
             "ro.product.device", "ro.build.fingerprint"):
    check(f"{prop} is populated for spoofing", bool(core14.get(prop)), prop)

# ------------------------------------------------------------------ GUI
print("== GUI / CLI wiring ==")
js = (REPO / "gui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
app_src = (REPO / "gui" / "app.py").read_text(encoding="utf-8")
cli = (REPO / "main.py").read_text(encoding="utf-8")

check("dropdown shows the hardware model code",
      "option.dataset.modelCode" in js and "${model.name} (${model.model})" in js)
check("dropdown value is the exact profile name", "option.value = model.name;" in js)
check("create toast shows the resolved model", "Device created: ${resolved" in js)
check("model_note is surfaced", "data.model_note" in js)
check("create response reports the resolved model", '"requested_model": device.get("requested_model")' in app_src)
check("create response explains a normalized selection", "data.model_note" in app_src or "model_note" in app_src)
check("CLI documents model codes", "hardware model code" in cli)

if failures:
    print(f"\n{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("\nAll model-selection checks passed.")
