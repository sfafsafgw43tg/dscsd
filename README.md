# AntiDetect Android Emulator

A cross-platform Python toolchain that wraps the official Android SDK emulator, generates a unique synthetic device identity per run, and applies **multi-layer anti-detection** — at boot/config level (AVD `config.ini` + ADB `setprop`), at the hardware layer (IMEI/MAC/GPU/sensors/battery), and at runtime (Frida hooks on the Java API, native `libc`, and Google attestation).

> ⚠️ **Educational / research project.** Frida hooking, SafetyNet/Play Integrity forging, and identity spoofing touch anti-fraud systems. Use only on devices and accounts you own or are authorized to test. Hardware-key attestation cannot truly be defeated — the included `safetynet_bypass.js` forges the JWS result and passes basic checks only.

---

## ✨ Features

### 🔑 Synthetic identity generation (`profile_generator.py`)
Every device gets a full, internally-consistent identity:
- **Luhn-valid 15-digit IMEI** (+ `imei2` for dual-SIM) using real TAC prefixes per brand (Samsung, Xiaomi, Pixel, Huawei).
- Brand-specific **serial numbers** (Samsung `R5CT…`, Xiaomi 16-char, Pixel 12-char, OnePlus `NB…`).
- 16-hex **Android ID**, **GSF ID**, UUIDv4 **advertising ID**.
- Three **MAC addresses** (Wi-Fi, Bluetooth, generic) using **real vendor OUI prefixes** per brand.
- 3–8 realistic nearby **Wi-Fi SSIDs** (NETGEAR/TP-LINK/ASUS with `_5G`/`_Guest` suffixes).
- 50+ real-device profiles (`device_profiles.py`) with genuine model/device/board/build-fingerprint strings.

### 🛡️ Three-tier spoofing
1. **Boot / config (`spoof/system_*.py`)**
   - Per-partition props (`ro.product.{system,vendor,odm,product,system_ext}.*`) — a commonly-missed detection vector.
   - Emulator indicators hidden: `ro.kernel.qemu=0`, `ro.boot.qemu=0`, and `ro.hardware`/`ro.boot.hardware` set to the spoofed SoC board (not `ranchu`/`goldfish`, which are themselves emulator tell-tales).
   - `SystemSpoofer`: `adb root` + `remount`, build props, hide emulator strings.
   - Launches pass the spoofed `ro.*` props via `emulator -prop` (the AVD `config.ini` only holds valid `hw.*` keys — `ro.*` lines there are ignored).
2. **Hardware (`spoof/hardware_spoof.py`, `gpu_spoof.py`, `sensor_simulator.py`, `battery_spoof.py`)**
   - IMEI via `service call iphonesubinfo`, serial, android_id, file hiding.
   - **GPU**: one coherent chip per device — renderer, vendor, EGL vendor, driver version, GL extension list and GL limits all come from the same chip profile (Adreno 660/730/740/750, Mali-G68/G610/G710/G715/G720, Xclipse 920/940).
   - **Sensors**: threaded daemon with `idle` / `pocket` / `hand` motion modes to defeat static-sensor detection.
   - **Battery**: flagship/midrange/budget profiles with time-varying telemetry.
3. **Runtime Frida hooks (`frida_toolkit/scripts/*.js`)**
   - `anti_detect.js` — `Build`, `TelephonyManager` (IMEI/IMSI/MEID/number/SIM serial), `Settings$Secure`, `File.exists()` (18-entry emulator-path blacklist), and the GPU: `GLES20.glGetString` (vendor/renderer/version/extensions/GLSL), `glGetIntegerv` limits, `glGetShaderPrecisionFormat`, **native `glGetString`** in `libGLESv2` (Unity/Unreal/Flutter bypass the Java API) and `EGL14.eglQueryString` (EGL vendor/version/extensions).
   - `native_hooks.js` (380 lines) — libc-level interception, 31-entry file blacklist, `/proc/cpuinfo` rewriting (`QEMU Virtual CPU` → `Qualcomm Kryo 585`, etc.).
   - `safetynet_bypass.js` (334 lines) — forges SafetyNet/Play Integrity JWS attestation results.

### 🌐 Anonymity
- `ProxyManager` supports HTTP and SOCKS5 proxies, wired into the emulator via `-http-proxy`.
- **Host webcam passthrough**: each device can use a real webcam (`webcam0`, `webcam1`, …) or a USB camera (`usb0`, …) per facing, so the front and back cameras can be two genuinely different cameras. Configured per device, written to the AVD `config.ini` and enforced with `emulator -camera-front/‑back` at launch.

---

## 🏗️ Architecture

```
main.py (Click CLI)            gui/app.py (Flask + SocketIO dashboard)
   │
   ▼
core/device_manager.py ── DeviceManager: create/list/launch/stop, AVD config.ini,
   │                        system+hardware+Frida spoofing orchestration
   └── core/hook_manager.py ── HookManager: Frida session lifecycle

spoof/   → SystemSpoofer, HardwareSpoofer, GPUSpoofer, SensorSimulator, BatterySpoofer
profiles/→ profile_generator (identity), device_profiles (50+ real models)
frida/   → FridaHooks (inject), FridaServerManager (auto-download frida-server 16.1.4),
           scripts/anti_detect.js, native_hooks.js, safetynet_bypass.js
anonymity/ → ProxyManager (HTTP/SOCKS5)
```

Generated device identities are saved as JSON under `data/devices/` (gitignored).

---

## 🚀 Getting Started

### Prerequisites
- **Python 3.8+**
- **Android SDK** with `emulator`, `adb`, `avdmanager`, `sdkmanager` — plus at least one system image, by default `system-images;android-34;google_apis;x86_64` (userdebug, root capable). Play Store images are also supported but cannot run frida-server.

  The SDK is auto-detected from `ANDROID_SDK_ROOT`, then `ANDROID_HOME` / `ANDROID_SDK_HOME`, then the OS default location, then `PATH`. No editing required:
  ```bash
  # Linux / macOS
  export ANDROID_SDK_ROOT="$HOME/Android/Sdk"
  # Windows (PowerShell)
  setx ANDROID_SDK_ROOT "$env:LOCALAPPDATA\Android\Sdk"
  ```
  `python main.py check_sdk` prints exactly what was found and exits non-zero if something is missing.
- **Hardware acceleration** — VT-x/AMD-V enabled in BIOS plus KVM (Linux), WHPX/HAXM (Windows), or the Hypervisor.framework (macOS). Without it, x86_64 images cannot boot; the launcher detects this with `emulator -accel-check` and warns.
- **Root** on the emulator for `service call` IMEI spoofing and protected `ro.*` props. `adb root` only works on **userdebug** images — that is why the default image is `google_apis` and **not** `google_apis_playstore`. The tool still falls back gracefully (`su -c` → plain shell) and treats failures as non-fatal.
- **Frida server** on the device — `FridaServerManager` resolves the version of the installed `frida` Python client (so client and server always match), downloads `frida-server-<version>` for the device's real ABI (`x86_64`, `arm64`, …), validates that the file really is an ELF binary for that ABI, pushes it to `/data/local/tmp/`, verifies the size on the device, and starts it with `adb root` (or `su`/plain shell on user builds).

### ⚡ One-click start (Windows)
Double-click **`START.bat`**. It will:

1. find Python (uses `.venv` if present, then the `py` launcher, then `python`)
2. install the Python dependencies (and `pywebview` for the desktop window)
3. let you **pick your Android SDK folder** with a native folder dialog (or type/paste the path)
4. install/repair the SDK components: licenses, `platform-tools`, `emulator`, and a system image — downloading Google's command-line tools first if `sdkmanager` is missing
5. open the app in its **own desktop window** (no browser tab)

While it boots, the card shows a **step-by-step progress panel** instead of a
spinner that never changes:

```
Starting device                                   1m 32s · step 3/7
[==========                              ] 44%
✓ Checking SDK, AVD and ports
✓ Starting the emulator process
◐ Booting Android                      <- this step is running
○ Enabling root
○ Applying & verifying the spoof
○ Attaching Frida hooks
○ Finishing up
Now  Booting Android (92s elapsed, 208s left - adb: device)
```

Each finished step keeps a one-line note of what it actually checked
(`Checking the AVD antidetect_google_847dfc`), so a tick is evidence, not
decoration. If a step goes quiet for 20 s the panel stops pretending to be
busy and says so:

```
Still  Still checking hardware acceleration — 34s with no new step.
       The emulator has not written a log line yet.        (34s)
```

If a launch ever refuses to start because of a port conflict, the app now
says which ports are busy and why, frees leftover emulator processes it owns,
and (worst case) lets the emulator try anyway rather than refusing on a guess.
`python main.py doctor` lists the real listeners under **Emulator ports**.

That is a first boot: several minutes is normal (`-no-snapshot` means no
quick-boot cache). If a step fails, the panel turns red, says which step and
why, and the card offers **↻ Retry launch** — you are never left guessing
whether it is slow or dead.

It **never creates a device for you**: the dashboard opens on its
*No virtual devices yet* screen and the first phone in the list is always one
you picked in **+ New Device**. (An earlier version silently made a
`first_device` on first start - wrong model, wrong cameras, and a card you had
to delete before your own device.)

Options pass straight through, e.g.:
```bat
START.bat --android 13
START.bat --sdk "C:\Android\Sdk"
START.bat --create my_phone                  :: explicitly create + boot "my_phone"
START.bat --create my_phone --model "Google Pixel 8" --android 15
START.bat --browser
START.bat --skip-install                     :: deps + SDK check, create nothing
```

Prefer an isolated environment? Create one first and the launcher uses it automatically:
```bat
python -m venv .venv
START.bat
```

On Linux/macOS use `./start.sh` (same behaviour).

### Install (manual)
```bash
pip install -r requirements.txt
pip install pywebview        # optional: native desktop window instead of a browser tab
```

### 🗂️ Choosing the Android SDK folder
The SDK folder is remembered in `data/settings.json` and takes priority over environment variables.

- **GUI:** ⚙️ Settings → *Android SDK folder* → **Browse…** (native folder picker) → **Save**. The same panel has an **Install** button that installs `platform-tools`, `emulator` and the selected system image with live progress.
- **CLI:** `python main.py set-sdk "C:\Android\Sdk"` (or `python main.py set-sdk --pick` for a folder dialog), then `python main.py setup` to install missing components.
- **Env vars** still work as a fallback: `ANDROID_SDK_ROOT` / `ANDROID_HOME` / `ANDROID_SDK_HOME`.

If a saved folder is deleted or isn't an SDK, it is ignored automatically so the app keeps working with env/default detection.

### Usage (Click CLI)
```bash
python main.py check_sdk                                        # verify SDK/ADB
python main.py set-sdk --pick                                   # choose the SDK folder
python main.py setup -a 14                                      # install/repair SDK components
python main.py models                                           # list 50+ phone models
# Model names resolve exactly; "Pixel 8" is the base model, never "Pixel 8 Pro".
# An exact hardware code also works (that is what fingerprint sites print).
python main.py create -m "Samsung Galaxy S24" -a 14             # create device (rootable image)
python main.py create -m "Samsung Galaxy S24" -a 14 -i google_apis_playstore  # Play Store (no root)
python main.py create -m "Samsung Galaxy S24" -a 14 -p socks5://127.0.0.1:1080
python main.py create -m "Samsung Galaxy S24" --camera-back webcam0 --camera-front webcam1  # two independent real cameras
python main.py create -m 23116PN5BC                                        # by exact hardware model code
python main.py create -m "Samsung Galaxy S24" --camera-both webcam0   # same webcam on both (one shared camera)
python main.py create -m "Samsung Galaxy S24" --camera-back webcam0 --camera-front emulated
python main.py set-camera <name> --both webcam0                       # change an existing device
python main.py list                                             # list saved devices
python main.py launch <name> --frida                            # launch + runtime hooks
python main.py launch <name> --no-wait --boot-timeout 300       # don't block on boot
python main.py launch <name> --wipe                             # wipe data, clean boot
python main.py logs <name>                                      # emulator log tail
python main.py stop <name>
python main.py delete <name>                                    # remove device + AVD
python main.py gui --host 0.0.0.0 --port 8080                   # web dashboard

# Apply spoofing to an already-running emulator (serial auto-detected):
python apply_antidetect.py -m "Samsung Galaxy S24 Ultra"
python apply_antidetect.py --device <name>                      # reuse saved identity
python update_devices.py                                        # re-apply props to AVDs
```

### ✅ Verified spoofing (not "trust me" spoofing)
Spoofing writes are **read back and verified** with `getprop`. `ro.*`
properties are read-only after init, so the pipeline escalates until the
guest really reports the identity: runtime `setprop` → `/data/local.prop`
+ reboot (works on some userdebug images) → `adb disable-verity` +
remount + **rewrite build.prop** + reboot (works where local.prop can't —
Android 14 google_apis, proven on-device). Build number, display id,
incremental, `ro.build.type=user`, tags=release-keys and device name are
all derived from the profile fingerprint, so Settings → About shows a real
retail phone — model, serial, build number, no "userdebug … dev-keys"
tell. The per-prop ✓/✗ report ("Spoof verify report") lives in 📋 Details;
a `⚠ Spoof incomplete` chip appears if any prop is unchangeable on an
image (Frida hooks still cover `Build.*` for hooked apps). Note: the
build.prop rewrite edits the shared system image — last write wins for
every AVD using that image. Originals are backed up on-device at
`/data/local/antidetect-buildprop-backup/`.

### 🎮 GPU spoofing (one coherent chip)
An emulator is caught on graphics by *mismatches*: an "Adreno" renderer with
ARM's Mali shader extensions, a Qualcomm driver version on a Mali chip, the
emulator's own `Android Emulator OpenGL ES Translator`/`SwiftShader` strings,
or phone-class limits (`16384` texture size with a `8192` viewport).

Every device therefore gets **one** GPU profile (`profiles/gpu_profiles.py`)
and every answer is derived from it:

| Query | Spoofed |
|---|---|
| `glGetString(GL_VENDOR / GL_RENDERER / GL_VERSION / GL_EXTENSIONS / GL_SHADING_LANGUAGE_VERSION)` | vendor + chip + real driver version + that vendor's extension list |
| `GLES20.glGetIntegerv` (`GL_MAX_TEXTURE_SIZE`, `GL_MAX_VIEWPORT_DIMS`, `GL_MAX_VERTEX_ATTRIBS`, `GL_MAX_TEXTURE_IMAGE_UNITS`, `GL_MAX_RENDERBUFFER_SIZE`, `GL_MAX_COMBINED_TEXTURE_IMAGE_UNITS`) | the chip's real limits |
| `glGetShaderPrecisionFormat` | 23-bit precision triple (not SwiftShader's) |
| native `glGetString` in `libGLESv2.so` | same answers (engine/Unity/Flutter path) |
| `EGL14.eglQueryString` (`EGL_VENDOR`, `EGL_VERSION`, `EGL_EXTENSIONS`) | matching EGL vendor |
| native `glGetStringi` (GLES3 extension enumeration) | the chip's extension list |
| native `eglQueryString` in `libEGL.so` | matching EGL vendor/version/extensions |
| native `glGetIntegerv` / `glGetShaderPrecisionFormat` (renderer process) | the chip's limits/precision |
| `SystemProperties` (`ro.opengles.version`, `debug.hwui.renderer`) | GLES 3.2, software-UI flags |
| `/proc/cpuinfo`, `ro.product.*`, build props | already handled by the CPU/prop spoofing |

Answers are **unconditional** — every GPU query is answered from the identity,
never "only when the real string looks emulator-ish". That filter is what let a
passthrough host GPU through as
`Google (ATI Technologies Inc.) / Android Emulator OpenGL ES Translator (AMD Radeon RX 6700 XT)`.

**The renderer itself no longer uses the host GPU.** Patching GL strings is not
enough on its own: with host passthrough (`-gpu auto` / `host` / `angle`) the
desktop card is still the driver the guest loads, and it leaks through Vulkan,
`/proc`, EGL init logs and any process the hooks did not reach. Devices are
therefore launched with the emulator's guest/software renderer
(`-gpu swiftshader_indirect`, also written to `hw.gpu.mode` in `config.ini`) and
`resolve_gpu_mode()` refuses a host-passthrough mode for any spoofed device —
the substitution is logged and saved as `device["gpu_mode"]`, and 📋 Details
shows it as *Emulator renderer*. Override with `EMULATOR_GPU_MODE` only if you
knowingly want another backend.

**WebGL is a separate process.** A WebGL fingerprint (`UNMASKED_VENDOR_WEBGL` /
`UNMASKED_RENDERER_WEBGL`, the `gpu:` row) is read in Chromium's sandboxed
renderer/GPU process (`<package>:privileged_process0`), not in the app's main
process. Hooking an app therefore also attaches the graphics-only script
(`frida_toolkit/scripts/gpu_hooks.js`: native GL/EGL + Java GLES/EGL, no libc) to
that process, re-scanning a few seconds later because browsers start it lazily.

The identity is generated with the device (`device["gpu"]`, also visible in
📋 Details as *GPU*, *GL Version* + an EGL/limits/extension fact grid), shipped
into every Frida payload, and reported by **Spoof** (`Antidetect applied … GPU:
ARM Mali-G720`) and `python apply_antidetect.py --device <name>`. **Details →
↻ New identity** regenerates it along with the IMEI/serial/MAC.
`python main.py doctor` cross-checks every saved device and flags a
renderer/vendor, extension or limits contradiction.

`ro.hardware.egl` / `ro.hardware.vulkan` are deliberately **never** touched:
Android's loader resolves `libEGL_<value>.so` / `vulkan.<value>.so` from them, so
pointing them at "adreno"/"arm" makes RenderThread load a missing driver and
crashes every app. Only cosmetic props (`debug.hwui.renderer=skiagl`,
`debug.egl.hw=1`, `debug.sf.hw=1`) are written; the rest is hook-level.

### 🖼️ Custom system images
Any image already sitting in `SDK/system-images/` (Android Studio
downloads, extracted archives, modified images, android-desktop builds)
is auto-listed in the New Device dialog. Or use **📂 Import image from
folder…**: point at a directory containing `system.img` (e.g. an extracted
Lineage/modified build), pick the Android version + tag, and it's copied
into the SDK as a proper package and selected immediately.

### 🩺 Doctor (health check)
When anything misbehaves, run it instead of guessing:

```powershell
python main.py doctor
```
or in the app: **⚙️ Settings → 🩺 Health check → Run doctor**. It checks the
Python version, SDK layout, duplicate cmdline-tools copies, platform-tools
age, adb server, installed system images, settings file, Frida client/server
version alignment, hardware acceleration and every saved device (missing AVD
files, zombie emulators holding ports, stale pid/serial state) — each item
comes with a concrete fix hint.

### 🔓 Automatic rooting
Devices are **rooted automatically right after boot** (`adb root`, with the adbd-restart handled and verified via `shell id`), so Frida hooks and property spoofing work without any manual step.

- Toggle globally in **⚙️ Settings → 🔓 Rooting**, or per device in the **New Device** dialog ("Root automatically after launch", on by default) / CLI `--no-root`.
- Only the **Google APIs** image allows `adb root`; on Play Store images the card shows **⚠ No root** and the device file records why.
- The device card shows the *actual* state: **✓ Root** (rooted this session), **✓ Root capable** (will be rooted at launch), **⚠ Root failed** (hover for the reason), **⚠ No root** (Play Store image). Root state clears on stop because it never survives a reboot.
- `adb root` gives root to the **shell / Frida**, not to apps — a "Root Checker" app will still say *not rooted* (no `su` binary is installed; that would need Magisk, which anti-detect profiles usually want to avoid). Verify with `adb shell id` → `uid=0(root)`. Apps probing `Settings.Global.adb_enabled` are answered `0` by the Frida hooks, so adb can stay up without being detectable.

### ♻️ Spoofing & hooks survive restarts
The identity spoof is written into RAM (`/dev/__properties__` via `resetprop-rs`) and `frida-server` is a process, so **nothing about them is on disk in the guest** — a reboot clears both. The app therefore remembers *what you asked for* and puts it back:

- Applying **🎭 Spoof** / **🔧 Hooks** (or launching) records `auto_spoof` / `auto_hooks` plus the boot id the values belong to in `data/devices/<name>.json`.
- After an emulator reboot the card shows **⚠ Needs restore** (Details: *"Spoofing/hooks lost on restart"*, pipeline cells *"Lost on restart"*) instead of a stale `✓`.
- They are re-applied automatically — when the device is launched again, when the dashboard starts (background sweep for every stale device), on the card's **↻ Restore** button, via `POST /api/devices/<name>/restore`, or from the CLI:
  ```bash
  python main.py restore             # every device that needs it
  python main.py restore my_device   # just one
  ```
- **Details → Keep Applied After Restart** has per-device **Spoofing** / **Frida hooks** checkboxes (**Apply now** next to them). Unticking one opts that device out for good (`python main.py keep-applied my_device --no-hooks`).
- `Stop` clears only the *live* flags, so relaunching still re-applies everything. `python main.py doctor` flags devices whose protection was lost.

### 📷 Cameras (including your real webcam)
When creating a device (GUI **New Device** dialog or `create` command) you can choose a source for the **back** and **front** camera:

| Value | Meaning |
|-------|---------|
| `virtualscene` | 3D virtual room (default back camera) |
| `emulated` | Synthetic camera (default front camera) |
| `webcam0`, `webcam1`, … | A real webcam on the **host** machine (`webcam0` is usually the built-in/default one) |
| `usb0`, `usb1`, … | An external USB camera (the emulator uses it for both facings) |
| `none` | No camera |

**Front and back are independent cameras.** Pick a different source per facing
to get two different images, and every device additionally **looks** like it
has two unrelated physical sensors to the apps inside it:

- **Two different webcams** preset → back `webcam0`, front `webcam1`;
  **Two virtual sources** preset → back `virtualscene`, front `emulated`.
- Each camera reports the sensor the **selected phone actually has**
  (`profiles/device_profiles.py` → `CAMERA_PROFILES`, e.g. a Galaxy S24 Ultra
  reports its 200 MP ISOCELL HP2SX main + 12 MP selfie sensor, a Pixel 8 its
  ISOCELL GN2), never the emulator's generic synthetic camera and never a
  random "phone-ish" sensor. Plus its own serial, resolution, focal length,
  aperture, orientation, pose, frame rate and **stream-size list** (the
  emulator's 640×480/1280×720-only list is itself a tell). Unknown/custom
  models fall back to a per-facing pool so the two still differ.
  (`identity.camera_specs`, shown in 📋 Details). The Frida camera hook
  (`frida_toolkit/scripts/camera_hooks.js`) applies them to Camera2
  characteristics and the Camera1 `CameraInfo`/`Parameters`, so
  `getCameraCharacteristics("0")` and `("1")` no longer return the same
  sensor, and `SCALER_AVAILABLE_STREAM_CONFIGURATIONS` lists that sensor's
  sizes. This works on existing devices too (specs are backfilled the next
  time you change cameras) and only needs hooks to be applied.
- Using the *same* source on both facings (`webcam0` + `webcam0`) still works
  but is reported as a hint, because then both cameras show identical images.
  You need two different inputs for two different pictures.

The GUI has **Two different webcams** / **Two virtual sources** presets (plus the older **“Use webcam0 for both”** shortcut, which warns that both cameras will then see the same images), and the device details dialog can change the cameras later (restart the device to apply). Global defaults live in **⚙️ Settings → Default cameras** (saved to `data/settings.json` and pre-filled in the **New Device** dialog). The choice is saved in `data/devices/<name>.json`, written to `hw.camera.front`/`hw.camera.back` in the AVD `config.ini`, and also passed at launch as `-camera-front`/`-camera-back` so it always wins.

Notes:
- If a webcam can't be opened (wrong index, camera in use by another app, or no webcam at all) the emulator exits; the launch error now names the camera as the likely cause. Try `webcam1`, or close the app that is using the camera.
- Webcam passthrough requires the emulator to run with a GUI (not `-no-window`), which is the default here.

### ✈ Airplane mode
Airplane mode is **manual only**. The app never turns it on by itself — not on
launch, not when a spoof fails to verify, not during a restore. A device's
radios stay exactly as you left them, so a half-applied tweak can never leave
you wondering why nothing has network.

You can still switch it yourself whenever you want:

```
python main.py airplane <device> on
python main.py airplane <device> off
```

or via `POST /api/devices/<name>/airplane` with `{"enable": true|false}`.
The device confirms the change with `settings get global airplane_mode_on`
before the app reports success, and 📋 Details shows the result
(`Radios: ✈ Airplane` / `Online`) or the reason it failed.

Spoof verification is **reported**, not enforced: if the spoof does not take,
Details keeps the warning and the error text (`is still NOT spoofed after
launch`) — and the phone stays online.

### 🧱 System image: root vs Play Store
Every device records the exact system image it was created with, and the dashboard shows a **✓ Root** / **⚠ No root** tag.

| `-i` / GUI value | Package | `adb root` | Frida & prop spoofing |
|---|---|---|---|
| `google_apis` (default) | `system-images;android-<api>;google_apis;x86_64` | ✅ works | ✅ works |
| `google_apis_playstore` | `system-images;android-<api>;google_apis_playstore;x86_64` | ❌ `adbd cannot run as root in production builds` | ❌ frida-server cannot run, `setprop` on protected props is refused |

Play Store images are user builds: the bootloader is locked, `su` is absent and `adb root` is rejected. If you pick one (CLI or **New Device** dialog) you get an explicit warning instead of a silent failure — apps can still be installed, but use `google_apis` when you need Frida. Devices store `image`, `image_variant` and `rootable` in `data/devices/<name>.json`; recreate the device with `-i google_apis` to switch (the image is baked into the AVD).

### Dashboard: desktop app or browser
`python main.py gui` opens the dashboard in its **own desktop window** when `pywebview` is installed (falling back to a browser otherwise).

```bash
python main.py gui --desktop     # native window (default when pywebview is present)
python main.py gui --browser     # browser tab, http://127.0.0.1:8080
python main.py gui --host 0.0.0.0 --port 8080   # expose to your LAN
python bootstrap.py              # one-shot: deps + SDK setup + desktop app
```

The dashboard is a Flask + Socket.IO app; the desktop shell serves it on a random loopback port inside a native window, so it works offline (the Socket.IO CDN script is optional and the UI degrades to polling). REST endpoints live under `/api/...` for device CRUD, launch, stop, model listing, SDK settings and component installation.

Launching from the GUI is **asynchronous**: `POST /api/devices/<name>/launch` returns immediately and the dashboard polls `GET /api/devices/<name>/status`, which reports `starting` → `booting` → `running`/`error` plus the emulator log tail, so a slow boot or a hard failure is never hidden behind a hung request.

### 🩺 Troubleshooting a launch that won't start
1. `python main.py check_sdk` — confirms `emulator`, `adb`, `avdmanager`, `sdkmanager`, installed system images and AVDs (`ANDROID_SDK_ROOT` is the usual culprit).
2. `python main.py logs <name>` — every launch writes the emulator's stdout/stderr to `data/logs/<name>.log` (previously sent to `DEVNULL`, so failures were invisible). The CLI/GUI error message also names the likely cause (missing acceleration, KVM/HAXM/WHPX, permissions, RAM, GPU backend, corrupt AVD).
3. Retry cleanly: `python main.py launch <name> --wipe`. Launches use `-no-snapshot`, so a corrupt quickboot snapshot cannot block startup.
4. Multiple emulators are supported: each launch picks the next free console port (5554, 5556, …) and records its own serial, so spoofing/stop/Frida always target the right instance.

### 🔁 "I stop the device and can't launch it again"
Stop now kills the **whole emulator process tree** (on Windows the VM runs as a
separate `qemu-system-x86_64.exe` child) and waits until the console stops
answering and the ports are free, so the next Launch gets a clean slate:

- **Real adb is lazy**: it keeps listing a just-killed emulator for a few
  seconds. Launch detects that ghost (the emulator console is the authority)
  and clears it instead of failing with *"Device already running"* — the old
  behaviour, which looked like a dead Launch button.
- **Stuck emulator?** A failed Stop says why and offers **Force stop**
  (`python main.py stop <name> --force`). It kills the tree and frees only that
  device's console/adb port pair. `python main.py doctor` also flags a port that
  is busy with no console answering.
- **"another emulator instance is running"** after a hard kill: the stale
  `*.lock` files in the `.avd` folder are removed automatically before launch.
- If you launched an emulator **outside** the app (Android Studio), that copy
  owns the ports/AVD — close it or use Force stop.

### 🔌 "Device is not ready (state: offline)" / Spoof & Frida do nothing
`adb root` restarts adbd, so for a few seconds afterwards `adb devices` shows the
emulator as `offline` and every `adb shell`/`push` fails. The app used to check
the state exactly during that window and abort ("not ready to spoof (state:
offline)", "frida-server did not start"), which made Spoof/Hooks look broken
right after launch. A stale adb server (e.g. started by Android Studio with a
different `platform-tools`) leaves the device stuck offline in the same way.

Now:

- `adb root` waits until adbd is back and verifies `uid=0` before anything else runs;
- boot detection, spoofing, Frida install and hook injection call
  `adb.wait_until_ready()`, which rides out the offline window and heals it with
  `adb reconnect` (and an adb-server restart as a fallback);
- **Settings → Repair ADB** runs the same recovery manually and shows
  `adb devices -l` plus the adb version; **Check** shows the adb path/version;
- failures name the real cause instead of a generic toast.

API: `POST /api/adb/repair` (`{"serial": "emulator-5554"}`, optional),
`GET /api/adb/status`.

If Repair cannot bring the device back, the platform-tools in the selected SDK are
probably older than the system image: run **Settings → Install / repair SDK
components** (it installs `platform-tools`), or `python main.py setup -a 14`.

### 🧩 Frida says "not installed" / installation fails
The Frida card and the spoofing buttons always target the emulator that is **actually running**. The serial is resolved from the device's saved launch serial, or by matching the running emulator's AVD name (`adb -s <serial> emu avd name`) - so a second emulator on port 5556, or one started in Android Studio, is no longer mistaken for `emulator-5554`. A resolved serial is written back to `data/devices/<name>.json`.

**Most common cause:** the device was created with a Play Store image. Those are user builds, so `adb root` is refused and `frida-server` cannot be started — the tool now warns about this at creation time and the status card shows the reason. Create the device with the **Google APIs (root)** image. Other causes are reported verbatim (no device, `adb push` failed, GitHub blocked, binary is an HTML page, wrong ABI, server exited):

1. Start the emulator and wait for the home screen, then press **Download & Install** again. The status card also shows a full `diagnose()` dump: `adb` path, device state, ABI, build type, image rootable, client/server versions.
2. **From file…** - download `frida-server-<version>-android-<abi>.xz` from the [Frida releases](https://github.com/frida/frida/releases) in a browser and pick it; the `.xz`/`.zip` archive is decompressed and validated automatically (ELF magic, ABI, size).
3. Manual: copy the extracted binary to `data/frida-server`, then press **Install** (it is re-validated, pushed, size-checked and started).
4. Check the device side with `adb shell /data/local/tmp/frida-server` (or `adb shell cat /data/local/tmp/frida.log`).
5. Client/server versions must match: the manager downloads the same version as your installed `frida` client (fallback `16.1.4` when the client is missing).

API: `GET /api/status/frida?serial=…` (auto-detects when omitted), `POST /api/frida/install` (`{"serial": …, "local_path": …}`), `GET /api/frida/install/status`, `POST /api/frida/pick-server`.

Environment knobs: `EMULATOR_RAM` (MB, default 4096), `EMULATOR_CORES` (default 4), `EMULATOR_GPU_MODE` (default `swiftshader_indirect`; host passthrough modes are refused for spoofed devices), `EMULATOR_BOOT_TIMEOUT`/`EMULATOR_START_TIMEOUT` (in `config.py`), `GUI_HOST`/`GUI_PORT`.

---

## 📦 Dependencies

| Package | Purpose |
|---------|---------|
| `flask`, `flask-socketio`, `simple-websocket` | Web dashboard + real-time updates (threading mode; `eventlet` optional) |
| `frida-tools` | Runtime hook injection |
| `adb-shell` | ADB communication |
| `click` | CLI framework |
| `pyyaml` | Config parsing |
| `colorama` | Colored terminal output |
| `pysocks` | SOCKS5 proxy support |
| `requests` | HTTP (frida-server download) |

---

## ⚠️ Known Limitations

- **Emulator constraints** — x86_64 system images need hardware acceleration; ARM images are dramatically slower on x86 hosts. Cross-platform SDK detection is automatic, but only Linux/macOS are exercised by the bundled tests (Windows paths are handled via `cmd /c` for `.bat` tools).
- **Hardware attestation** — `safetynet_bypass.js` forges the JWS result and passes basic software checks, but strict key-backed Play Integrity attestation cannot be truly defeated.
- Frida hooks target the Java API and libc; apps using native obfuscation or custom detection may still fingerprint the device.

---

## 📁 Project Structure

```
android-emulator/
├── main.py                  # Click CLI entry
├── bootstrap.py             # one-shot setup: deps + SDK selection + launch
├── START.bat / start.sh     # double-click launchers (Windows / Linux-macOS)
├── config.py                # SDK paths (saved setting/env/auto), versions, defaults
├── apply_antidetect.py      # Standalone spoofing for running emulators
├── update_devices.py        # Re-apply props to existing AVDs
├── core/                    # device_manager, hook_manager
├── spoof/                   # system, hardware, gpu, sensor, battery spoofers
├── profiles/                # identity + 50+ real device profiles
├── frida_toolkit/           # FridaHooks, server manager, JS scripts (NOT importable as "frida" - that is the pip package)
├── anonymity/               # ProxyManager
├── gui/                     # Flask + SocketIO dashboard, desktop window, native dialogs
├── core/sdk_setup.py        # installs platform-tools/emulator/system images
└── tests/                   # launch-path tests that run against a fake SDK
```

### Tests (no Android SDK required)
```bash
python tests/integration_launch_test.py   # create/launch/stop/port allocation/props
python tests/cli_gui_smoke_test.py         # CLI + GUI endpoints incl. async launch
python tests/settings_desktop_test.py      # SDK selection/persistence, settings API, desktop mode
python tests/console_spoof_test.py         # sensor/battery console auth + syntax
python tests/frida_serial_test.py          # serial binding, Frida status/install, spoofing API
python tests/camera_test.py                # webcam0 selection, launch flags, editing, validation
python tests/independent_camera_test.py    # independent front/back sources + per-camera sensor identity
python tests/gpu_spoof_test.py             # coherent GPU identity, GL/EGL hooks, spoof report, doctor
python tests/webgl_gpu_leak_test.py        # no host-GPU passthrough; WebGL/GPU-process hooking
python tests/round19_test.py              # per-model cameras, guest GPU, auto-recovery
python tests/model_selection_test.py        # selected model == spoofed model; codes, conflicts, release
python tests/settings_camera_test.py       # Settings camera defaults endpoint, create fallback, pid probe
python tests/auto_root_test.py             # automatic adb root after boot, opt-outs, settings toggle
python tests/image_install_test.py         # strict system-image detection + auto-install for any version
python tests/antidetect_static_test.py     # adb-safety + qemu-pipe regression guards
python tests/restore_after_restart_test.py # spoofing/hooks survive reboot + app restart
python tests/stop_then_launch_test.py      # stop -> launch lifecycle, lingering emulators, force stop
python tests/doctor_test.py                # health checks flag planted problems with fix hints
python tests/custom_image_test.py          # custom image detection/import + device creation
python tests/spoof_verify_test.py          # ro.* refusal -> local.prop staging -> verified props
python tests/integration_launch_test.py    # create/launch/stop, image variants, rootable flags
python tests/adb_recovery_test.py          # offline device healing, adb root, repair endpoint
```
Both build a stub SDK (`tests/fake_sdk.py`) with fake `emulator`/`adb`/`avdmanager`, so the real launch code path is exercised without downloading anything.

### 🚀 "I see Launch, but clicking it does nothing"
The button was reachable and the request answered `success`, yet no emulator
started — in three different ways, all fixed:

- **A launch that outlived its emulator.** Pressing Stop while a device was
  still booting used to leave the background launch chain running until its
  boot timeout. Every later Launch got `{"success": true, "message": "Launch
  already in progress"}` — a successful-looking no-op. Stop now **cancels** the
  chain (epoch token): it aborts at its next checkpoint, kills the half-started
  emulator, and resets the state to idle. A chain state with no live chain
  behind it is never trusted, so a stale `booting` cannot refuse a launch.
- **A silent button.** Busy card buttons used the `disabled` attribute, which
  swallows clicks without a word. They are now `aria-disabled`: the click still
  reaches the handler, which explains itself ("`<device>` is still starting
  — wait for it to finish booting"). A failed launch turns the button into
  **↻ Retry launch** in one click.
- **A click lost to a re-render.** The grid refreshes every 10 s (and after
  every action); a click whose mousedown hit the old button and mouseup the new
  one used to be dropped. The handler now replays it from where the press
  started, and the Launching state (`⏳ Launching…`) stays visible on the card.

Regression test: `python tests/launch_click_test.py` (stop-during-boot,
click-after-stop, stale chain state, plus JS/CSS wiring guards).

### ⏹ "Stop says it stopped, but the emulator is still there"
Stop now proves the device is gone instead of trusting one kill:

- **Nothing is declared stopped until it is**: console silent, ports free, the
  process gone and no stray `emulator`/`qemu` process left for that AVD. Stop
  sweeps (recorded pid → the process that owns the console/adb ports → strays),
  kills each session leader so helper children go too, and escalates SIGTERM →
  SIGKILL.
- **Port probing is honest**: a wedged emulator with a full accept backlog is
  still detected as *running* (the old `connect()` probe said "gone"), while a
  harmless `TIME_WAIT` entry no longer makes a stopped device look busy.
- **A recycled pid is never killed**: only a process this app launched, an
  emulator/qemu executable, or the process whose console answers with the
  device's AVD is signalled. A foreign program on the device's ports (Android
  Studio, a second dashboard) is *reported* — Stop answers 409 naming the pid
  and its command line, and the card shows **⚠ Stop didn't finish** with
  **↻ Retry stop** until it succeeds.
- Stale AVD locks left by a hard-killed VM are removed, so the retry can launch.

Regression test: `python tests/stop_really_stops_test.py`.
