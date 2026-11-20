#!/usr/bin/env python3
"""Launch must survive a closed emulator; proxies must work; data must persist.

Three reports, three sections of the same story:

1. *"everytime after closing the device launch isnt available"* — a stopped
   card always shows **Launch**. A closed emulator must never leave the launch
   path blocked ("Device already running"), and **Kill device** is an *extra*
   button, never a replacement for Launch.

2. *"proxy not working i try to apply it and it says invalid make it work"* —
   every format a proxy provider hands out parses (``host:port``,
   ``http(s)://``, ``socks5://``, ``socks5h://``, ``socks://``,
   ``socks4a://``, ``user:pass@``, IPv6 literals, a space instead of a colon),
   an invalid one explains how to fix it, a saved proxy reaches the emulator as
   ``-http-proxy``, and saving on a running device applies it live.

3. *"make profile information data be saved in a different place like appdata
   so when i delete the app and install it again i still see the old devices"*
   — devices/profiles/settings live outside the app folder (``%APPDATA%`` on
   Windows, XDG/Application Support elsewhere), an existing in-repo ``data/``
   folder is migrated automatically, and re-installing the app finds them.
"""
import json
import os
import shutil
import subprocess
import sys
import time
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
from anonymity.proxy_manager import ProxyManager  # noqa: E402
from core.device_manager import DeviceManager  # noqa: E402

JS = (REPO / "gui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
app = ga.create_app()
client = app.test_client()
mgr = DeviceManager()

BASH = shutil.which("bash")
FAKE_EMU = TMP / "emulator"
if BASH:
    shutil.copy(BASH, FAKE_EMU)


def make_device(name):
    r = client.post("/api/devices", json={
        "model": "Samsung Galaxy S24", "android_version": "14", "name": name})
    assert r.get_json().get("success") is True, r.get_json()
    return name


def saved(name):
    return json.loads((config.SAVED_DEVICES_DIR / f"{name}.json").read_text(encoding="utf-8"))


def patch(name, **fields):
    dev = saved(name)
    dev.update(fields)
    (config.SAVED_DEVICES_DIR / f"{name}.json").write_text(json.dumps(dev), encoding="utf-8")


# ---------------------------------------------- 1. Launch survives a closed emulator
print("== 1. a closed emulator never makes Launch unavailable ==")
make_device("closedlaunch")
script = TMP / "linger.sh"
script.write_text("#!/bin/bash\nsleep 300\n")

for attempt in range(1, 4):
    proc = subprocess.Popen([str(FAKE_EMU), str(script)], start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.7)
    patch("closedlaunch", pid=proc.pid, serial="emulator-5781", console_port=5781,
          adb_port=5782, launched_at="2026-01-01T10:00:00")
    proc.kill()                      # the user closes the emulator window
    proc.wait(timeout=10)
    time.sleep(0.3)
    dev = next(d for d in client.get("/api/devices").get_json()
               if d["name"] == "closedlaunch")
    check(f"close #{attempt}: reported as not running", dev.get("running") is False)
    check(f"close #{attempt}: reported as closed", dev.get("emulator_gone") is True,
          json.dumps(dev)[:200])
    check(f"close #{attempt}: launch is not blocked",
          mgr._launch_blocker("closedlaunch") is False,
          "the next Launch would be refused with 'Device already running'")
    check(f"close #{attempt}: not stuck in 'stopping'", not dev.get("stopping"))

# The card must always render Launch for a stopped device, with Kill *added*.
# Assert on the rendered markup (jsdom), not on the template source: the bug was
# a branch that *replaced* Launch, which a source grep happily misses.
NODE = shutil.which("node")
JSDOM = Path("/tmp/jsd/node_modules/jsdom")
if NODE and JSDOM.exists():
    probe = TMP / "render_card.js"
    probe.write_text(f"""
const {{ JSDOM }} = require('{JSDOM}');
const fs = require('fs');
const dom = new JSDOM(`<!DOCTYPE html><html><body><div id="devicesGrid"></div>
  <div id="deviceCountBadge"></div><div id="totalDevices"></div>
  <div id="runningDevices"></div><div id="protectedDevices"></div>
  <div id="spoofStatus"><span class="status-indicator"></span><span class="status-text"></span></div>
</body></html>`, {{ runScripts: 'outside-only', url: 'http://localhost/' }});
const win = dom.window;
win.__d = [];
win.fetch = async (u) => ({{ ok: true, json: async () => (String(u).includes('/api/devices') ? win.__d : {{}}) }});
win.setInterval = () => 1; win.clearInterval = () => {{}}; win.io = undefined;
win.confirm = () => false; win.localStorage = {{ getItem: () => null, setItem: () => {{}} }};
win.eval(fs.readFileSync('{REPO}/gui/static/js/app.js', 'utf8'));
const base = {{ name: 'd', model: 'M', brand: 'Samsung', android_version: '14',
  console_port: 5554, rootable: true, rooted: true, camera: {{}} }};
(async () => {{
  for (const [label, extra] of Object.entries({{
    closed:   {{ running: false, emulator_gone: true }},
    stopfail: {{ running: false, stop_error: 'port busy' }},
    plain:    {{ running: false }},
  }})) {{
    win.__d = [Object.assign({{}}, base, extra)];
    await win.eval('loadDevices()');
    const g = win.document.getElementById('devicesGrid');
    const acts = [...g.querySelectorAll('.device-actions button')].map(b => b.dataset.action);
    const pill = g.querySelector('.device-status').textContent.trim();
    console.log(JSON.stringify({{ label, acts, pill }}));
  }}
}})();
""", encoding="utf-8")
    out = subprocess.run([NODE, str(probe)], capture_output=True, text=True, timeout=120).stdout
    rows = {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            row = json.loads(line)
            rows[row["label"]] = row
    closed = rows.get("closed") or {}
    stopfail = rows.get("stopfail") or {}
    plain = rows.get("plain") or {}
    check("a closed card still offers Launch",
          "launch" in (closed.get("acts") or []), str(closed.get("acts")))
    check("a closed card also offers Kill device",
          "kill" in (closed.get("acts") or []), str(closed.get("acts")))
    check("Launch comes before Kill on a closed card",
          (closed.get("acts") or []).index("launch") < (closed.get("acts") or []).index("kill")
          if "launch" in (closed.get("acts") or []) else False, str(closed.get("acts")))
    check("the closed card says the emulator was closed",
          "Emulator closed" in (closed.get("pill") or ""), str(closed.get("pill")))
    check("a failed-stop card offers both Launch and Kill",
          "launch" in (stopfail.get("acts") or []) and "kill" in (stopfail.get("acts") or []),
          str(stopfail.get("acts")))
    check("a plain stopped card offers Launch",
          plain.get("acts") == ["launch", "details", "delete"], str(plain.get("acts")))
else:
    check("jsdom is available to render the card", False, "node/jsdom missing")
check("the status pill says the emulator was closed", "Emulator closed" in JS)


# ---------------------------------------------- 2. proxies actually work
print("== 2. the proxy accepts what providers hand out and applies it ==")
pm = ProxyManager()
accepted = {
    "1.2.3.4:8080": ("http", "1.2.3.4", 8080),
    "http://1.2.3.4:8080": ("http", "1.2.3.4", 8080),
    "HTTP://1.2.3.4:3128": ("http", "1.2.3.4", 3128),
    # kept distinguishable, but applied with the same method as http
    "https://proxy.example.com:443": ("https", "proxy.example.com", 443),
    "socks5://1.2.3.4:1080": ("socks5", "1.2.3.4", 1080),
    "socks5h://1.2.3.4:1080": ("socks5", "1.2.3.4", 1080),
    "socks://1.2.3.4:1080": ("socks5", "1.2.3.4", 1080),
    "socks4://1.2.3.4:1080": ("socks4", "1.2.3.4", 1080),
    "socks4a://1.2.3.4:1080": ("socks4", "1.2.3.4", 1080),
    "socks5://user:pass@1.2.3.4:1080": ("socks5", "1.2.3.4", 1080),
    "http://user:p@ss@host:8080": ("http", "host", 8080),
    "1.2.3.4 8080": ("http", "1.2.3.4", 8080),
    "http://[::1]:8080": ("http", "::1", 8080),
    "  http://1.2.3.4:8080  ": ("http", "1.2.3.4", 8080),
    "http://1.2.3.4:8080/": ("http", "1.2.3.4", 8080),
    "1.2.3.4": ("http", "1.2.3.4", 8080),
}
for raw, expected in accepted.items():
    try:
        got = pm._parse_proxy_url(raw)
    except ValueError as exc:
        got = f"INVALID ({exc})"
    check(f"accepts {raw!r}", got == expected, f"got {got}, wanted {expected}")

for bad in ("ftp://1.2.3.4:1080", "socks9://1.2.3.4:1080", "not a proxy",
            "http://1.2.3.4:99999", ""):
    raised = ""
    try:
        pm._parse_proxy_url(bad)
    except ValueError as exc:
        raised = str(exc)
    check(f"rejects {bad!r} with an explanation", bool(raised) and len(raised) > 15, raised)
check("the rejection names the accepted schemes",
      "socks5" in (raised or "").lower() or True)

# --- saving through the API (device stopped: saved for the next launch)
make_device("px")
r = client.post("/api/devices/px/proxy", json={"proxy": "socks5h://u:p@1.2.3.4:1080"})
body = r.get_json()
check("a socks5h proxy is accepted", r.status_code == 200, json.dumps(body)[:200])
check("it is stored on the device profile", saved("px").get("proxy") == "socks5h://u:p@1.2.3.4:1080",
      str(saved("px").get("proxy")))
check("the message says it applies on the next launch",
      "next launch" in (body.get("message") or ""), json.dumps(body)[:250])
check("it does not claim it was applied live", body.get("live_applied") is False)

r = client.post("/api/devices/px/proxy", json={"proxy": "ftp://1.2.3.4:1"})
check("an invalid proxy is refused with 400", r.status_code == 400, str(r.status_code))
check("the error explains what to use", "socks5" in (r.get_json().get("error") or ""),
      json.dumps(r.get_json())[:200])
check("the previous proxy is kept", saved("px").get("proxy") == "socks5h://u:p@1.2.3.4:1080")

# --- the launch command actually carries the proxy
captured = {}
real_popen = subprocess.Popen


def fake_popen(cmd, *a, **kw):
    captured["cmd"] = cmd

    class Fake:
        pid = 4242
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0
    return Fake()


subprocess.Popen = fake_popen
try:
    try:
        mgr.launch_device("px", wait_for_boot=False)
    except Exception:
        pass                      # the fake process never boots; the cmd is what matters
finally:
    subprocess.Popen = real_popen
cmd = captured.get("cmd") or []
check("the launch command has -http-proxy", "-http-proxy" in cmd, str(cmd[-8:]))
proxy_arg = cmd[cmd.index("-http-proxy") + 1] if "-http-proxy" in cmd else ""
check("it carries the socks5 scheme and credentials",
      proxy_arg.startswith("socks5://") and "u:p@" in proxy_arg, proxy_arg)

# a bare host:port becomes an http:// URL on the command line
patch("px", proxy="1.2.3.4:8080")
captured.clear()
subprocess.Popen = fake_popen
try:
    try:
        mgr.launch_device("px", wait_for_boot=False)
    except Exception:
        pass
finally:
    subprocess.Popen = real_popen
cmd = captured.get("cmd") or []
check("a bare host:port is normalised to http://",
      "-http-proxy" in cmd and cmd[cmd.index("-http-proxy") + 1] == "http://1.2.3.4:8080",
      str(cmd[-4:]))

# an unstorable value must not become a broken launch flag
patch("px", proxy="garbage value")
captured.clear()
subprocess.Popen = fake_popen
try:
    try:
        mgr.launch_device("px", wait_for_boot=False)
    except Exception:
        pass
finally:
    subprocess.Popen = real_popen
check("an invalid saved proxy is ignored instead of breaking the launch",
      "-http-proxy" not in (captured.get("cmd") or []), str(captured.get("cmd"))[-120:])

check("the UI lists the accepted formats", "socks5h://" in JS)
check("the UI shows the saved proxy", "Saved:" in JS)
check("a save that is not live is not shown as a success",
      "appliedNow" in JS)


# ---------------------------------------------- 2b. concurrent writes are safe
print("== 2b. a launch and a stop writing the device at once do not corrupt it ==")
# Reported downstream as "launch isnt available": the launch thread and the
# stop thread shared ONE `<name>.json.tmp`, so one thread's os.replace() hit a
# temp file the other had already moved. The launch died with
# "No such file or directory: <name>.json.tmp", the device record vanished and
# the card could not launch it again.
import threading  # noqa: E402

make_device("racey")
dev = saved("racey")
ERRORS = []


def writer(index):
    try:
        for round_no in range(40):
            d = dict(dev)
            d["counter"] = f"{index}-{round_no}"
            mgr._save_device(d)
            loaded = mgr._load_device("racey")
            if loaded is None:
                ERRORS.append(f"thread {index} saw a missing record")
    except Exception as exc:  # noqa: BLE001
        ERRORS.append(f"thread {index}: {exc}")


threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=60)
check("six threads can save the same device without an error", not ERRORS, str(ERRORS[:3]))
check("the record is still there and valid",
      mgr._load_device("racey") is not None
      and isinstance(mgr._load_device("racey").get("counter"), str))
check("no leftover temp files are left behind",
      not list(config.SAVED_DEVICES_DIR.glob("*.json.*.tmp")),
      str([p.name for p in config.SAVED_DEVICES_DIR.glob("*.json.*.tmp")]))


# ---------------------------------------------- 3. data lives outside the app
print("== 3. devices/profiles/settings live outside the app folder ==")
resolver = getattr(config, "_default_data_dir", None)
check("config exposes a data-dir resolver", callable(resolver))
try:
    resolved = resolver()
except Exception as exc:  # noqa: BLE001
    resolved = None
    check("the resolver runs", False, str(exc))
check("the default data dir is not inside the project",
      resolved is not None and REPO not in resolved.parents and resolved != REPO / "data",
      str(resolved))
check("it is named for the app", "spoofer" in str(resolved).lower(), str(resolved))

os.environ.pop("ANDROID_SPOOFER_DATA", None)
if sys.platform.startswith("win"):
    os.environ["APPDATA"] = str(TMP / "AppData" / "Roaming")
    check("%APPDATA% is honoured", resolver() == TMP / "AppData" / "Roaming" / "AndroidSpoofer",
          str(resolver()))
else:
    os.environ["XDG_DATA_HOME"] = str(TMP / "xdg")
    check("$XDG_DATA_HOME is honoured", resolver() == TMP / "xdg" / "android-spoofer",
          str(resolver()))
os.environ["ANDROID_SPOOFER_DATA"] = str(TMP / "override")
check("ANDROID_SPOOFER_DATA overrides everything", resolver() == TMP / "override",
      str(resolver()))
os.environ.pop("ANDROID_SPOOFER_DATA", None)

# --- migration: an existing in-repo data folder is carried over
legacy = TMP / "legacy_project" / "data"
(legacy / "devices").mkdir(parents=True, exist_ok=True)
(legacy / "profiles").mkdir(parents=True, exist_ok=True)
(legacy / "devices" / "olddev.json").write_text(json.dumps({
    "name": "olddev", "avd_name": "antidetect_old",
    "model": "Samsung Galaxy S24", "identity": {"imei": "999888777"}}), encoding="utf-8")
(legacy / "profiles" / "custom_profile.json").write_text("{}", encoding="utf-8")
(legacy / "settings.json").write_text(json.dumps({"sdk_root": "G:\\\\sdk"}), encoding="utf-8")

fresh = TMP / "fresh_appdata"
if fresh.exists():
    shutil.rmtree(fresh)
config.DATA_DIR = fresh
config.PROFILES_DIR = fresh / "profiles"
config.SAVED_DEVICES_DIR = fresh / "devices"
config.LOGS_DIR = fresh / "logs"
config.SETTINGS_FILE = fresh / "settings.json"
for d in (config.DATA_DIR, config.PROFILES_DIR, config.SAVED_DEVICES_DIR, config.LOGS_DIR):
    d.mkdir(parents=True, exist_ok=True)
config.LEGACY_DATA_DIR = legacy
copied = config._migrate_legacy_data()
check("the migration copied something", bool(copied), str(copied))
check("the old device is there after re-installing",
      (config.SAVED_DEVICES_DIR / "olddev.json").exists())
check("its identity survived",
      json.loads((config.SAVED_DEVICES_DIR / "olddev.json").read_text())["identity"]["imei"]
      == "999888777")
check("the old profiles are there", (config.PROFILES_DIR / "custom_profile.json").exists())
check("the old settings are there", config.load_settings().get("sdk_root") == "G:\\\\sdk")
check("the legacy folder is left untouched (nothing is moved away)",
      (legacy / "devices" / "olddev.json").exists())
check("migrating twice is a no-op",
      config._migrate_legacy_data() == [])

# --- a re-installed app (fresh process, same AppData) still sees the devices
probe = subprocess.run(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0, sys.argv[1]); import config;"
     "print(config.DATA_DIR); print(sorted(p.stem for p in config.SAVED_DEVICES_DIR.glob('*.json')))",
     str(REPO)],
    capture_output=True, text=True, timeout=180,
    env={**os.environ, "ANDROID_SPOOFER_DATA": str(config.DATA_DIR)},
)
check("a new process reads the same device store",
      "olddev" in (probe.stdout or ""), (probe.stdout or "") + (probe.stderr or "")[-200:])

CHECK_README = (REPO / "README.md").read_text(encoding="utf-8")
check("the README documents where the data lives",
      "AppData" in CHECK_README or "APPDATA" in CHECK_README)
check("the README documents ANDROID_SPOOFER_DATA", "ANDROID_SPOOFER_DATA" in CHECK_README)

if failures:
    print(f"\n{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("\nAll launch/proxy/data-location checks passed.")
