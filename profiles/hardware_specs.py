"""
Real hardware specs - RAM, storage, CPU, GPU and screen per phone model.

The emulator always has less than the phone it pretends to be: an AVD with
``hw.ramSize=2048`` still has 2 GB even when the spoofed model is a Galaxy S24
Ultra with 12 GB. Android's Settings -> About / Storage / CPU pages and every
anti-emulator SDK read the *real* numbers from:

  ActivityManager.MemoryInfo.totalMem          (RAM, "Random-access memory")
  ActivityManager.getDeviceConfigurationInfo() / SystemMemoryInfo
  StatFs on /data                              (storage, "Internal storage")
  /proc/cpuinfo, System.getProperty("os.arch")
  Build.SUPPORTED_ABIS                          (x86_64 leaks the emulator)
  GLES20.glGetString(GL_RENDERER)               ("Android Emulator OpenGL ES
                                                Translator (SwiftShader)")
  DisplayMetrics / getRealMetrics               (resolution, dpi)

This module answers one question: *what should the real phone report, and what
can we honestly claim?* It never invents a number it cannot back up, and it
never claims more RAM than the emulator actually has - Android would OOM-kill
apps that went along with a 12 GB claim on a 2 GB AVD. Instead each spec is
clamped to what the host AVD can actually serve, and the claim is recorded in
``notes`` so the dashboard and ``doctor`` can say exactly what happened.
"""
import re

# ---------------------------------------------------------------------------
# Per-model hardware specs
# ---------------------------------------------------------------------------
# ram_gb / storage_gb: the retail configuration the model ships in.
# cores, soc, gpu (renderer name as it appears in profiles/gpu_profiles.py),
# abi: real device ABIs, screen: (width, height, dpi, inches).
_SPECS = {
    # ---------------------------------------------------------- Samsung
    "SM-S928": dict(ram_gb=12, storage_gb=(256, 512, 1024), soc="Exynos 2400",
                    cores=10, gpu="Xclipse 940", abi=["arm64-v8a", "armeabi-v7a"],
                    screen=(1440, 3120, 505, 6.8), gpu_series="xclipse 940"),
    "SM-S926": dict(ram_gb=12, storage_gb=(256, 512), soc="Exynos 2400",
                    cores=10, gpu="Xclipse 940", abi=["arm64-v8a", "armeabi-v7a"],
                    screen=(1080, 2340, 416, 6.7), gpu_series="xclipse 940"),
    "SM-S921": dict(ram_gb=8, storage_gb=(128, 256, 512), soc="Exynos 2400",
                    cores=10, gpu="Xclipse 940", abi=["arm64-v8a", "armeabi-v7a"],
                    screen=(1080, 2340, 416, 6.2), gpu_series="xclipse 940"),
    "SM-S918": dict(ram_gb=12, storage_gb=(256, 512, 1024), soc="Snapdragon 8 Gen 2",
                    cores=8, gpu="Adreno (TM) 740", abi=["arm64-v8a", "armeabi-v7a"],
                    screen=(1440, 3088, 500, 6.8), gpu_series="adreno 740"),
    "SM-S916": dict(ram_gb=8, storage_gb=(256, 512), soc="Snapdragon 8 Gen 2",
                    cores=8, gpu="Adreno (TM) 740", abi=["arm64-v8a", "armeabi-v7a"],
                    screen=(1080, 2340, 393, 6.6), gpu_series="adreno 740"),
    "SM-S911": dict(ram_gb=8, storage_gb=(128, 256, 512), soc="Snapdragon 8 Gen 2",
                    cores=8, gpu="Adreno (TM) 740", abi=["arm64-v8a", "armeabi-v7a"],
                    screen=(1080, 2340, 425, 6.1), gpu_series="adreno 740"),
    "SM-S908": dict(ram_gb=8, storage_gb=(128, 256, 512), soc="Snapdragon 8 Gen 1",
                    cores=8, gpu="Adreno (TM) 730", abi=["arm64-v8a", "armeabi-v7a"],
                    screen=(1440, 3088, 500, 6.8), gpu_series="adreno 730"),
    "SM-S906": dict(ram_gb=8, storage_gb=(128, 256), soc="Snapdragon 8 Gen 1",
                    cores=8, gpu="Adreno (TM) 730", abi=["arm64-v8a", "armeabi-v7a"],
                    screen=(1080, 2340, 393, 6.6), gpu_series="adreno 730"),
    "SM-S901": dict(ram_gb=8, storage_gb=(128, 256), soc="Snapdragon 8 Gen 1",
                    cores=8, gpu="Adreno (TM) 730", abi=["arm64-v8a", "armeabi-v7a"],
                    screen=(1080, 2340, 425, 6.1), gpu_series="adreno 730"),
    "SM-A546": dict(ram_gb=8, storage_gb=(128, 256), soc="Exynos 1380",
                    cores=8, gpu="Mali-G68 MP5", abi=["arm64-v8a", "armeabi-v7a"],
                    screen=(1080, 2340, 403, 6.4)),
    "SM-A346": dict(ram_gb=6, storage_gb=(128, 256), soc="Exynos 1280",
                    cores=8, gpu="Mali-G68 MP4", abi=["arm64-v8a", "armeabi-v7a"],
                    screen=(1080, 2340, 390, 6.6)),
    "SM-A146": dict(ram_gb=4, storage_gb=(64, 128), soc="Exynos 1330",
                    cores=8, gpu="Mali-G68 MP2", abi=["arm64-v8a", "armeabi-v7a"],
                    screen=(1080, 2408, 400, 6.6)),
    "SM-F946": dict(ram_gb=12, storage_gb=(256, 512, 1024), soc="Snapdragon 8 Gen 2",
                    cores=8, gpu="Adreno (TM) 740", abi=["arm64-v8a", "armeabi-v7a"],
                    screen=(1812, 2176, 373, 7.6), gpu_series="adreno 740"),
    "SM-F731": dict(ram_gb=8, storage_gb=(128, 256, 512), soc="Snapdragon 8 Gen 2",
                    cores=8, gpu="Adreno (TM) 740", abi=["arm64-v8a", "armeabi-v7a"],
                    screen=(720, 748, 306, 6.7), gpu_series="adreno 740"),
    # ------------------------------------------------------------ Google
    "Pixel 8 Pro": dict(ram_gb=12, storage_gb=(128, 256, 512, 1024), soc="Google Tensor G3",
                        cores=9, gpu="Mali-G715 MC10", abi=["arm64-v8a", "armeabi-v7a"],
                        screen=(1344, 2992, 489, 6.7)),
    "Pixel 8": dict(ram_gb=8, storage_gb=(128, 256), soc="Google Tensor G3",
                    cores=9, gpu="Mali-G715 MC10", abi=["arm64-v8a", "armeabi-v7a"],
                    screen=(1080, 2400, 428, 6.2)),
    "Pixel 8a": dict(ram_gb=8, storage_gb=(128, 256), soc="Google Tensor G3",
                     cores=9, gpu="Mali-G715 MC10", abi=["arm64-v8a", "armeabi-v7a"],
                     screen=(1080, 2400, 430, 6.1)),
    "Pixel 7 Pro": dict(ram_gb=12, storage_gb=(128, 256, 512), soc="Google Tensor G2",
                        cores=8, gpu="Mali-G710 MC10", abi=["arm64-v8a", "armeabi-v7a"],
                        screen=(1440, 3120, 512, 6.7)),
    "Pixel 7": dict(ram_gb=8, storage_gb=(128, 256), soc="Google Tensor G2",
                    cores=8, gpu="Mali-G710 MC10", abi=["arm64-v8a", "armeabi-v7a"],
                    screen=(1080, 2400, 416, 6.3)),
    "Pixel 6": dict(ram_gb=8, storage_gb=(128, 256), soc="Google Tensor",
                    cores=8, gpu="Mali-G78 MP20", abi=["arm64-v8a", "armeabi-v7a"],
                    screen=(1080, 2400, 411, 6.4)),
    # ----------------------------------------------------------- OnePlus
    "CPH2581": dict(ram_gb=16, storage_gb=(256, 512), soc="Snapdragon 8 Gen 3",
                    cores=8, gpu="Adreno (TM) 750", abi=["arm64-v8a", "armeabi-v7a"],
                    screen=(1440, 3168, 510, 6.82), gpu_series="adreno 750"),
    "CPH2447": dict(ram_gb=16, storage_gb=(128, 256), soc="Snapdragon 8 Gen 2",
                    cores=8, gpu="Adreno (TM) 740", abi=["arm64-v8a", "armeabi-v7a"],
                    screen=(1440, 3216, 525, 6.7), gpu_series="adreno 740"),
    "NE2213": dict(ram_gb=12, storage_gb=(128, 256), soc="Snapdragon 8 Gen 1",
                   cores=8, gpu="Adreno (TM) 730", abi=["arm64-v8a", "armeabi-v7a"],
                   screen=(1440, 3216, 525, 6.7), gpu_series="adreno 730"),
    "CPH2493": dict(ram_gb=16, storage_gb=(256, 512), soc="Dimensity 9000",
                    cores=8, gpu="Mali-G710 MC10", abi=["arm64-v8a", "armeabi-v7a"],
                    screen=(1240, 2772, 451, 6.74)),
    "CPH2569": dict(ram_gb=12, storage_gb=(128, 256), soc="Snapdragon 782G",
                    cores=8, gpu="Adreno (TM) 642L", abi=["arm64-v8a", "armeabi-v7a"],
                    screen=(1080, 2412, 394, 6.7), gpu_series="adreno 642"),
    # ---------------------------------------------------- Huawei / Honor
    # Kirin 9000s ships with Maleoon 910; Honor/Huawei boards are in the same
    # table, so they must resolve too.
    "MNA-AL00": dict(ram_gb=8, storage_gb=(256, 512), soc="Kirin 9000s",
                     cores=8, gpu="Maleoon 910 MP4", abi=["arm64-v8a", "armeabi-v7a"],
                     screen=(1220, 2700, 444, 6.67)),
    "ALN-AL10": dict(ram_gb=12, storage_gb=(256, 512, 1024), soc="Kirin 9000s",
                     cores=8, gpu="Maleoon 910 MP4", abi=["arm64-v8a", "armeabi-v7a"],
                     screen=(1260, 2720, 458, 6.82)),
    "FOA-AL00": dict(ram_gb=12, storage_gb=(256, 512), soc="Kirin 9000s",
                     cores=8, gpu="Maleoon 910 MP4", abi=["arm64-v8a", "armeabi-v7a"],
                     screen=(1220, 2700, 444, 6.76)),
    "BVL-AN00": dict(ram_gb=12, storage_gb=(256, 512, 1024), soc="Snapdragon 8 Gen 3",
                     cores=8, gpu="Adreno (TM) 750", abi=["arm64-v8a", "armeabi-v7a"],
                     screen=(1280, 2800, 453, 6.8), gpu_series="adreno 750"),
    "REA-AN00": dict(ram_gb=12, storage_gb=(256, 512), soc="Snapdragon 7 Gen 3",
                     cores=8, gpu="Adreno (TM) 720", abi=["arm64-v8a", "armeabi-v7a"],
                     screen=(1200, 2664, 435, 6.7), gpu_series="adreno 720"),
    # ------------------------------------------------------------ Xiaomi
    "23116PN5BC": dict(ram_gb=12, storage_gb=(256, 512, 1024), soc="Snapdragon 8 Gen 3",
                       cores=8, gpu="Adreno (TM) 750", abi=["arm64-v8a", "armeabi-v7a"],
                       screen=(1440, 3200, 522, 6.73), gpu_series="adreno 750"),
    "23127PN0CC": dict(ram_gb=12, storage_gb=(256, 512), soc="Snapdragon 8 Gen 3",
                       cores=8, gpu="Adreno (TM) 750", abi=["arm64-v8a", "armeabi-v7a"],
                       screen=(1200, 2670, 460, 6.36), gpu_series="adreno 750"),
    "2210132G": dict(ram_gb=12, storage_gb=(256, 512), soc="Snapdragon 8 Gen 2",
                     cores=8, gpu="Adreno (TM) 740", abi=["arm64-v8a", "armeabi-v7a"],
                     screen=(1440, 3200, 522, 6.73), gpu_series="adreno 740"),
    "2211133G": dict(ram_gb=8, storage_gb=(128, 256), soc="Snapdragon 8 Gen 2",
                     cores=8, gpu="Adreno (TM) 740", abi=["arm64-v8a", "armeabi-v7a"],
                     screen=(1080, 2400, 414, 6.36), gpu_series="adreno 740"),
    "23078PND5G": dict(ram_gb=12, storage_gb=(256, 512, 1024), soc="Dimensity 9200+",
                       cores=8, gpu="Immortalis-G715 MC11", abi=["arm64-v8a", "armeabi-v7a"],
                       screen=(1220, 2712, 446, 6.67)),
    "23090RA98G": dict(ram_gb=12, storage_gb=(256, 512), soc="Dimensity 7200 Ultra",
                       cores=8, gpu="Mali-G610 MC4", abi=["arm64-v8a", "armeabi-v7a"],
                       screen=(1220, 2712, 446, 6.67)),
    "23090PC98G": dict(ram_gb=8, storage_gb=(128, 256, 512), soc="Dimensity 7200 Ultra",
                       cores=8, gpu="Mali-G610 MC4", abi=["arm64-v8a", "armeabi-v7a"],
                       screen=(1080, 2400, 396, 6.67)),
    "23090RA33G": dict(ram_gb=6, storage_gb=(128, 256), soc="Dimensity 6100+",
                       cores=8, gpu="Mali-G57 MC2", abi=["arm64-v8a", "armeabi-v7a"],
                       screen=(1080, 2400, 396, 6.67)),
    "22101316UG": dict(ram_gb=8, storage_gb=(128, 256), soc="Dimensity 1080",
                       cores=8, gpu="Mali-G68 MC4", abi=["arm64-v8a", "armeabi-v7a"],
                       screen=(1080, 2400, 395, 6.67)),
    "23013PC75G": dict(ram_gb=12, storage_gb=(256, 512), soc="Snapdragon 8+ Gen 1",
                       cores=8, gpu="Adreno (TM) 730", abi=["arm64-v8a", "armeabi-v7a"],
                       screen=(1440, 3200, 526, 6.67), gpu_series="adreno 730"),
    "23049PCD8G": dict(ram_gb=8, storage_gb=(128, 256), soc="Snapdragon 7+ Gen 2",
                       cores=8, gpu="Adreno (TM) 725", abi=["arm64-v8a", "armeabi-v7a"],
                       screen=(1080, 2400, 396, 6.67), gpu_series="adreno 725"),
    "22101320G": dict(ram_gb=8, storage_gb=(128, 256), soc="Snapdragon 778G",
                      cores=8, gpu="Adreno (TM) 642L", abi=["arm64-v8a", "armeabi-v7a"],
                      screen=(1080, 2400, 395, 6.67), gpu_series="adreno 642"),
}

# Model -> the retail configuration list, used when a model is not in _SPECS but
# its brand/name is known. Keeps every device with a plausible answer.
_BRAND_DEFAULTS = {
    "samsung": dict(ram_gb=8, storage_gb=(128, 256), soc="Exynos 1380", cores=8,
                    gpu="Mali-G68 MP5", abi=["arm64-v8a", "armeabi-v7a"],
                    screen=(1080, 2340, 400, 6.4)),
    "google": dict(ram_gb=8, storage_gb=(128, 256), soc="Google Tensor G3", cores=9,
                   gpu="Mali-G715 MC10", abi=["arm64-v8a", "armeabi-v7a"],
                   screen=(1080, 2400, 420, 6.2)),
    "xiaomi": dict(ram_gb=8, storage_gb=(128, 256), soc="Snapdragon 8 Gen 2", cores=8,
                   gpu="Adreno (TM) 740", abi=["arm64-v8a", "armeabi-v7a"],
                   screen=(1080, 2400, 400, 6.5), gpu_series="adreno 740"),
    "huawei": dict(ram_gb=8, storage_gb=(256, 512), soc="Kirin 9000s", cores=8,
                   gpu="Maleoon 910 MP4", abi=["arm64-v8a", "armeabi-v7a"],
                   screen=(1220, 2700, 444, 6.7)),
    "honor": dict(ram_gb=12, storage_gb=(256, 512), soc="Snapdragon 8 Gen 3", cores=8,
                  gpu="Adreno (TM) 750", abi=["arm64-v8a", "armeabi-v7a"],
                  screen=(1280, 2800, 453, 6.8), gpu_series="adreno 750"),
    "oneplus": dict(ram_gb=12, storage_gb=(128, 256), soc="Snapdragon 8 Gen 2", cores=8,
                    gpu="Adreno (TM) 740", abi=["arm64-v8a", "armeabi-v7a"],
                    screen=(1440, 3216, 525, 6.7), gpu_series="adreno 740"),
}

# SoC name -> the ARM micro-architecture the device really uses. Used for
# /proc/cpuinfo "CPU part" and for the "64-bit" row of Settings -> CPU.
# Only the newest big cores are ARMv9; everything else on the market is v8.2-A,
# and claiming v9 for an Exynos 1330 is a spec sheet contradiction.
_ARM9_SOCS = {"Exynos 2400", "Snapdragon 8 Gen 3", "Snapdragon 8 Elite",
              "Dimensity 9300", "Dimensity 9200+", "Google Tensor G3",
              "Google Tensor G4"}
_SOC_ARCH = {
    "snapdragon": "ARMv8.2-A (64-bit)",
    "exynos": "ARMv8.2-A (64-bit)",
    "dimensity": "ARMv8.2-A (64-bit)",
    "tensor": "ARMv8.2-A (64-bit)",
}

# Fabrication process per SoC family - Settings -> CPU shows this.
_SOC_FABRICATION = {
    "Snapdragon 8 Gen 3": "4 nm", "Snapdragon 8 Gen 2": "4 nm",
    "Snapdragon 8+ Gen 1": "4 nm", "Snapdragon 8 Gen 1": "4 nm",
    "Snapdragon 7+ Gen 2": "4 nm", "Snapdragon 778G": "6 nm",
    "Exynos 2400": "4 nm", "Exynos 2200": "4 nm", "Exynos 1380": "5 nm",
    "Exynos 1280": "5 nm", "Exynos 1330": "5 nm",
    "Google Tensor G3": "4 nm", "Google Tensor G2": "5 nm", "Google Tensor": "5 nm",
    "Dimensity 9200+": "4 nm", "Dimensity 7200 Ultra": "4 nm",
    "Dimensity 6100+": "6 nm", "Dimensity 1080": "6 nm",
}


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------
def _spec_lookup(model, brand=""):
    """Best spec entry for *model* (hardware code or marketing name)."""
    text = str(model or "").strip()
    if not text:
        return None, None
    # Exact hardware model code, then code prefix (SM-S928B -> SM-S928).
    if text in _SPECS:
        return text, _SPECS[text]
    for code, spec in _SPECS.items():
        if code.lower() == text.lower():
            return code, spec
    normalised = re.sub(r"[^A-Za-z0-9+]", "", text)
    for code in sorted(_SPECS, key=len, reverse=True):
        if normalised.lower().startswith(re.sub(r"[^A-Za-z0-9+]", "", code).lower()):
            return code, _SPECS[code]
    # Marketing name ("Samsung Galaxy S24 Ultra" -> "SM-S928" via profiles).
    try:
        from profiles.device_profiles import get_model_profile

        profile = get_model_profile(text)
        if profile:
            code = str(profile.get("model", ""))
            if code in _SPECS:
                return code, _SPECS[code]
            for key in sorted(_SPECS, key=len, reverse=True):
                if code.upper().startswith(key.upper()):
                    return key, _SPECS[key]
    except Exception:  # noqa: BLE001 - profiles is optional here
        pass
    return None, None


def _brand_spec(brand, model=""):
    key = str(brand or "").strip().lower()
    if key in _BRAND_DEFAULTS:
        return _BRAND_DEFAULTS[key]
    return _BRAND_DEFAULTS["samsung"]


def soc_arch(soc):
    """'ARMv9.0-A (64-bit)' / 'ARMv8.2-A (64-bit)' for *soc* - never guessed up."""
    name = str(soc or "").strip()
    if name in _ARM9_SOCS:
        return "ARMv9.0-A (64-bit)"
    low = name.lower()
    for family, arch in _SOC_ARCH.items():
        if family in low:
            return arch
    return "ARMv8.2-A (64-bit)"


def soc_fabrication(soc):
    return _SOC_FABRICATION.get(str(soc or ""), "5 nm")


# ---------------------------------------------------------------------------
# The claim: what the phone would report, clamped to what we can serve
# ---------------------------------------------------------------------------
def hardware_spec(model, brand="", *, real_ram_mb=None, real_storage_mb=None,
                  real_cores=None, avd_ram_mb=None, mode="retail"):
    """Specs the spoofed phone reports, clamped to what the AVD can really serve.

    ``real_*`` are measured on the running emulator (see
    ``core.spec_probe``). ``avd_ram_mb`` is the AVD's configured ``hw.ramSize``.

    Two different numbers matter and are easy to mix up:

    * **RAM**: a 2 GB AVD pretending to be a 12 GB flagship is one of the
      loudest emulator tells, so in the default ``retail`` mode Settings
      reports the phone's real retail RAM (8/12 GB) and ``availMem`` /
      ``usedMem`` are scaled to keep the same *ratio* the VM really has, so
      "8 GB total, 1.2 GB free" never looks like a contradiction.
      ``mode="honest"`` instead caps the figure at what the VM really has.
      Either way the physical limit is the AVD's: the dashboard shows both
      numbers so nothing is hidden.
    * **Storage**: Settings shows the *data partition* size, and an AVD's data
      partition (6 GB by default) is far smaller than a real 256 GB phone. It
      is safe to report the retail size, so we do, but the free/used split is
      scaled from the real numbers so "used + free = total" stays true instead
      of showing 250 GB free on a 6 GB disk.

    Returns a dict with the reported values, the clamping decisions and notes.
    """
    code, spec = _spec_lookup(model, brand)
    if spec is None:
        spec = _brand_spec(brand, model)

    notes = []
    retail_ram_mb = int(spec["ram_gb"]) * 1024
    # The configuration a shop actually sells: the middle option (256 GB for a
    # 256/512/1024 GB phone), never the pointless 1 TB maximum.
    storage_options = sorted(int(x) for x in spec["storage_gb"])
    retail_storage_mb = storage_options[min(1, len(storage_options) - 1)] * 1024

    # ---- RAM: cap at reality (an impossible RAM figure is emulator bait).
    if real_ram_mb:
        available = int(real_ram_mb)
    elif avd_ram_mb:
        available = int(avd_ram_mb)
    else:
        available = retail_ram_mb
    honest = str(mode or "retail").lower() == "honest"
    ram_reported = min(available, retail_ram_mb) if honest else retail_ram_mb
    if ram_reported > available:
        notes.append(
            f"Settings reports the {spec['ram_gb']} GB of the {code or model}; "
            f"the AVD itself has {round(available / 1024, 1)} GB, so very large "
            "allocations are still limited by the VM (that is honest and "
            "expected - it is not hidden).")
    elif ram_reported < retail_ram_mb:
        notes.append(
            f"This AVD has {round(available / 1024, 1)} GB of RAM, so the "
            f"{spec['ram_gb']} GB of the {code or model} is reported as "
            f"{round(available / 1024, 1)} GB (honest mode).")

    # ---- Storage: report the retail size, scale the used/free split.
    storage_reported = retail_storage_mb
    # Used space on a phone that has been set up: system image + apps. Scaled
    # to the reported size so used + free == total (Settings computes free as
    # total - used, and a mismatch is visible).
    storage_used_mb = int(storage_reported * 0.34)
    storage_free_mb = max(0, storage_reported - storage_used_mb)
    if real_storage_mb and int(real_storage_mb) < storage_reported:
        notes.append(
            f"Settings reports the {code or model}'s {round(storage_reported / 1024)} GB "
            f"internal storage; the AVD data partition is "
            f"{round(int(real_storage_mb) / 1024, 1)} GB, which is what apps can "
            "really write.")

    cores = int(real_cores or spec["cores"])
    # Never report more cores than the VM has: /proc/cpuinfo would disagree.
    cores = min(cores, int(real_cores)) if real_cores else cores

    width, height, dpi, inches = spec["screen"]
    return {
        "matched_model": code,
        "soc": spec["soc"],
        "cpu_cores": cores,
        "cpu_arch": soc_arch(spec["soc"]),
        "cpu_fabrication": soc_fabrication(spec["soc"]),
        "cpu_governor": "schedutil",
        "cpu_frequency_label": "-",  # Settings shows "-" when it cannot read it
        "abi": list(spec["abi"]),
        "gpu": spec["gpu"],
        "gpu_series": spec.get("gpu_series"),
        "ram_retail_mb": retail_ram_mb,
        "ram_reported_mb": ram_reported,
        # What the VM can really serve - shown next to the claim, never hidden.
        "ram_available_mb": available,
        "ram_mode": "honest" if honest else "retail",
        "ram_claimed_gb": spec["ram_gb"],
        "storage_retail_mb": retail_storage_mb,
        "storage_reported_mb": storage_reported,
        "storage_used_mb": storage_used_mb,
        "storage_free_mb": storage_free_mb,
        "screen": {"width": width, "height": height, "dpi": dpi, "inches": inches},
        "notes": notes,
        # Kept for callers that want the raw retail entry.
        "retail": {
            "ram_gb": spec["ram_gb"],
            "storage_gb": list(spec["storage_gb"]),
            "soc": spec["soc"],
            "cores": spec["cores"],
        },
    }


def ram_label(spec):
    """'8.0 GB' - what Settings -> About shows for *spec*."""
    mb = spec.get("ram_reported_mb") or 0
    return f"{mb / 1024:.1f} GB"


def storage_label(spec):
    """'256 GB' (or '128 GB' exactly) for the reported storage."""
    gb = (spec.get("storage_reported_mb") or 0) / 1024
    return f"{gb:.0f} GB" if abs(gb - round(gb)) < 0.05 else f"{gb:.1f} GB"
