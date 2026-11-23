# Fix log

Chronological record of every reported problem and what was changed for it.
**Newest rounds are at the bottom** — read those first; older entries describe
the code as it was then, and a few behaviours were later removed on purpose:

| Removed / superseded | Where the change is recorded |
|---|---|
| automatic airplane-mode gate (*"hold the radios off until the spoof verifies"*) | Round 24 — airplane mode is now **manual only** |
| the `first_device` starter device the launcher invented | Round 20 — the app **never creates a device** |
| one-line "Booting AVD → …" launch banner | Round 21/22 — replaced by the **step-by-step boot panel** |
| `connect()`-based port probing (false "port in use") | Round 23 — listener table + leftover-emulator cleanup |
| delete removing the AVD from one guessed folder | Round 25 — every AVD home, and failures are reported |

Everything below is kept as written at the time so the reasoning is traceable.

---

## Merge notes: best of both branches + camera settings

This branch merges `arena/3cd70718-redesignedpancakefridaaaa` (the advanced
line: START.bat launcher, SDK picker/installer, desktop mode, adb recovery,
image variants, per-device cameras, test suite) with the fixes from
`arena/11908164-redesignedpancakefridaaaa`. The merge takes the advanced tree
as the base and ports over everything it was still missing.

## What was wrong (your screenshot)

> No running emulator matches 'first_device' … adb reports: no devices
> connected (adb devices is empty).

The advanced branch already fixed most of the chain (real serial resolution,
boot waits, launch logging, adb repair). This merge adds the remaining fixes:

### 1. 🔥 `_pid_alive` no longer kills the emulator on Windows
`_pid_alive()` probed with `os.kill(pid, 0)`. On Windows `os.kill` calls
`TerminateProcess()` for any signal except CTRL_C/CTRL_BREAK — so the probe
itself **terminates the emulator** (the dashboard says "Running" while adb
correctly reports an empty device list). A stale pid could even belong to an
unrelated process, which would be killed too.
→ `_pid_alive` now uses `tasklist /FI "PID eq …"` on Windows and signal 0
only on POSIX. Regression test: `tests/settings_camera_test.py` starts a
helper process and proves it survives the probe.

### 2. Frida injection targets the resolved serial, not "any USB device"
`HookManager._run_script` used `frida.get_usb_device()` / CLI `frida -U`,
which hooks whatever the first adb device happens to be.
→ The Python API opens `frida.get_device(<serial>)` (e.g. `emulator-5556`)
and the CLI gets `-D <serial>`; only an unknown serial falls back to `-U`.
CLI injection now also fails fast when `frida` exits immediately instead of
reporting success for a dead process.

### 3. Windows-safe termination
`stop_session` used `os.kill(pid, 9)` → replaced by `taskkill /F` on Windows
/ SIGKILL on POSIX via `_terminate_pid`.

### 4. Error toasts stay readable + click-to-dismiss (12 s for errors).

## New: choosing a camera in ⚙️ Settings (webcam0 included)

The project already had per-device cameras (New Device dialog, 📋 Details,
`set-camera` CLI). Now there is a **global default camera picker in Settings**:

- **⚙️ Settings → 📷 Default cameras (used for new devices)** — front/back
  fields with `emulated`, `virtualscene`, `webcam0`, `webcam1`, …, `none`,
  plus a **"Use webcam0 for both"** shortcut and **Save defaults**.
- Saved to `data/settings.json` via the new `POST /api/settings/cameras`
  endpoint (server-side validated with `validate_camera_mode`).
- The **New Device** dialog is pre-filled with these defaults; empty fields
  on the server also fall back to them (explicit form values win).
- Existing devices can still be switched to `webcam0` in their 📋 Details
  (writes `hw.camera.front/back` to the AVD `config.ini`; restart applies).

## Tests

All existing suites pass unchanged, plus the new
`tests/settings_camera_test.py` (16 checks):

```
python tests/integration_launch_test.py   # launch path, ports, props
python tests/cli_gui_smoke_test.py        # CLI + GUI endpoints
python tests/settings_desktop_test.py     # SDK settings, desktop mode
python tests/console_spoof_test.py        # sensor/battery console
python tests/frida_serial_test.py         # serial binding, frida install
python tests/camera_test.py               # per-device webcam0 selection
python tests/settings_camera_test.py      # NEW: settings defaults + pid probe
python tests/adb_recovery_test.py         # offline-device healing
```

Run with: `python main.py gui` (or `START.bat` on Windows), then
⚙️ Settings → 📷 Default cameras → "Use webcam0 for both" → Save defaults.

---

## Round 3: "Running" but `adb devices` stays empty — for good

You re-tested and the dashboard still said **Running** while Spoof/Hooks got
*adb devices is empty*. Two further root causes, both fixed:

### 3a. Stale/reused pids faked "Running" forever
`_is_running` trusted a persisted pid blindly. After an emulator crash,
Windows quickly **reuses that pid for an unrelated process** — the dashboard
then showed "Running" for something that wasn't an emulator at all, while adb
(correctly) saw nothing.

→ A pid now only counts as evidence when the **emulator console port confirms
it** (`avd name` over TCP 127.0.0.1:5554/5556/…) or during a short grace
window right after launch. Stale pids are detected and cleared from
`data/devices/<name>.json`, so the card flips back to Stopped and Launch
becomes available again. New module: `core/emulator_console.py` (banner +
auth-token handling).

### 3b. A live emulator adb can't see is now self-healed
adb registration genuinely breaks (stale adb server, version clash with a
second platform-tools copy, killed server). Previously every Spoof/Hooks
click just failed. Now `get_device_serial()`:

1. probes the recorded console port (or scans 5554–5568 when the pid is
   alive) — an emulator console answers **even when adb is blind**;
2. when the console proves the device is alive, **restarts the local adb
   server** (rate-limited, so refresh loops can't spam it) — running
   emulators re-register automatically;
3. re-matches the serial (`emulator-<console port>` or by AVD name) and
   persists it.

The launch-time `wait_for_boot` does the same once mid-boot instead of
burning the whole boot timeout on an adb-blind emulator.

### 3c. Errors that actually say what's wrong
Spoof/Hooks failures now include a **diagnostic snapshot**: whether the saved
process is alive, whether any emulator console answers (and which AVDs), what
adb lists, and the last lines of the emulator log (`data/logs/<name>.log`).
Plus a new `GET /api/devices/<name>/diagnose` endpoint with the full picture.

### New probe/heal tests
`tests/console_heal_test.py` (14 checks) simulates the emulator console over
a real socket: AVD probing with/without auth token, stale-pid rejection,
console-confirmed running state, and the full
console-alive + adb-blind → restart → re-register → serial healed flow.

All 9 test suites pass:

```
python tests/console_heal_test.py      # NEW: console probe + adb self-heal
python tests/settings_camera_test.py   # settings camera defaults, pid probe
python tests/camera_test.py            # per-device cameras incl. webcam0
python tests/frida_serial_test.py      # serial binding, frida install/status
python tests/integration_launch_test.py
python tests/cli_gui_smoke_test.py
python tests/settings_desktop_test.py
python tests/console_spoof_test.py
python tests/adb_recovery_test.py
```

**If the emulator still doesn't appear after this update**, the new error
message tells you why. The most common remaining cause: the emulator window
is stuck before boot (hardware acceleration missing) — open
`data/logs/first_device.log` (path is in the error), and enable
VT-x/AMD-V + "Windows Hypervisor Platform", or start the virtual machine
from Android Studio once to validate the image.

---

## Round 4: automatic rooting ("root would automatically install")

Devices now **root themselves right after boot** — no manual `adb root` and
no hunting for the right settings:

- `DeviceManager.launch_device` runs `adb root` (verified via `shell id`,
  adbd-restart aware) once boot is confirmed, *before* spoofing/Frida, and
  persists the real outcome (`rooted` / `rooted_at` / `root_error`) so the
  card can show it: **✓ Root** active · **✓ Root capable** ·
  **⚠ Root failed** (hover shows why) · **⚠ No root** (Play Store image).
- **New Device dialog:** "Root automatically after launch" checkbox
  (on by default; auto-disabled/unchecked when the Play Store image is
  selected, since those refuse root).
- **⚙️ Settings → 🔓 Rooting:** global default toggle
  (`POST /api/settings/auto-root`, persisted in `data/settings.json`);
  the dialog checkbox is prefilled from it.
- Per-device override wins over the saved default; CLI: `create --no-root`.
- Root state is cleared on stop/launch because root never survives a reboot.
- `adb.root()` no longer burns its whole 15s verify window when adbd answers
  with an empty shell (fails fast after 3 empty probes).
- New `tests/auto_root_test.py` (15 checks): default-on rooting, stop
  clearing, per-device opt-out, Play-Store honesty, settings round-trip
  and GUI create fallback.

All 10 test suites pass.

### Round 4 follow-up: spoofing killed adb ("device does not set as rooted")

Root cause: `spoof/system_spoof.py` ended with
`settings put global adb_enabled 0` to hide USB debugging from apps.
On an emulator adb is our **only** management channel, and the Mainline
AdbService observes that setting — adbd stopped right after "Settings
applied", the device went `offline` for good, and no
kill-server/reconnect could revive it (the setting even persists across
reboots). Every later Frida hook attempt therefore failed with
"Device not ready", so nothing after rooting ever worked.

Fixes:

- The `adb_enabled=0` write is **removed** (with a comment explaining why).
- The anti-detection goal is preserved *without* breaking the channel:
  `frida_toolkit/scripts/anti_detect.js` now hooks `Settings$Global.getInt` so
  in-app probes of `adb_enabled` and `development_settings_enabled`
  read back **0**, while the real adb stays up.
- Regression guard in `tests/auto_root_test.py`: the spoof flow must
  never issue `settings put ... adb_enabled`.
- Note: **Root Checker-style apps always report "not rooted"** on these
  devices — `adb root` gives the adb shell and Frida root, but apps only
  see root when an `su` binary (Magisk/SuperSU) is installed, which is
  out of scope (and usually undesirable) for an anti-detect emulator.
  The correct check is `adb shell id` → `uid=0(root)` (the dashboard tag
  "✓ Root" means exactly this).

If a device was already bricked this way (`offline` forever after spoof):
delete it in the dashboard and re-create it — that wipes the userdata
image where `adb_enabled=0` was persisted.

---

## Round 5: Frida hooks never injected (but logs said "protections applied")

User report: "it says that root applied but I don't see it" - dashboard
toast: **Failed to apply hooks**, console showed:

```
[HookManager] Python Frida API failed (module 'frida' has no attribute 'get_usb_device')
[HookManager] Frida CLI exited with code 2: unrecognized arguments: --no-pause
```

Root causes, all fixed:

1. **Package shadowing.** The repo had a top-level package literally named
   `frida/`, so every `import frida` inside the app resolved to OUR package
   instead of the pip Frida client - hence "no attribute
   'get_usb_device'". It also poisoned version detection, so the pinned
   frida-server (16.1.4) did not match the installed client (17.x).
   The directory is now **`frida_toolkit/`**; the real Frida library
   imports cleanly (verified: `frida 17.26.3` inside the test venv).
2. **Dead CLI flag.** frida-tools 13+ removed `--no-pause` (spawned apps
   resume by default). We now probe `frida --help` once and only pass the
   flag when the installed CLI advertises it.
3. **Stale frida-server kept forever.** `data/frida-server.version` marks
   the downloaded server build; a client/server version mismatch now
   re-downloads automatically, and an old *running* server on the device
   is killed and restarted with the matching build.
4. **Silent partial failure.** `inject_system_wide` / `_run_script` now
   record `last_error` and `apply_all_protections` returns failure when
   apps could not be hooked, so the UI toast tells the truth instead of
   "All protections applied!" while nothing was injected.

Root itself was never the problem here - `adb root` is confirmed working
(the spoof + frida-server both ran as uid=0). "Root Checker Basic" inside
the emulator keeps saying *not rooted* **by design**: apps need an `su`
binary (Magisk/SuperSU) to get root, which is deliberately not installed
- for anti-detect, "rooted via adb only, invisible to apps" is usually
exactly what you want. Root verification: `adb shell id` = `uid=0(root)`
≡ the dashboard's ✓ Root tag.

---

## Round 6: full sweep ("fix all bugs, make all fully work")

- **Android 12 / other-version creation failing** ("Package path is not
  valid; valid system image paths are…"): `is_system_image_installed`
  counted an *empty directory* (sdkmanager cancel/crash remnant) as an
  installed image, so the auto-install never ran and avdmanager choked.
  Now an image only counts as installed when a non-empty `system.img`
  exists; `create` auto-installs missing images for ANY supported Android
  version before avdmanager runs (regression test
  `tests/image_install_test.py`, fake sdkmanager now materialises images).
- **Concurrent Spoof/Hooks while the launch chain is up** (the double
  spoofing runs + double frida-server pushes in the logs): apply endpoints
  now return a clear 409 when the launch chain (`starting`/`booting`) or a
  background hook run is still in flight; per-device hook in-flight
  registry dedupes the chain itself.
- **Status lies**: `/api/status/adb` only counts devices in `device`
  state (an 'offline' entry no longer lights the card green); the device
  list exposes `launch_state`, the card shows `Starting…` (action buttons
  hidden except Stop) until root+spoof+hooks finish; the
  "Anti-Detect Active" counter now counts devices whose hook chain really
  completed (`hooks_applied` persisted, cleared on stop/relaunch) instead
  of "all devices".
- **Root turbulence during spoofing**: SystemSpoofer delegates to the
  verified `core.adb.root` (no-op when already rooted — no extra adbd
  restart; kills the old fixed 2s race); HardwareSpoofer no longer re-runs
  `adb root` before every root command (~8 adbd restarts per pass → 1).
- Root state surfaced in the Details dialog (live status + explanation why
  Root-Checker apps report "not rooted"); CLI `list` shows a Root line.
- Bare `except:` removed in proxy manager.

All 11 test suites pass.

---

## Round 7: polish pass ("make it perfect")

- **Cameras/sensors/GPS protected from the emulator-file hiding**: the
  runtime rename of `/dev/qemu_pipe` and `/dev/socket/qemud` could kill
  the camera HAL (virtual scene / webcam pass-through) and sensor/GPS
  pipes on first use after spoofing. Those nodes stay; apps' *presence*
  checks for them were already spoofed by the `File.exists()` hook in
  `frida_toolkit/scripts/anti_detect.js` (regression-guarded by the new
  `tests/antidetect_static_test.py`).
- **frida_inject.log no longer grows forever**: each injection opens it
  fresh ("wb"), so the tail printed on failures is actually the current
  error, and disk doesn't fill across sessions.
- **No more `/favicon.ico` 404 spam** in the console (inline SVG icon).
- All three Frida scripts (`anti_detect.js`, `native_hooks.js`,
  `safetynet_bypass.js`) are syntax-checked in CI (`node --check`).
- Static regression suite added: `tests/antidetect_static_test.py` pins
  the adb-safety and qemu-pipe rules above.

All 12 test suites pass.

---

## Round 8: "no root, no spoofing, nothing applied" → always visible why

Symptom: launch failed, card silently reverted to *Stopped*; since the
auto chain only runs after a confirmed boot, nothing was rooted or
spoofed, and the only hint was a toast that vanishes in seconds.

Fixes:

- **Zombie-emulator port collisions**: the console-port allocator now
  TCP-probes both the console and adb port — a crashed emulator that adb
  no longer lists can no longer steal the port (the new instance used to
  exit immediately with "port in use").
- **Launch failures persist**: the last launch error is saved on the
  device (`launch_error`) and shown as a red `⚠ Launch failed` chip on
  the card until the next launch clears it.
- **Details dialog shows the evidence**: "Last launch error" block +
  live emulator log tail, so the exact cause (GPU, accel, port, image)
  is one click away instead of buried in `data/logs/*.log`.
- **No premature "is running" toast**: launch polling waits for the raw
  chain state, not just the adb-visible emulator.
- Boot timeout 180s → **240s** (env override `EMULATOR_BOOT_TIMEOUT`) for
  slow first boots / SDKs on external drives.

If a launch still fails, open 📋 Details — it now tells you exactly why.

---

## Round 9: Doctor — self-diagnosing health checks

Every real-machine failure this project has hit maps onto something
checkable in advance, so there's now a single report that hunts all of
them: `python main.py doctor` (CLI, non-zero exit on errors) and
**⚙️ Settings → 🩺 Health check → Run doctor** in the dashboard
(`GET /api/doctor`, `core/doctor.py`).

Checks: Python ≥ 3.9 · SDK layout (emulator/adb/avdmanager/sdkmanager) ·
duplicate cmdline-tools copies (the `latest`+`latest-2` confusion seen in
real logs) · platform-tools age (below 33 is flagged - that lineage broke
adb against Android 13+ images here) · adb server responsiveness ·
installed vs missing system images (strict: non-empty system.img) ·
settings.json integrity · Frida client/server version alignment · hardware
acceleration probe (`-accel-check`) · per-device consistency (AVD ini
exists, console port not stolen by a zombie/other AVD, stale
pid-without-adb state).

Every issue prints a concrete fix hint; the launch-failure toast now
points straight at Details + doctor. Covered by tests/doctor_test.py
(planted problems must surface; restoring state must clear them).

---

## Round 10: spoofing is now REAL (verified on-device) + custom images

**The screenshot told the truth:** Details said *rooted ✓* (and it is —
`adb shell id` → uid=0) but Settings still showed
`sdk_gphone64_x86_64` / `EMULATOR37X2X12X0`. Cause: the spoofer ran
`setprop ro.product.model X`, which Android refuses — `ro.*` props are
read-only after init. The fake adb used in tests "accepted" it, so the
pipeline reported success while changing nothing.

**Fix (no Magisk needed — adb-root is real, uid=0 verified):**
1. Runtime `setprop` for everything (works for non-ro keys),
2. read back — every mismatch is staged,
3. stage A: merge into **`/data/local.prop`** + reboot (works on some
   userdebug images; on Android 14 userdebug it demonstrably does NOT
   override ro.* — proven on-device with the verify read-back),
4. stage B (when A leaves any mismatch): **`adb disable-verity` → reboot →
   `remount`/`remount,rw` → rewrite the build.prop files** that exist
   (`/system`, `/product`, `/system_ext`, `/vendor`, `/vendor/default.prop`,
   `/odm/etc/build.prop`) with originals backed up to
   `/data/local/antidetect-buildprop-backup/` → final reboot → verify.
   Note this edits the shared system image, so the last-spoofed identity
   wins for every AVD on that image (documented; userspace-only use).
5. Anything still mismatched is reported **honestly** (props that only the
   boot cmdline can set on a given image — Frida hooks still cover
   `Build.*` for hooked apps).

The prop set grew too: Model, device name (Settings device_name +
bluetooth_name), serial AND **build number** (`ro.build.id`,
`ro.build.display.id`, `ro.build.version.incremental`,
`ro.build.version.release`, `ro.build.type=...user`, `ro.build.tags`,
`ro.build.flavor`, `ro.build.description`, `ro.product.name`) are all
parsed from the profile fingerprint — so "userdebug … dev-keys" stops
leaking, and Settings → About shows a real retail build number.

**Visibility:** the report is saved on the device — Details now shows a
✓/✗ line per prop ("Spoof verify report"), and a `⚠ Spoof incomplete`
chip appears on the card when anything could not be changed. State is
cleared on stop/relaunch like root/hook state.

**Tests:** fake SDK now *models the real device* — ro.* setprop is
refused, props are seeded with sdk_gphone defaults at boot, and
`/data/local.prop` push behaves like init's override loader.
`tests/spoof_verify_test.py` reproduces the user's symptom, then proves
the staged fix makes `getprop` actually return the identity values.

**Custom system images:** every image physically present in the SDK's
`system-images/` directory (downloaded via Android Studio, extracted
archives, modified images, Android-desktop builds…) is auto-listed in the
New Device dialog as "Custom: API xx · tag · abi", plus a **📂 Import
image from folder…** flow (native folder picker or pasted path) that
copies a folder containing `system.img` into the SDK as a proper
`system-images;android-<api>;<tag>;x86_64` package and selects it. The
Android version is derived from the package so it can't disagree. Empty
`system.img` shells (partial downloads) are never offered. Endpoints:
`GET /api/system-images`, `POST /api/system-images/import`,
`POST /api/system-images/pick-folder`. Covered by
`tests/custom_image_test.py`.

---

## Round 11: spoofing/hooks were never saved across a restart (the reported bug)

> "We have some problems with this app when I apply spoofing and hooks it
> don't save after restart."

**Why it happened.** Nothing about the protection chain lives on disk in the
guest: the spoofed `ro.*` values are written straight into the running kernel's
`/dev/__properties__` by `resetprop-rs`, and `frida-server` + its sessions are
just processes. So:

1. **An emulator reboot** (Android's power menu, `adb reboot`, a Stop/Launch,
   or the zygote-reload path) wiped every spoofed prop and killed
   frida-server — nothing re-applied them, because the app had only ever
   stored the *result* (`spoof_report` / `hooks_applied`), never the
   *intent* ("this device must stay spoofed/hooked").
2. **Restarting the app** lost the in-memory chain (sessions, simulator
   threads) and had no record to rebuild it from.
3. The saved `spoof_report` / `hooks_applied` flags kept claiming the device
   was protected even after the reboot, so the dashboard never even offered a
   re-apply — it looked "saved", it just wasn't active.

**Fix: persisted intent + boot-id tracking + automatic restore.**

- New per-device fields in `data/devices/<name>.json`:
  - `auto_spoof` / `auto_hooks` — *"keep this applied"* (set when you use
    Spoof/Hooks, and on launch; an explicit `false` is a permanent opt-out),
  - `spoof_applied` / `hooks_applied` (+ `*_at` timestamps) — *really live on
    this boot*,
  - `runtime_boot_id` — the `/proc/sys/kernel/random/boot_id` the live state
    belongs to.
- **Honest staleness.** `DeviceManager._runtime_status()` compares the live
  boot id with the recorded one; a mismatch (reboot, emulator restart) marks
  the device `runtime_stale` instead of advertising protection that is gone.
  Devices that were never protected are never nagged.
- **Restore engine.** `restore_device()` / `restore_all_pending()` re-run the
  exact same verified pipeline (`adb root` → `HardwareSpoofer` +
  `SystemSpoofer` → `HookManager.apply_all_protections` + battery/sensor
  simulators), then re-pin the boot id.
- **Automatic everywhere it matters:**
  - the dashboard restores every stale device in a background thread as soon
    as it is opened (an app restart no longer forgets anything),
  - `POST /api/devices/<name>/restore` and the new **↻ Restore** card button,
  - `python main.py restore [name]` (no name / `--all` sweeps everything),
  - launching again re-applies automatically (that always worked; the intent
    now survives Stop, so `auto_spoof`/`auto_hooks` are honoured).
- **Visible in the UI:** a `⚠ Needs restore` chip + the *"Spoofing/hooks lost
  on restart"* alert in Details, the pipeline cells say *"Lost on restart"*
  instead of a stale `✓`, and Details has **Keep Applied After Restart**
  checkboxes (`POST /api/devices/<name>/restore-intent`) plus an *Apply now*
  button.
- **Doctor** now flags `spoofing/hooks were lost on reboot` per device with
  the one-click fix.
- The dashboard's 10 s poll reads the boot id through a 15 s cache, so the
  extra `adb shell` stays at ~1 call per device per poll.

`Stop` clears the *live* flags but deliberately keeps the intent; `Delete`
removes everything as before.

Covered by the new `tests/restore_after_restart_test.py` (56 checks): intent
persistence, stale detection after a guest reboot, verified re-apply of the
`ro.*` props, stop/launch behaviour, the sweep, opt-out, hook restore, all
GUI endpoints and the CLI. All 16 suites pass.

---

## Round 12: "after I stop the device I cannot launch it again"

The card showed **Stopped** with a Launch button, but clicking it did nothing
(only a toast). Two real-machine races were responsible — the fake SDK used by
the tests hides both, because it drops a killed device instantly and its
"emulator" is a single process:

1. **Real adb is lazy.** `adb emu kill` returns before adbd deregisters, so for
   a few seconds `adb devices` still lists the dead emulator. `launch_device()`
   then saw `_is_running() == True` and raised **"Device already running"**,
   which the dashboard only surfaced as a toast that vanished — the button
   looked grey/dead.
2. **The VM is a child process.** On Windows `emulator.exe` spawns
   `qemu-system-x86_64.exe` as a *separate* process. `stop_device()` killed
   only the recorded pid, so the VM survived: it kept the console/adb ports
   bound and the AVD files locked, and every later launch died with
   "port in use" / "another emulator instance is running".

**Fixes**

- **Stop kills the whole process tree** (`taskkill /T /F` on Windows;
  session-guarded `killpg` + `pkill -P` on POSIX — with a hard guard so it can
  never signal the app's own process group).
- **Stop verifies it really stopped** (`_wait_until_stopped`): console silent,
  ports free, pid gone, adb dropped the serial. If an emulator refuses to die,
  stop now says so (`Could not stop '<name>' … / main.py doctor`) instead of
  reporting success and leaving the user stuck.
- **adb is told to drop the dead serial** (`adb -s <serial> reconnect offline`)
  so the next Launch isn't rejected.
- **Launch self-heals instead of refusing** (`_launch_blocker`): a live
  answering console means "really running"; a stale adb entry or recycled pid
  (no console, nothing tracked) is cleared and the launch proceeds. A tracked
  child process is still trusted for the first 90 s of a boot, and a process
  that is alive long after launch but never opened a console is stopped first.
- **Stale AVD locks removed before launch** (`_clear_stale_avd_locks`): a hard
  kill leaves `hardware-qemu.ini.lock` in the `.avd` folder, which makes the
  emulator refuse to start ("another emulator instance is running").
- **Dashboard**: a rejected Launch now shows the reason (and refreshes the
  card), and a failed Stop offers **Force stop** — `POST
  /api/devices/<name>/stop {"force": true}`, also `python main.py stop <name>
  --force`, which kills the tree and frees this device's port pair only.
- **Doctor** flags a port that is busy with no console answering (the zombie
  case) and names the fix.

Note on the previous `#11` change: the first version of `_launch_blocker`
trusted "adb listed it, no console" as a ghost, which cleared the bookkeeping
of a *just-launched* emulator whose console had not opened yet (caught by the
`cli_gui_smoke_test` "relaunching running device reports error" check). It now
only treats a device as a ghost when no process we know about is alive (or it
is alive but way past its boot window), so a fresh launch is always protected.

Covered by the new `tests/stop_then_launch_test.py` (22 checks): stop/launch
round-trips, the lazy-adb ghost, a surviving `qemu` child holding a console
port, stale lock cleanup, launch-time ghost clearing, the force-stop endpoint
and static guards. All 17 suites pass.

---

# Round 13: front and back are now two independent cameras

**Ask:** "add a feature so the front camera and back camera can be independent
cameras and look like independent ones".

Two separate problems were hiding behind that sentence.

### 1. The *sources* could already differ, but nothing helped you do it
`create`/`set-camera`/Details already accepted a per-facing source, yet the UI
offered a single prominent shortcut — **"Use webcam0 for both"** — which is
the opposite of independent: both facings then show the *same* host webcam.
Now:

- **New presets** in the New Device dialog, ⚙️ Settings defaults and 📋 Details:
  - **Two different webcams** → back `webcam0`, front `webcam1` (two
    genuinely different real images);
  - **Two virtual sources** → back `virtualscene`, front `emulated`.
  "Use webcam0 for both" is still there (it's legitimate when you only have
  one webcam) but it now says plainly that both cameras will show the same
  images.
- **`usb0`, `usb1`, …** external-USB sources are accepted alongside
  `webcamN`, and `/api/cameras` lists them plus the host webcams this machine
  actually has (`webcams`, `webcam_error`), so the dashboard stops guessing.
- **Shared-source warning:** `camera_role_conflicts()` detects a pair that
  makes apps see one camera (`webcam0`+`webcam0`, `emulated`+`emulated`, …)
  and it is returned by `POST /api/devices/<n>/camera`, by the create
  endpoint (`camera_warning`), by the `create`/`set-camera` CLI output, and
  shown as a toast. It is a hint, never a hard error — you asked for the
  ability, not a restriction.

### 2. To apps, both cameras looked like the *same emulator camera*
An AVD reports identical static metadata for both facings, so
`CameraManager.getCameraCharacteristics()` returned the same sensor for
`"0"` and `"1"` — the classic "one virtual camera, two ids" tell.

- **New generated identity (`profiles/profile_generator.generate_camera_specs`)**:
  each device gets two internally consistent camera modules — distinct sensor
  model (`SONY IMX890` vs `SAMSUNG S5KGD2`, …), distinct serial, resolution
  (e.g. 4096×3072 back / 3280×2464 front), pixel size, focal length (wide 6.7 mm
  vs selfie 3.3 mm), aperture, orientation (90° vs 270°), frame rate, pose and
  build date. Stored as `identity["camera_specs"]` and mirrored at
  `device["camera_specs"]`.
- **New Frida script `frida_toolkit/scripts/camera_hooks.js`** (loaded with
  every combined anti-detect script, so it is active wherever hooks are):
  - **Camera2** — patches `CameraManager.getCameraCharacteristics(id)` per
    facing: `LENS_FACING`, `SENSOR_ORIENTATION`, `SENSOR_INFO_PHYSICAL_SIZE`,
    `SENSOR_INFO_PIXEL_ARRAY_SIZE`, `SENSOR_INFO_SERIAL_NUMBER`,
    `LENS_INFO_AVAILABLE_FOCAL_LENGTHS`/`_APERTURES`, `LENS_POSE_*`,
    `INFO_SUPPORTED_HARDWARE_LEVEL` and a per-facing `INFO_DEVICE_ID`.
  - **Camera1** — `getNumberOfCameras()` ≥ 2, per-index `CameraInfo`
    facing/orientation, and per-facing `Parameters` picture size + a
    `camera-id`/`sensor-model` parameter.
  - **EXIF** — `Make`/`Model` of captured stills come from the identity, not
    the emulator.
  - Same invariants as the rest of the toolkit: installed via
    `setImmediate`, every hook in `try/catch`, nothing throws into the app.
- **Devices created before this round** get specs generated the first time
  their cameras are changed (`set_device_cameras` backfills
  `camera_specs` + `identity.camera_specs`), so old AVDs stop looking
  identical too — no re-create needed.

### UI
📋 Details now lists both sensors as apps will see them (model, resolution,
focal/aperture, orientation, serial) above the per-facing source inputs, so you
can verify at a glance that they are not the same camera. `/api/device/<n>/identity`
returns `camera_specs` and the current `camera_sources`.

**Honest limitation:** independent *sources* need two different inputs — two
host webcams (`webcam0`+`webcam1`) or a USB camera. With a single webcam you
can only reuse it on both facings; the identity/characteristics spoofing still
makes them look independent, but the picture is necessarily the same.

Covered by the new `tests/independent_camera_test.py` (63 checks: validation,
spec independence and stability, per-facing persistence through
config.ini/JSON/launch flags, legacy backfill, the Frida payload contents, and
the GUI/CLI wiring). All 19 suites pass.

---

# Round 14: GPU spoofing is now part of the spoofing pass

**Ask:** "add gpu spoof into spoofing".

There was a `spoof/gpu_spoof.py`, but it was effectively dead code: `spoof_all()`
never called it, it hard-coded three partial extension lists, its GL version was
a random number (`V@0<600-800>.0`, which no driver ever printed) and it did not
match the renderer — and `anti_detect.js` only spoofed `GLES20.glGetString`
partially, i.e. the renderer/vendor pair was there but the extension list,
driver version, GL limits and the **native** `glGetString` (the one Unity,
Unreal and Flutter actually call) still leaked the emulator.

### What is spoofed now
One GPU model per device (`profiles/gpu_profiles.py`: Adreno 660/730/740/750,
Mali-G68/G610/G710/G715/G720, Xclipse 920/940 — each with vendor, EGL vendor,
real driver-version prefix, vendor-specific shader extensions and numeric
limits). Every answer is **derived from that one entry**, so nothing can
contradict anything:

- `GL_VENDOR`, `GL_RENDERER`, `GL_VERSION`, `GL_EXTENSIONS`,
  `GL_SHADING_LANGUAGE_VERSION` via `GLES20.glGetString` — always answered from
  the identity (partial spoofing is itself a tell);
- `GLES20.glGetIntegerv` for `GL_MAX_TEXTURE_SIZE`, `GL_MAX_VIEWPORT_DIMS`,
  `GL_MAX_VERTEX_ATTRIBS`, `GL_MAX_TEXTURE_IMAGE_UNITS`,
  `GL_MAX_RENDERBUFFER_SIZE`, `GL_MAX_COMBINED_TEXTURE_IMAGE_UNITS`;
- `glGetShaderPrecisionFormat` (SwiftShader's precision triple differs);
- **native** `glGetString` in `libGLESv2.so`/`libGLESv1_CM.so`/`libGLESv3.so`
  via `Interceptor.replace` + `NativeFunction`, with the spoofed strings
  allocated **once** (caching — a render loop calls this thousands of times);
- `EGL14.eglQueryString`: `EGL_VENDOR`, `EGL_VERSION`, `EGL_EXTENSIONS`
  ("Android"/"Google"/"SwiftShader" are emulator tells);
- cosmetic props `debug.hwui.renderer=skiagl`, `debug.egl.hw=1`,
  `debug.sf.hw=1`.

`HardwareSpoofer.spoof_all()` now calls `spoof_gpu()`, which stores the result in
`prop_report["gpu"]`, so **Spoof** in the dashboard, `apply_antidetect.py` and the
API all report what the device claims (`GPU: ARM Mali-G720 · EGL ARM · OpenGL ES
3.2 v1.r43p0 · 18 extensions`).

### Kept deliberately untouched
`ro.hardware.egl` / `ro.hardware.vulkan` are **never** written (the previous
version didn't either, but the test guard is now explicit): Android's loader uses
them as exact `.so` filename suffixes (`libEGL_<v>.so`, `vulkan.<v>.so`), so
"adreno"/"arm" makes RenderThread load a missing driver and crash every app that
draws. Vendor/renderer spoofing lives in the GL/EGL hooks instead — documented
in `gpu_spoof.py`, `system_spoof.py` and the README.

### Stored, stable, checkable
- The identity carries `gl_renderer/gl_vendor/gl_version/egl_vendor/
  gl_extensions/gl_limits/gpu_name/gpu_hw/gpu_score/gpu_year`; the device JSON
  mirrors them in `device["gpu"]`.
- `build_gpu_identity()` prefers the saved block, so re-applying spoofing or
  regenerating hooks never changes the GPU apps already saw.
- **Details → ↻ New identity** regenerates the GPU alongside the IMEI/serial/MAC
  (`device["gpu"]` is refreshed too), and 📋 Details shows *GPU*, *GL Version* and
  an EGL / limits / extension fact grid.
- `python main.py doctor` gained **GPU identity**: it cross-checks every saved
  device and flags a renderer/vendor contradiction (`Mali-G715` + `Qualcomm`),
  another vendor's extensions (`GL_QCOM_*` on ARM) or missing limits. The check
  is strictly read-only (it no longer perturbs `pid`/liveness state — a bug
  caught while writing it: the first version called `list_devices()`, whose
  ghost-clearing made the neighbouring "stale pid" doctor row disappear).

Covered by the new `tests/gpu_spoof_test.py` (85 checks: table coherence,
generated identities, spoofer behaviour, the driver-breaking props, the Frida
payload + hooks, source-level surfaces, device JSON plumbing and doctor
detection). All 20 suites pass.

---

# Round 15: the host GPU is no longer visible to WebGL

**Ask:** a fingerprint report showed

```
gpu:   Google (ATI Technologies Inc.)
       Android Emulator OpenGL ES Translator (AMD Radeon RX 6700 XT)
```

> "GPU should not show this"

Two independent bugs, both fixed:

### 1. GPU strings were spoofed conditionally
`anti_detect.js` only replaced `GL_RENDERER`/`GL_VENDOR`/`EGL_VENDOR` when the
real value contained `Android Emulator` / `SwiftShader` / `ANGLE` / `Google`.
With real-GPU passthrough (`-gpu host`, the default `auto`) the driver reports
the **host** stack — `Android Emulator OpenGL ES Translator (AMD Radeon RX 6700
XT)`, `Google (ATI Technologies Inc.)` — which matched none of those hints on
the vendor side and was passed straight through to the page, with "confidence:
high". Every GL/EGL string is now answered from the identity unconditionally
(partial spoofing is itself a tell), and the hook log prints what it overrode
(`… overriding: … [emulator/host leak]`) so a leak is visible instead of silent.

### 2. WebGL is read in a *different process*
`UNMASKED_VENDOR_WEBGL` / `UNMASKED_RENDERER_WEBGL` are computed inside
Chromium's **sandboxed renderer/GPU process** (`<package>:privileged_process0`).
Hooks were only installed in the app's main process, so that process kept
answering with the real driver — which is exactly the `gpu:` row in the report.
Now `HookManager.inject_hook`s also:

- **discovers** the renderer/GPU processes (`find_renderer_processes`): Frida's
  process list, falling back to `ps -A`; matched on the app's own package
  prefix **plus** a renderer suffix (`privileged_process`, `sandboxed_process`,
  `renderer`, `gpu`), capped at 8, so another app's sandbox (or a `:remote`
  service) is never touched;
- **injects a graphics-only script** (`inject_renderer_processes` →
  `generate_graphics_script` → new `frida_toolkit/scripts/gpu_hooks.js`). The
  full combined script's native layer deliberately bails out in sandboxed
  processes (system-UI crash protection), and libc/Java identity hooks are
  pointless there — WebGL only needs the GL/EGL strings;
- **re-scans** 4 s and 12 s later (`_schedule_renderer_followup`), because a
  browser creates its GPU process lazily on the first WebGL/Canvas use, and
  skips PIDs that are already hooked.

`gpu_hooks.js` covers native `glGetString`, **`glGetStringi`** (GLES3 extension
enumeration, where Chromium reads the extension list), native `eglQueryString`,
and the Java `GLES20`/`EGL14` variants; all strings are cached (render-loop hot
path) and derived from the identity, never hardcoded.

The dashboard reports the coverage: **Inject hooks** now answers
`Anti-detect hooks active in <pkg> (1 WebGL/GPU process(es) covered)` (or tells
you to re-inject once the page has rendered), and 📋 Details shows
**Hooked in: app + WebGL/GPU process** on the GPU fact grid.

Honest limits: strings computed *outside* Android (the host browser you look at
the emulator with) can't be spoofed from inside — the fix applies to
fingerprinting that runs **in** the emulated Android app, which is what the
report above shows. A browser that has not rendered anything yet has no GPU
process to hook; the follow-up scan covers it, or re-inject.

Covered by the new `tests/webgl_gpu_leak_test.py` (50 checks: unconditional
spoofing, renderer discovery/matching/capping, the graphics-only script and its
GL/EGL surfaces, and the UI reporting). All 21 suites pass.

---

# Round 16: selecting a phone model no longer spoofs a different phone

**Ask:** a fingerprint report (whatphone.github.io) showed `23116PN5BC / Xiaomi`
after selecting a **Google** model:

> "when i use a diffrent phone model from google i get smtyh like this wichj is
> not the model i sellected"

`23116PN5BC` is the **Xiaomi 14 Pro**. The report was not lying — the spoofed
identity really was the wrong phone.

### Root cause: fuzzy name matching drifted to the bigger sibling
`get_model_profile()` returned the first profile whose name *contained* the
request, and the table lists the bigger sibling first. So **9 of 45** models
silently spoofed a different phone:

| selected | actually spoofed | build identity |
|---|---|---|
| Google Pixel 8 | Google Pixel 8 **Pro** | husky |
| Google Pixel 7 / 6 | Pixel 7 **Pro** / 6 **Pro** | cheetah / raven |
| Xiaomi 14 | Xiaomi 14 **Pro** | **23116PN5BC** |
| Xiaomi 13 | Xiaomi 13 **Pro** | 2210132G |
| Samsung Galaxy S24 / S23 / S22 | **Ultra** variants | SM-S928B / 918B / 908B |
| POCO F5 | POCO F5 **Pro** | 23013PC75G |

Matching is now: exact name → exact hardware model code → all tokens present
**and no unrequested variant word** (`Pro`/`Ultra`/`Plus`/`Max`/`FE`/`5G`/…) →
unique prefix. Ambiguous or unknown selections are **refused with a clear
error** instead of silently spoofing a sibling, and every one of the 45 model
names still resolves to itself (guarded by a test).

### The label could not reveal it
The device JSON stored the **raw request** while the identity came from the
(wrong) resolved profile, so the card said "Google Pixel 8" over a Pixel 8 Pro
identity. Now `device["model"]` is the resolved profile's name and the raw
request is kept as `requested_model`; the create toast shows the resolved
device and the API returns `model` + `model_note`.

### Model codes are first-class
The dropdown shows the hardware code next to every name
(`Google Pixel 8 (Pixel 8)`) — the code is what fingerprint reports print — and
an exact code can be used instead of a name (`create -m 23116PN5BC`). A
selection that names one phone and a **different** code (`Xiaomi 14
23116PN5BC`) is rejected, naming both candidates, instead of silently picking
one.

### The identity's build now matches its own fingerprint
- `build_id` / `incremental` are taken from the profile fingerprint instead of
  being random (a random build id next to a real fingerprint is a mismatch, and
  the build number in the dashboard never matched what apps see).
- A fingerprinted release could contradict the running image: a Pixel 8 on an
  **Android 15** image advertised `Android 14.0.0`. `ro.build.version.release`
  (and the identity) now follow the image the device actually runs, while
  `ro.build.id` / fingerprint stay the profile's real build.

Reported OS/browser strings (`Android 14.0.0` next to Chrome 113) come from the
emulator image and the installed Chrome, not from the spoofed identity, so they
are unchanged — Chrome ignores UA overrides on `whatphone.github.io`.

Covered by the new `tests/model_selection_test.py` (78 checks: exact resolution
for all models, the 9 previously-drifting ones, codes/conflicts, created-device
labels + persisted JSON, build/release consistency, and the GUI/CLI wiring).
All 22 suites pass.

---

## Round 17 — "I see Launch but I cannot click it"

Your screenshot: *first_device*, Samsung Galaxy S24, **Stopped**, `✓ Root capable`
— and a Launch button that did nothing. No emulator, no error, no toast.

Three independent causes, all of them fixed (the first one is the real trigger):

### 1. A launch chain that outlived its emulator ate every click
Stop only killed the emulator; the background launch thread kept waiting for
`sys.boot_completed` (up to `EMULATOR_BOOT_TIMEOUT`, 4 minutes). While it did,
`POST /api/devices/<name>/launch` answered

```json
{"success": true, "state": "booting", "message": "Launch already in progress"}
```

— a **success** for a request that does nothing. The dashboard showed
"Launching…", the card stayed Stopped, and repeating the click changed nothing.
(Also reachable after a failed launch, where `_launch_state` was left at
`error`/`booting` with no chain behind it.)

- `DeviceManager` now hands every launch an **epoch token**; `stop_device()`
  bumps the epoch, so the chain aborts at its next checkpoint (process start,
  boot poll, before auto-root/spoof), kills the half-started emulator and
  raises `LaunchCancelledError`. A Stop that lands *before* the thread enters
  `launch_device` can no longer be swallowed (the GUI claims the token before
  spawning the thread).
- The GUI records the chain's lifecycle (`set_launch_running` /
  `set_launch_finished`) and **only trusts** `starting`/`booting` while that
  chain is alive: a stale state is reported as idle and can never refuse a
  launch again.
- `stop` (and a successful force-stop) always resets the chain state to idle.

### 2. A "successful" no-op is now an error, not silence
`launchDevice()` matched `"Launch already in progress"` and checked whether the
device is *really* up. If it isn't, it re-asks once (the server is allowed to
clear a chain with no thread) and, if it still refuses, says so instead of
pretending. A failed launch also turns the card's button into
**↻ Retry launch** (one click), instead of only the small ⚠ tag.

### 3. The card could look clickable and be dead
- Busy buttons were rendered with `disabled`, which swallows clicks silently
  — right-click, faded, nothing at all. They are now `aria-disabled` and the
  delegate explains the no-op ("`first_device` is still starting — wait for it
  to finish booting."), plus a `.btn[aria-disabled]` style that looks inert.
- The grid re-renders every 10 s **and** on every action. A click whose
  mousedown landed on the old button and mouseup on the new one fired on the
  grid and was dropped — indistinguishable from "the button doesn't work".
  The handler now remembers where the press started (`pointerdown`) and replays
  the action through one `runCardAction()` dispatcher.
- The Launching state stays visible on the card (`⏳ Launching…` + spinner
  cursor) instead of the button vanishing for the whole boot.

Covered by the new `tests/launch_click_test.py` (41 checks: epoch cancellation,
stop-during-boot via the API, stale chain state, click-after-stop relaunch, and
static guards for the JS/CSS wiring). Verified end-to-end in a real DOM
(jsdom + the live Flask app + fake SDK): Launch → Running → Stop → Launch →
Running, with the card returning to a live 🚀 Launch after the stop. All 23
suites pass.

---

## Round 18 — "after clicking stop make the device stop properly"

Stop used to declare success as soon as the *recorded pid* disappeared. On a
real machine that is not the same as "the emulator is gone", so the card could
say **Stopped** while the VM kept running (or, worse, the next Launch died with
"port in use"). Five distinct holes, all closed:

### 1. `connect()` and `bind()` were both wrong probes
`_port_in_use` decided with `connect()`. A wedged emulator with a full accept
backlog refuses/times out on connect **while still holding the port**, so Stop
called a running VM "gone". The naive fix (`bind()`) is wrong the other way: a
harmless `TIME_WAIT` entry makes bind fail, so a fully stopped device looked
busy forever. The probe now uses the **listener table** (`ss`/`lsof`/`netstat`)
when available, treats a connect timeout as "taken", and only falls back to
`bind()` when no listener table exists.

### 2. The only handle on a stuck VM was deleted
`_is_running()` popped the persisted pid whenever the console was silent - and
it runs on *every* dashboard refresh, so it ran before Stop. A VM that was still
starting (or wedged before its console listened) became untracked, and Stop had
nothing left to kill. The record is now kept as long as there is evidence of the
device (`_emulator_evidence`: a recorded/hooked pid that is ours or looks like
an emulator, or one of our processes owning the port pair).

### 3. Stop did one kill and hoped
`emu kill` is a request. The VM can ignore it (and SIGTERM), or its pid can be
from an earlier launch. `stop_device` now **sweeps** until the device is really
gone: recorded pid → owner of the console/adb ports → stray `emulator`/`qemu`
processes for this AVD, killing each one's **session leader** so the whole tree
(helper children holding ports included) goes with it. The kill escalates
SIGTERM → SIGKILL instead of a single SIGKILL, and AVD lock files the dead VM
left behind are cleared afterwards.

### 4. Zombies and recycled pids
- A zombie answers `kill -0` but runs nothing: Stop waited out its whole timeout
  on one. `_pid_alive` now reads `/proc/<pid>/stat` and ignores `Z`/`X`.
- Killing "whatever pid is recorded" is dangerous (Windows recycles pids fast).
  Nothing is signalled without proof it is ours: the pid must be a process this
  manager launched, an emulator/qemu executable, or the console on its port
  must answer with its AVD (`_may_kill_for_device`). Executable *names* are
  matched, never command lines - an earlier attempt matched args and matched the
  test harness's own shell.
- A foreign program on the device's ports is **reported, not killed**: Stop
  answers 409 naming the pid and its command line.

### 5. A "successful" stop that wasn't
`_force_release` only checked the console, so a foreign port holder produced
`success: true`. It now requires the port pair to be free as well. Stop failures
are stored as `stop_error` (separate from `launch_error`), the card shows
**⚠ Stop didn't finish** ("↻ Retry stop" on the button), Details explains it, and
the dashboard waits for the device to actually report not-running
(`confirmDeviceStopped`) before saying "Stopped".

Covered by the new `tests/stop_really_stops_test.py` (44 checks: port-probe
truth table, stubborn emulator ignoring `emu kill`+SIGTERM, untracked emulator
found via its port, helper child, recycled pid that must survive, foreign holder
that must be reported and survive, and the normal launch→stop→launch cycle).
Verified end-to-end in a real DOM as well. All 24 suites pass.

## Round 19 — cameras match the phone, the GPU is no longer yours, spoof self-heals, radios gated

Four reports, one theme: the emulator's *own* identity kept leaking through
surfaces the hook layer could not cover.

### 1. "make the cameras match the device"
`generate_camera_specs()` picked a sensor from a generic pool keyed by
`(brand, board, model)` and the phone model never entered the decision — so a
Galaxy S24 Ultra advertised a plausible-but-wrong sensor, and the AVD's generic
`640x480 / 1280x720` stream list stayed in place on both facings.
→ `profiles/device_profiles.py` now carries `CAMERA_PROFILES`, the real
back/front sensor of **every** model in `ALL_DEVICES` (sensor, max resolution,
pixel size, focal length, aperture, orientation). `generate_camera_specs()`
reads it (by name *or* hardware code, e.g. `SM-S928B`), builds the sensor's
own `stream_sizes` list and keeps the front/back independence guarantees;
unknown/custom models fall back to the per-facing pool so they still differ.
`camera_hooks.js` additionally rewrites
`SCALER_AVAILABLE_STREAM_CONFIGURATIONS` from that list and reports the phone
model for `LensMake`/`LensModel`/`UniqueCameraModel` in EXIF.

### 2. "change the GPU render cause OpenGL reveals it as my GPU still"
Unconditional GL string spoofing was not enough: the emulator ran with
`-gpu auto` (= host GL passthrough), so the guest *loaded and reported* the
host card (`AMD Radeon RX 6700 XT`) through every path Frida had not reached.
→ `config.DEFAULT_GPU_MODE` is now `swiftshader_indirect` (the emulator's own
guest/software stack, shipped with the SDK on every host), and
`DeviceManager.resolve_gpu_mode()` refuses `auto`/`host`/`angle`/`desktop` for
any spoofed device, logs and explains the substitution, and saves the mode as
`device["gpu_mode"]` (also written to `hw.gpu.mode` in `config.ini`, shown in
Details as *Emulator renderer*). `gpu_hooks.js` (the Chromium WebGL/GPU-process
script) gained native `glGetIntegerv` limits, native
`glGetShaderPrecisionFormat`, Java `glGetShaderPrecisionFormat` and a
`SystemProperties` hook — while `ro.hardware.egl`/`vulkan` stay untouched, as
before, because they are `.so` filename suffixes.

### 3. "make the spoof always automatically recover and apply into the device on the launch"
The intent (`auto_spoof`/`auto_hooks`) existed but only launch-with-`--frida`
and a startup sweep used it, and a *partial* spoof (one prop reverted on the
same boot) was never noticed.
→ New `spoof_drift()` compares the device's own verified prop report against
the identity (rebuilt through `build_identity_prop_maps`), `autorecover()` /
`autorecover_all()` re-root, re-apply the verified spoof + system settings,
re-attach hooks from intent (no radio gate any more — see Round 24); launch re-attaches
hooks from a saved intent without needing the frida flag; drift now marks the
device `runtime_stale`/`needs_restore`; the dashboard sweep uses
`autorecover_all()`; endpoints `POST /api/devices/<name>/autorecover`, and a
**↻ Recover now** action on the card and in Details. Nothing is reported as
applied that the device did not confirm.

### 4. "make the device be on airplane mode when spoof and fingerprint not applied"
> ⚠️ **Superseded — see Round 24.** The gate described below was removed at the
> user's request: airplane mode is manual only and nothing toggles it
> automatically. Kept for the record.
An unspoofed guest announces the emulator's own IMEI/baseband/network.
→ `SystemSpoofer.set_airplane_mode()` writes
`settings put global airplane_mode_on`, broadcasts `AIRPLANE_MODE`, flips
`svc wifi/data` and **verifies** with `settings get`. The launch path closes the
gate before spoofing, opens it only when `spoof_applied` is verified, and
`_apply_system_spoof` re-checks it; `enforce_airplane_gate()` is idempotent per
boot, persists `airplane_mode`/`airplane_gate`, and is re-evaluated after every
restore/recovery. The card shows **✈ Airplane (spoof pending)**, Details explains
why and offers *Recover* / *Turn airplane off anyway*, and a failed spoof keeps
the device offline with the reason on the card instead of a green "Running".

Covered by `tests/round19_test.py` (per-model cameras + stream sizes, guest-GPU
resolution rules, drift detection and unattended recovery, the airplane gate on
a live fake device and an end-to-end launch that closes then opens it). All 25
suites pass.

## Round 20 — the app never creates a device for you

`bootstrap.py` (what `START.bat` / `start.sh` run) created a starter device
named **`first_device`** whenever the list was empty. That is why a fresh
install already had a phone nobody chose - a Samsung Galaxy S24 with default
cameras - sitting on the dashboard, and why every later report was filed
against `first_device`.

- **No implicit creation anywhere.** `prepare_devices()` now only reports what
  exists (`No devices yet - nothing was created for you.`) and points at the
  New Device dialog. The launcher creates a device **only** when the command
  line names it: `bootstrap.py --create my_phone` (`--device` kept as an
  alias). `--no-device` still parses, but it is now the default behaviour.
- **`POST /api/devices` requires a model.** An empty or modelless request used
  to fall through to the "create" path; it is now a 400 that says the app never
  creates a device on its own. The dashboard's New Device dialog is the only
  way in (plus `python main.py create`).
- **`python main.py` with no command opens the dashboard** instead of printing
  help, so the no-creation default is the easy path.
- The empty state says so out loud ("Nothing is created for you…") and the
  button reads *Choose a model & create a device*.

New `tests/no_auto_device_test.py` pins it: an empty install stays empty after
bootstrap (`empty`), after the dashboard loads, and after an empty POST; the
dashboard renders the empty state; `--create NAME` still works; and the only
create call sites are the ones that name a model.

## Round 21 — "is it booting or is it dead?" is now answerable

A first boot on `-no-snapshot` takes **minutes**, and the card said the same
thing the whole time: a "Starting…" pill and one line,
*Booting AVD → auto-root → spoof → Frida hooks…*. A slow boot, a hung boot and
a dead emulator looked identical, so the only way to find out was to wait, or
to open Details and read the raw log.

- **The launch chain reports every step.** `DeviceManager` keeps a progress
  record per launch (`begin_launch_progress` / `report_launch_progress` /
  `fail_launch_progress` / `clear_launch_progress`) with the phases
  *preparing → starting → booting → rooting → spoofing → hooking → finishing*.
  `wait_for_boot` calls back about once a second, so the boot step shows
  `Booting Android (92s elapsed, 208s left - adb: device)` instead of nothing.
- **The bar moves inside a step.** The percentage is weighted per phase and
  gets partial credit from markers like `(60/120)`, so a long boot does not
  freeze the bar.
- **The card shows it.** The panel sits directly under the device header with
  ✓ done / animated current / ○ pending rows, elapsed time, step `n/7`, the
  current sentence, `role="progressbar"` + `aria-live` for screen readers, and
  the pill reads `Starting 44%`.
- **Failure is not a spinner.** A failed launch freezes the panel red, names
  the failing step and shows the error's first line; Stop clears the record so
  a closed device never keeps a phantom panel.
- **Live-ish updates.** A starting card is polled every 1.2 s instead of the
  10 s idle interval, and the poller stops as soon as nothing is starting.

`tests/boot_progress_test.py` covers the phase order, a non-decreasing
percentage, a real (fake-SDK) launch walking the chain to "Ready", the failed
and stopped states, and the panel's markup/ARIA/poller.

## Round 22 — the spinner was broken, and "checking SDK…" could hang

Two reports, one screenshot: the active-step icon looked like a torn arc, and
the panel sat on *Checking SDK, AVD and ports / Validating the Android SDK and
the AVD* indefinitely.

**The icon.** It was a `0.62rem` box with a `2px` border and `0.165rem`
margins: at that size the border collapsed into a lopsided sliver that read as
a broken shape rather than a spinner. It is now a proper `0.78rem` square
(`box-sizing: border-box`, `flex: 0 0 0.78rem` so the row cannot squash it)
with two coloured edges for a clean gap, a 0.8 s rotation, and a slower ring —
not a freeze — under `prefers-reduced-motion`.

**The phase that never ended.** Everything before the emulator process spawns
was one unnamed sentence, so any slow call there looked like a hang:

- that phase now reports each check separately — emulator binary, adb, AVD,
  system image, renderer, acceleration, console port — so the list keeps
  moving instead of repeating one line;
- `emulator -accel-check` had a 30 s timeout and was **only ever a warning**.
  On Windows/WHPX it can sit there for the whole budget before the launch even
  starts. It now gets 8 s and continues regardless, and its verdict is shown
  as a step (`Acceleration: usable`);
- the console-port scan (`while port in used or in use…`) could walk the port
  range probing each one. It is bounded now (64 probes / 20 s) and reports the
  busy ports it skips, failing with an actionable error instead of looping;
- a watchdog (`start_progress_watchdog`) annotates a step that has had nothing
  new for 20 s: which step it is really on, how long it has been quiet (live,
  counting up) and the last real emulator log line — or an honest *"has not
  written a log line yet"*. A finished chain is left alone, and the CLI
  (`main.py launch`) streams the same phases with percentages.

`tests/boot_progress_test.py` grew sections 6-8: the stall flag, the live
quiet counter, the honest no-log wording, the bounded port scan, the 8 s
acceleration budget, the sub-step labels, and the spinner's box/border/animation
rules.

## Round 23 — "No free emulator console port available" on a clean machine

The card said **Stopped**, the device had never launched, and the launch died
with *"No free emulator console port available (tried 6 ports starting at
5554). Stop any leftover emulator processes and try again."* Nothing was
running - the port *probes* were wrong, in three separate ways:

1. **`connect()` was trusted** (`_connect_says_busy`): anything that was not a
   clean `ECONNREFUSED` counted as "in use". On Windows a closed port often
   answers with `WSAEINVAL`/"unreachable" instead, so every candidate port
   looked busy. The listener table is now the only authority; when it is
   unavailable, only a real emulator console answering on the port (or an
   emulator process owning it) counts.
2. **Any live pid counted as "ours"** on Windows (`_pid_is_ours` fell back to
   `True`), so an unrelated local server made its port permanently unusable
   and could keep a stopped device looking busy. Ownership is now strictly
   emulator-shaped (`emulator.exe` / `qemu-system-*`), which is enough because
   the app always launches that binary itself.
3. **The allocator gave up** after a few ports. It now scans a bounded window
   (`PORT_SCAN_SPAN`), reports which ports it skipped, and if every probe
   disagrees it hands the emulator a port to try - the emulator's own "port in
   use" error is far more useful than a pre-emptive refusal.

Also:

- **Leftover emulators are cleaned up.** `_release_leftover_emulators` kills
  orphaned emulator/qemu processes squatting on console ports (identified by
  executable, never by port alone) and reuses their ports. A non-emulator
  listener is left alone and simply skipped.
- **The toast shows the reason.** It re-printed *"Open Details for the log
  tail, or Settings → Run doctor"* even when the error already said what was
  wrong; it now shows the first line of the actual error.
- **`python main.py doctor` has an "Emulator ports" check** that lists real
  listeners, their owners, whether those are ours, and the next console port.

`tests/port_allocation_test.py` (new, 6 sections) pins the probe behaviour, the
bounded allocator with its fallback, the leftover cleanup, a full launch on a
clean machine, the toast wording and the doctor row.

## Round 24 — the automatic airplane-mode gate is gone

The app used to hold a device in **airplane mode until its spoof verified**: it
set `airplane_mode_on` before applying the spoof, released it only on a verified
spoof, re-closed it whenever a spoof failed or drifted, and did all of that
again on every launch, restore and background recovery. That is now removed
entirely, as requested.

Removed:

- `DeviceManager.enforce_airplane_gate()` and the rule behind it
  (`_airplane_required`, `_spoof_current`);
- every call site — launch chain, `restore_device`, `autorecover`,
  `autorecover_all`, the manual spoof endpoint and the auto-root path;
- the derived `airplane_gate == "airplane"` / `airplane_required` states the UI
  used to render;
- in the dashboard: the **✈ Airplane (spoof pending)** card tag, the
  *"Airplane mode is on — spoof not applied"* alert and its **Turn airplane off
  anyway** override;
- the doctor's *"unspoofed device with its radios on"* warning.

Kept (and still honest):

- `set_airplane_mode()` and `POST /api/devices/<name>/airplane` — the manual
  switch, verified with `settings get global airplane_mode_on` and surfaced as
  `Radios: ✈ Airplane` / `Online`, plus the error alert if it fails;
- spoof verification itself: a spoof that does not take is still reported
  (`is still NOT spoofed after launch`, `spoof_error` on the card). It just no
  longer also cuts the user's radios;
- `{"auto": true}` on the airplane endpoint, which now changes nothing and
  reports the current state (older clients do not break).

`tests/no_airplane_gate_test.py` (new) pins the removal: the API is gone, no
module calls it, a launch and a recovery leave `airplane_mode_on` untouched, a
failed spoof keeps the device online but still flagged, the UI no longer claims
a pending spoof is holding the radios off, and the manual switch still works.
`tests/round19_test.py` section 4 was rewritten to assert the removal instead
of the old gate.

---

## Round 25 — "after deletion the AVD does not get deleted in `C:\Users\user\.android\avd`"

The screenshot showed 15 `antidetect_*.avd` folders and a `samsung_f14578.avd`
still sitting in `C:\Users\user\.android\avd` after every device had been
deleted from the app.

### The bug
`delete_device()` removed exactly one path:

```python
avd_path = config.AVD_HOME / f"{device['avd_name']}.avd"
```

and `config.AVD_HOME` was `ANDROID_AVD_HOME`, else the **saved** `avd_home` in
`data/settings.json`, else `~/.android/avd`. When the saved value did not match
the machine — another install's settings file, an SDK that moved, a path that
was typed by hand — it pointed at a folder containing no AVDs. So:

- `rmtree()` ran on a path that did not exist (`exists()` was checked, so it was
  a silent no-op — `ignore_errors=True` would have hidden a failure anyway);
- `~/.android/avd/<avd>.avd` was **never touched**;
- the device JSON was deleted regardless, so the app returned
  `{"success": true}`, the dashboard toasted *"Deleted x"*, and the phone
  vanished from the list — while its AVD stayed on disk, still listed by
  `emulator -list-avds`, and still many GB.

A failed delete could also lie in the other direction: `shutil.rmtree(...,
ignore_errors=True)` reported nothing when Windows refused (folder held by a
running emulator, "Access is denied").

### The fix
- `config._resolve_avd_home()` (new) resolves the AVD home the way the emulator
  does and **sanity-checks the saved setting**: `ANDROID_AVD_HOME` wins; then a
  saved `avd_home` *while it still holds AVDs*; then `~/.android/avd`. A saved
  path with no AVDs, while a native location has some, is corrected in place
  (`data/settings.json` is rewritten) and logged once.
- `DeviceManager._avd_homes()` (new) returns every candidate AVD home
  (`config.AVD_HOME`, `ANDROID_AVD_HOME`, `ANDROID_EMULATOR_HOME/avd`,
  `ANDROID_SDK_HOME/.android/avd`, `~/.android/avd`), de-duplicated
  case-insensitively (Windows).
- `DeviceManager._delete_avd_files()` (new) removes `<avd>.avd` **and**
  `<avd>.ini` from **every** one of those homes, retries once after clearing
  emulator `.lock` files, and returns `(removed, failed)` instead of swallowing
  errors.
- `delete_device()` now keeps the device record when anything could not be
  removed, and raises `EmulatorLaunchError` naming every path and reason, with
  the hint to close the emulator holding it. Partial deletes can no longer look
  like success, and nothing is orphaned.
- The API keeps returning `400 + {"success": false, "error": …}` in that case
  (it did already — the point is that it is now actually triggered), and the
  dashboard stops claiming *"Deleted x and its AVD"* when it cannot.
- `main.py delete` prints the AVD folder it removed plus the AVD home it used,
  so the next report of this kind is self-diagnosing.

### Leftovers from before the fix
Devices deleted by the old code left AVDs that no saved device points at, so
they cannot be removed from the dashboard. Two new surfaces handle them:

- `python main.py doctor` → **Orphaned AVDs** row: how many, their names, total
  size, and the command to clean them;
- `python main.py clean-avds [--dry-run|--yes]`: removes exactly those folders.
  It never touches an AVD that a saved device still uses, and is a no-op on a
  clean machine.

### Tests
`tests/delete_avd_test.py` (new, 7 sections) builds real `.avd`/`.ini` pairs in
several AVD homes and pins: a stale saved `avd_home` is ignored and corrected;
delete removes the AVD from `~/.android/avd` and from duplicates elsewhere;
`ANDROID_AVD_HOME` still wins over the setting; a locked AVD raises (CLI exit 1,
API 400 + `success: false`) and keeps the device record; `main.py delete`
prints the folder and home; orphan detection excludes live devices and
`clean-avds` removes only orphans.

---

## Round 26 — "show stopping after I close the device; if I can't click Launch, show a Kill device button"

### 1. The card never said "Stopping…"
`POST /api/devices/<name>/stop` ran the whole teardown **inside the request**:
`adb emu kill` → wait → tree kill → sweep for a lingering qemu child → verify
the console and both ports are free → make adb drop the serial. On a real
machine that is several seconds of nothing, so the card went from **Running**
straight to **Stopped** with no state in between — and the Launch button was
grey for a reason the user could not see.

→ The route now returns as soon as the teardown is accepted:
`{"success": true, "state": "stopping"}`, with the work on a daemon thread
(`{"wait": true}` keeps the old synchronous behaviour for scripts and the
existing tests). `_stopping_` is a real state:

- `GET /api/devices` reports `stopping: true` + `stop_started_at`, and the card
  renders a red **⏹ Stopping…** pill with a spinning dot, a notice
  (*"Stopping the emulator on port 5554… 3s"*) and a disabled ⏳ Stopping…
  button. The elapsed counter ticks every 500 ms, so the card can never look
  frozen;
- polling drops to 1.2 s while anything is stopping (it used to stay on the
  10 s idle refresh), and the browser polls the device list until the server
  says the emulator is gone;
- a second click does not start a parallel kill: it answers *"Stop already in
  progress"*.

### 2. A closed emulator made Launch look dead, with no way out
If the user closed the emulator window (or it crashed), the saved `pid`/
`serial` survived. `_is_launch_blocker()` then trusted that stale runtime and
refused the launch with *"Device already running"* while the card said
**Stopped** — precisely the state where Launch cannot be clicked and nothing
explains why. Some code paths (`_is_running`'s last-resort branch) also left the
dead pid in the record forever.

→ `_is_running` now checks for **ghost runtime** before honouring a recorded
pid/serial (`_is_ghost_runtime`): console silent for this AVD, no
emulator-shaped process on the ports, no adb serial for it → the stale state is
cleared and the device is honestly reported as stopped. The grace window (90 s
after launch) still protects a genuinely booting emulator, and an emulator that
answers on the console is never touched. Dead pids are also dropped instead of
being kept forever.

→ The card learns about it: `emulator_gone` (derived, never persisted) is true
when a device was launched by the app, is not running, and was never stopped by
the app. The status pill reads **Emulator closed**, a notice explains it, and
the card shows **🔪 Kill device**.

### 3. Kill device / `main.py kill`
`DeviceManager.kill_device(name)` is the explicit escape hatch: it cancels a
launch, runs the stop sweep, tree-kills the recorded emulator, runs
`_force_release`, drops the AVD locks and verifies the console and ports are
free. It reports what it actually found instead of claiming success:

- `{"killed": false, "note": "Nothing was left to kill - the emulator had
  already exited."}` for a device that is already clean;
- `{"killed": true, "note": "Killed process 4711 (AVD …) and freed the ports."}`
- `{"success": false, "still_running": true, "note": "Something still answers on
  the emulator ports after the kill; it is not an emulator this app may kill."}`
  + HTTP 409 when a foreign program owns the ports (it is reported, never
  killed).

`POST /api/devices/<name>/kill` and `python main.py kill <name>` expose it. The
button appears (a) on a card whose emulator was closed, (b) next to
**↻ Retry stop** when a stop failed, and (c) on a stopped card whose last stop
could not finish. The stop error prompt now says *"Force kill …"* and calls it.

### Tests
`tests/stop_state_test.py` (new, 7 sections, 41 checks) pins: stop answers
immediately with `state: "stopping"` and the card is told; the state clears by
itself only when the wedged emulator is really dead; the default stop is still
synchronous; a closed emulator is reported (`emulator_gone`) while a cleanly
stopped one is not; kill frees a wedged emulator and unblocks the launch; kill
on an idle device says "nothing was left to kill"; `main.py kill` exit codes;
dead pids get cleared. The card markup was also rendered through jsdom in all
five states (running / stopping / closed / stopped-with-error / stopped) to
confirm the pills, notices and button sets.

---

## Round 27 — Launch after closing, "proxy says invalid", and data that survives a re-install

### 1. "everytime after closing the device launch isnt available"
Two separate causes, both fixed:

- **The card hid Launch.** The previous round introduced an "Emulator closed"
  card whose action row *replaced* Launch with **Kill device**. A stopped card
  now always renders **🚀 Launch**; Kill device is an **additional** button
  (only when the emulator was closed, or the last stop did not finish).
- **The launch itself could be refused.** A closed/crashed emulator can leave a
  qemu child, a ghost adb serial or a recycled pid behind, and
  `_launch_blocker()` then answered *"Device already running"* while the card
  said **Stopped**. `_is_running()` now detects ghost runtime
  (`_is_ghost_runtime`) *before* honouring a recorded pid/serial: console silent
  for this AVD, no emulator-shaped process on its ports, no adb serial → the
  stale state is cleared. A booting emulator stays protected by the 90 s grace
  window and by the console probe; a live emulator is never touched.

Verified by closing the emulator three times in a row: `running=False`,
`emulator_gone=True`, `_launch_blocker=False` every time, and the rendered
card shows `[launch, kill, details, delete]`.

### 2. "proxy not working i try to apply it and it says invalid make it work"
`ProxyManager._parse_proxy_url()` only accepted a narrow canonical form and
rejected everything else with a one-line error. Now it accepts what proxy
providers actually hand out:

| Input | Parsed as |
|---|---|
| `host:port`, `host port`, `host` | http (port 8080 default) |
| `http://host:port`, `HTTP://…`, `https://host:port` | http / https |
| `socks5://`, `socks5h://`, `socks://` | socks5 |
| `socks4://`, `socks4a://` | socks4 |
| `user:pass@host:port` (any scheme) | credentials kept |
| `http://[::1]:8080` | IPv6 literal |
| trailing `/`, surrounding spaces/quotes | stripped |

A genuinely wrong value still fails, but the message says what to use
("Unsupported proxy scheme 'ftp'. Use http://, https://, socks5:// or
socks4:// …"). And it now *works*, not just validates:

- **SOCKS5 is applied live.** It used to answer "saved on the profile, relaunch
  to apply" and never did anything. `_set_socks_proxy()` writes the
  `socks5://host:port` value into Android's global proxy settings through adb
  (no destructive `iptables` REDIRECT) and confirms the emulator through its
  console; only if that fails does the profile value wait for the next launch,
  and the toast says so.
- **The launch flag is correct**: `-http-proxy` gets `socks5://…` for SOCKS
  (including `user:pass@`) and `http://…` for HTTP/HTTPS; a stored value that
  does not parse is ignored with a progress note instead of becoming a broken
  flag that kills the boot.
- Saving on a **stopped** device no longer leaves you guessing: the message is
  "SOCKS5 proxy saved - it is passed to the emulator with -http-proxy on the
  next launch (the device is not running now)", and the toast is informational
  rather than a green "success". The Details panel lists every accepted format
  and shows the saved value.

### 3. "make profile information data be saved in a different place like appdata"
`DATA_DIR` was `<project>/data`, so deleting/unzipping the app wiped every
device, identity and profile. It now lives in a per-user location:

| OS | Path |
|---|---|
| Windows | `%APPDATA%\AndroidSpoofer` |
| macOS | `~/Library/Application Support/AndroidSpoofer` |
| Linux | `$XDG_DATA_HOME/android-spoofer` (`~/.local/share/android-spoofer`) |

`ANDROID_SPOOFER_DATA` overrides it (portable installs, tests).
`_migrate_legacy_data()` runs on import: if the new home has no devices and the
old in-project `data/` does, devices, profiles and `settings.json` are
**copied** (never moved - a half-finished upgrade loses nothing), a
`.migrated-from-legacy` marker prevents repeating it, and the app logs
*"Moved your saved data to …"*. Read-only/TCC failures are non-fatal.

### 4. Found while testing #1: a launch/stop race could delete the device record
`_save_device()` wrote every device through **one** shared temp name
(`<name>.json.tmp`). A launch (its own thread) and a stop (the GUI's teardown
thread) saving at the same moment meant one thread's `os.replace()` found a temp
file the other had already moved:

```
[Errno 2] No such file or directory: 'midboot.json.tmp' -> 'midboot.json'
```

That killed the launch chain, and because the *record write* failed the device
could appear to vanish from the app - another way "launch isn't available"
happens in the wild. It reproduced roughly one run in three.

`_save_device()` now takes a re-entrant lock and writes through a unique
`tempfile.mkstemp()` file before the atomic replace (and cleans the temp file up
if anything fails). Twelve consecutive runs of the previously flaky suite are
green, and `tests/persist_proxy_launch_test.py` §2b races six threads on one
device: it fails with exactly the old error when the fix is reverted.

### Tests
`tests/persist_proxy_launch_test.py` (new, 3 sections, 74 checks) pins: a closed
emulator is reported and never blocks the next launch (three closes in a row);
the rendered card always offers Launch with Kill added (jsdom, not a source
grep); 16 proxy spellings parse to the right `(scheme, host, port)`; invalid
ones are refused with a usable message and the old value is kept; the launch
command carries `-http-proxy socks5://u:p@host:port` and refuses to pass a
garbage value; `%APPDATA%`/XDG/`ANDROID_SPOOFER_DATA` resolution; migration
copies devices, identities, profiles and settings and keeps the old folder; and
a fresh process reads the same store. `tests/resetprop_and_hooks_test.py` gained
the SOCKS live-apply assertions (and its old "SOCKS must refuse" expectation was
replaced).

## Round 28 — "remake the proxy UI" + spoofed phones must stop reporting emulator specs

Two reports in one round.

### 1. The proxy said "url invalid" while the URL was valid

The proxy lived only inside **Details** as a free-text box with a Save button.
Anything the strict parser disliked was answered with a generic "invalid", the
card showed a static `Proxy` tag whether or not anything was applied, and
"saved in the profile" and "applied on the running emulator" looked identical.

* **The panel is on the card now** (`buildProxyPanel` in `gui/static/js/app.js`):
  a switch, the scheme, the value, Apply/Re-apply, a copy button and a
  `Live` / `Saved` / `Off` chip. It sits above the action buttons.
* **It updates as you type.** `parseProxyInput` gives instant local feedback,
  then `POST /api/proxy/parse` (`ProxyManager.parse_report`) confirms against
  the *same* parser the backend applies, so a URL the server accepts is never
  shown as invalid — the actual reported bug. Feedback always says *why*
  (`Unsupported scheme "ftp" — use http, https, socks5 or socks4`), never a bare
  "invalid".
* **Off really turns it off.** `set_device_proxy(name, "")` clears the profile
  and calls `pm.clear_proxy()` on a running emulator; the message is "Proxy
  turned off on the running device — it goes out directly now."
* **Honest state.** The device record now carries `proxy_live` /
  `proxy_error` / `proxy_normalised`, so the chip cannot claim "Live" for a
  proxy that is only stored in the profile. Stopping a device clears
  `proxy_live`.
* The parser tolerates spaces around `://` and `:` (`http:// 1.2.3.4:8080`,
  `1.2.3.4 : 8080`) and normalises `socks5h://` / `socks://` / `https://` to
  what the emulator actually accepts.

### 2. With spoofing applied, Settings still showed the emulator

Your screenshot of Settings → About → CPU showed `CPU hardware: ranchu`,
`Supported ABIs: x86_64, arm64-v8a`, `GPU renderer: Android Emulator OpenGL ES
Translator (Google SwiftShader)`, `GPU vendor: Google (Google Inc.)`,
`Fabrication: 28 nm`.

The cause: `Build.*` props were spoofed, but everything Settings → CPU and
Settings → Storage read came from APIs nobody hooked, and the GPU identity was
picked per *brand* rather than per *model*, so phones fell back to a random
Adreno.

* **`profiles/hardware_specs.py`** gives every model in the device list a real
  spec entry — SoC, cores, micro-architecture, fabrication, GPU, RAM, storage,
  ABIs, screen — including the OnePlus, Huawei and Honor models that had none.
  The GPU is matched to the SoC (Snapdragon → Adreno, Exynos 2400 → Xclipse 940,
  Tensor → Mali-G715), and 13 missing chips were added to
  `profiles/gpu_profiles.py` (Mali-G715/G720/G710/G78/G68 variants, G57, G610,
  Immortalis-G715, Adreno 720/725/642L, Maleoon 910) so no phone reports a chip
  its SoC does not have.
* **`frida_toolkit/scripts/spec_hooks.js`** answers `ActivityManager
  .getMemoryInfo` (RAM), `StatFs` (storage), `Build.SUPPORTED_ABIS` /
  `System.getProperty("os.arch")` / `availableProcessors`, `GLES20.glGetString`
  / `EGL14.eglQueryString` (renderer + vendor), `Resources.getDisplayMetrics`
  (screen) and the telephony calls (`getLine1Number`, `getMsisdn`,
  `getSimOperatorName`, `getNetworkOperatorName`, country ISO).
* **`native_hooks.js`** now also serves a memfd-backed `/proc/meminfo` and a
  per-core ARM `/proc/cpuinfo` (`Hardware: Exynos2400`, implementer `0x41`),
  replacing `Hardware: ranchu`.
* **RAM/storage look like the phone, honestly.** Settings reports the model's
  retail RAM and a storage size shops actually sell, with `used + free = total`.
  The AVD's real limit is kept in `ram_available_mb` and stated on the card;
  `mode="honest"` reports the real figure. Cores are never claimed above what
  the VM has.
* **Polish +48 line.** `profiles/phone_numbers.py` uses the supplied
  `profiles/phone_numbers.txt` (1000 numbers) and a prefix→carrier table, so the
  carrier shown (`Orange`, `Play`, `Plus`, `T-Mobile`, `Lycamobile`, `Netia`,
  `nju mobile`, `Plush`, `Red Bull Mobile`) always matches the number's prefix.
  Numbers the file does not cover are generated inside that carrier's own prefix
  block, so all nine carriers appear; numbers are stored per device and never
  reused. A duplicate `784` key in the first draft of the table silently dropped
  a carrier — the table is checked for unique keys now.
* 📋 Details gained a **Reported Phone Specs** grid (RAM, storage, chipset,
  cores, process, GPU, ABIs, screen, line + carrier) with the clamping notes.

The SIM props (`gsm.sim.operator.alpha` / `gsm.operator.alpha` / `gsm.*.iso-country`
/ `gsm.*.numeric`) are now part of the *verified* prop set, so "the line is Polish"
is checked with `getprop`, not just claimed. Replacing the old source-text
CarrierConfig guard with a value-level check immediately caught a real bug: a
numeric that already started with `260` was prefixed again (`26006` →
`26026006`), which is precisely the CarrierConfig corruption that guard existed
to prevent.

`tests/proxy_ui_test.py` (78 checks) and `tests/spec_spoof_test.py` (127 checks)
cover both halves; 34/34 suites are green.

## Round 29 — "fix settings not working in android device"

Settings was still crashing on Android 14 after the previous round, because that
round fixed **one of three writers**. `HardwareSpoofer` had been cleaned up, but
two other paths wrote the same load-bearing properties straight afterwards:

| Path | Wrote | Reached the device via |
|---|---|---|
| `spoof/system_spoof.py` → `SystemSpoofer._apply_build_props` | `ro.product.device`, `ro.product.<part>.device`, `ro.build.characteristics="default"` | runs **immediately after** `HardwareSpoofer` on every Spoof click, launch and restore |
| `spoof/system_props.py` → `generate_props_config` | `ro.product.device`, `ro.product.board`, `ro.product.<part>.device` | `emulator -prop` at boot, i.e. before `init` — the most load-bearing moment there is |

`SystemSpoofer` also used bare `setprop` for `ro.*`, which Android ignores after
`init`: the pass silently "succeeded" while doing nothing, and the boot `-prop`
flags were what actually delivered the poison. So the fix looked applied, the
identity looked spoofed, and Settings died on the next launch.

**Why these keys specifically.** They are not descriptive, they are *lookups*:
`ro.product.board`/`ro.hardware` are the HAL and EGL/Vulkan `.so` filename
suffix, `ro.product.device`/`ro.build.product` are framework resource-path
prefixes, `ro.build.characteristics` gates Settings/SystemUI features, and
`ro.product.cpu.abilist` drives zygote/linker ABI selection. A phone codename in
any of them makes the platform hunt for files the x86_64 emulator does not ship;
Settings is simply the first process a user notices dying.

**The fix is one policy, enforced everywhere.** New `spoof/prop_policy.py` holds
the guarded key set (plus `ro.hardware.*` as a prefix, and a separate
"runtime-only" set for `ro.kernel.qemu`/`ro.boot.qemu`). Every write path now
filters through `filter_props()`:

* boot `-prop` flags (`system_props.generate_props_config`),
* `SystemSpoofer._apply_build_props`,
* `HardwareSpoofer._rewrite_build_props` (this one edits the *shared image*, so a
  bad key there would outlive the emulator and break every later boot),
* the `resetprop` batch, which only ever receives the filtered set.

A hand-edited `boot_props` list or a device JSON saved by an older version is
filtered too, and each path prints what it refused to write.

**Existing devices are healed, not just spared.** Fixing the writers does nothing
for a device that already has `ro.hardware=zuma` in `/dev/__properties__` — or,
worse, in a `build.prop` that survives every reboot. `HardwareSpoofer.
_repair_toxic_props()` probes the guarded keys and restores them, taking the
exact native value from the pre-edit `build.prop` copies the spoofer already
keeps in `/data/local/antidetect-buildprop-backup/`, falling back to the
emulator's documented values for the HAL keys. It runs **before** the
"already spoofed, nothing to do" early return — otherwise a poisoned device whose
identity props already matched would never heal. Keys it cannot recover (there is
no safe default for `ro.build.product`) are reported in the device record as
`unresolved_repairs` with what to do about it, rather than guessed at.

**One more regression found while testing this.** `native_hooks.js` skips its
`__system_property_get` interceptor when it believes the OS props are already
spoofed, and that probe only looked at `ro.product.model`, `ro.serialno`,
`ro.kernel.qemu`. With the guarded keys now deliberately *native*, the shortcut
would fire and leave apps reading `ro.hardware=ranchu` — silently undoing the
whole point. The probe now also checks `ro.hardware`, `ro.product.device`,
`ro.product.board` and `ro.build.characteristics`, so the per-app hook always
engages while the system stays intact.

**Verified.** A real `emulator` launch is asserted end to end: the launch command
carries the cosmetic identity (model, brand, per-partition model/brand/name,
fingerprint, build id, serial, baseband, all six Polish SIM props) and **zero**
guarded keys. `tests/settings_crash_test.py` (new, 78 checks) also plants the
older version's output on a fake device and asserts the repair restores
`ro.hardware=ranchu`, `ro.product.device=generic_x86_64` and
`ro.build.characteristics=emulator` while leaving the spoofed model untouched;
35/35 suites are green.
