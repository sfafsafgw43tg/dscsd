#!/usr/bin/env python3
"""Proxy on the card: visible, validated live, applied and turned off for real.

Reported: *"we have a problem with the proxy, it says that url is invalid while
its valid - remake the proxy UI, type in and make it visible here in the menu up
to the device, and when I change it make it change realtime and when I turn it
off make it turned off"* plus a design reference.

Before this, the proxy lived only inside **Details**, was a free-text box with a
Save button, and any string the strict parser disliked was answered with a
generic "invalid". Nothing on the card showed whether a proxy was configured,
and "saved" and "applied on the emulator" looked identical.

Pin the new behaviour:

1. a proxy panel is rendered **on the card** (not only in Details), with a
   switch, a scheme chip, the value, and Apply;
2. the switch state is honest: Off / Saved / Live, driven by `proxy_live` from
   the server, never guessed;
3. validation is live, explains itself, and **defers to the server** - a value
   the backend accepts is never shown as invalid (the reported bug);
4. changing it on a running device applies it immediately and the card says so;
5. turning it off really clears it (profile + running emulator);
6. the accepted formats stay permissive (spaces, socks5h, credentials, IPv6).
"""
import json
import os
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

client = ga.create_app().test_client()
JS = (REPO / "gui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
CSS = (REPO / "gui" / "static" / "css" / "style.css").read_text(encoding="utf-8")

client.post("/api/devices", json={
    "model": "Google Pixel 8", "android_version": "15", "name": "pxuidev"})


def card():
    return next(d for d in client.get("/api/devices").get_json()
                if d["name"] == "pxuidev")


# ---------------------------------------------------- 1. validation never lies
print("== 1. a valid URL is never called invalid ==")
pm = ProxyManager()
valid_forms = [
    "1.2.3.4:8080", "http://1.2.3.4:8080", "https://proxy.example.com:443",
    "socks5://1.2.3.4:1080", "socks5h://u:p@host:1080", "socks://host:1080",
    "socks4://host:1080", "u:p@host:8080", "http://[::1]:8080",
    "1.2.3.4 : 8080", "http:// 1.2.3.4:8080", "host.example.net:8000",
]
for raw in valid_forms:
    report = pm.parse_report(raw)
    check(f"parse_report accepts {raw!r}", report.get("valid") is True,
          json.dumps(report)[:160])
    check(f"the API agrees about {raw!r}",
          client.post("/api/proxy/parse", json={"proxy": raw}).get_json().get("valid") is True)

bad_forms = ["ftp://host:1", "socks9://host:1080", "not a proxy", "http://host:99999"]
for raw in bad_forms:
    report = client.post("/api/proxy/parse", json={"proxy": raw}).get_json()
    check(f"parse rejects {raw!r}", report.get("valid") is False, json.dumps(report)[:120])
    check(f"the rejection explains itself for {raw!r}",
          len(str(report.get("error") or "")) > 20, str(report.get("error")))
check("an empty value is not an error, just empty",
      client.post("/api/proxy/parse", json={"proxy": ""}).get_json().get("empty") is True)
check("the parser normalises to scheme://host:port",
      pm.parse_report("socks5h://u:p@h:1080")["normalised"] == "socks5://u:p@h:1080",
      str(pm.parse_report("socks5h://u:p@h:1080")))

# ------------------------------------------- 2. the card reports the real state
print("== 2. the card knows Off / Saved / Live ==")
dev = card()
check("a fresh device has no proxy", dev.get("proxy") is None)
check("and it is not live", not dev.get("proxy_live"))

r = client.post("/api/devices/pxuidev/proxy", json={"proxy": "socks5://u:p@1.2.3.4:1080"})
body = r.get_json()
check("saving on a stopped device succeeds", body.get("success") is True, json.dumps(body)[:200])
check("it is stored", card().get("proxy") == "socks5://u:p@1.2.3.4:1080")
check("it is NOT claimed live on a stopped device", card().get("proxy_live") is False,
      str(card().get("proxy_live")))
check("the normalised form is stored for display",
      card().get("proxy_normalised") == "socks5://u:p@1.2.3.4:1080",
      str(card().get("proxy_normalised")))
check("the message says it waits for a launch",
      "next launch" in (body.get("message") or "").lower(), body.get("message"))

# --------------------------------------------------- 3. live apply on a running device
print("== 3. changing it on a running device applies it immediately ==")
client.post("/api/devices/pxuidev/launch", json={"frida": False})
deadline = time.time() + 90
state = {}
while time.time() < deadline:
    state = client.get("/api/devices/pxuidev/status").get_json()
    if state.get("state") in ("running", "error"):
        break
    time.sleep(0.25)
check("device launched", state.get("state") == "running", json.dumps(state)[:200])

r = client.post("/api/devices/pxuidev/proxy", json={"proxy": "http://1.2.3.4:8080"})
body = r.get_json()
check("an HTTP proxy applies live", body.get("live_applied") is True, json.dumps(body)[:200])
check("the card flips to live", card().get("proxy_live") is True)
check("the message names the serial", "emulator-" in (body.get("message") or ""),
      body.get("message"))

r = client.post("/api/devices/pxuidev/proxy", json={"proxy": "socks5://u:p@1.2.3.4:1080"})
check("a SOCKS proxy applies live too", r.get_json().get("live_applied") is True,
      json.dumps(r.get_json())[:200])

# --------------------------------------------------- 4. turning it off really turns it off
print("== 4. turning it off clears it everywhere ==")
r = client.post("/api/devices/pxuidev/proxy", json={"proxy": ""})
body = r.get_json()
check("turning it off succeeds", body.get("success") is True, json.dumps(body)[:200])
check("the message says it was turned off",
      "turned off" in (body.get("message") or "").lower(), body.get("message"))
check("the profile no longer has a proxy", card().get("proxy") is None)
check("the card is not 'live' any more", not card().get("proxy_live"))
check("the normalised form is dropped", card().get("proxy_normalised") is None)
check("no stale proxy error is left on the card", not card().get("proxy_error"))

# --------------------------------------------------- 5. the UI itself
print("== 5. the panel is on the card, not buried in Details ==")
render_fn = JS[JS.index("function buildProxyPanel"):JS.index("function buildSpoofTag")]
check("a proxy panel builder exists", "buildProxyPanel" in JS)
check("the card renders it", "${buildProxyPanel(device" in JS)
check("it has a real switch", 'role="switch"' in render_fn and "aria-checked" in render_fn)
check("it has a text field for typing", 'data-proxy-input' in render_fn)
check("it has an Apply button", 'data-proxy-apply' in render_fn)
check("it shows Off / Saved / Live", "'Live'" in render_fn and "'Saved'" in render_fn
      and "'Off'" in render_fn)
check("the card has a status chip too", "Proxy live" in JS and "Proxy saved" in JS)
check("typing is preserved across re-renders", "proxyDrafts" in JS)
check("the toggle is wired", "toggleProxy" in JS and "data-proxy-toggle" in JS)
check("Enter applies, Escape leaves the field",
      "e.key === 'Enter'" in JS and "e.key === 'Escape'" in JS)
check("a copy button exists", "data-proxy-copy" in render_fn)
check("the panel has styles", ".proxy-panel" in CSS and ".proxy-toggle" in CSS
      and ".proxy-input" in CSS)
check("invalid input is styled as an error", ".proxy-hint.is-invalid" in CSS)
check("live/saved dots have distinct colours",
      ".proxy-dot.live" in CSS and ".proxy-dot.saved" in CSS)

# client-side validation must defer to the server (the reported bug)
check("the UI asks the server before refusing a value", "/api/proxy/parse" in JS)
check("a server-accepted value is applied even if the local guess failed",
      "if (!server || !server.valid)" in JS)
check("the local parser accepts socks5h/socks aliases", "socks5h" in JS)
check("feedback shows what was parsed", "proxy-hint-normal" in JS)

# the old Details box still exists (it must not contradict the card)
check("Details still has a proxy control", "detailsProxyInput" in JS)

# --------------------------------------------------- 6. jsdom render (real markup)
print("== 6. the rendered card shows the right state ==")
try:
    import shutil
    import subprocess

    NODE = shutil.which("node")
    JSDOM = Path("/tmp/jsd/node_modules/jsdom")
    if not (NODE and JSDOM.exists()):
        raise RuntimeError("jsdom not available")
    probe = TMP / "render_proxy.js"
    probe.write_text(f"""
const {{ JSDOM }} = require('{JSDOM}');
const fs = require('fs');
const dom = new JSDOM(`<!DOCTYPE html><html><body><div id="devicesGrid"></div>
<div id="deviceCountBadge"></div><div id="totalDevices"></div><div id="runningDevices"></div>
<div id="protectedDevices"></div>
<div id="spoofStatus"><span class="status-indicator"></span><span class="status-text"></span></div>
</body></html>`, {{ runScripts: 'outside-only', url: 'http://localhost/' }});
const win = dom.window;
win.__d = [];
win.fetch = async (u) => ({{ ok: true, json: async () => (String(u).includes('/api/devices') ? win.__d : {{}}) }});
win.setInterval = () => 1; win.clearInterval = () => {{}}; win.io = undefined;
win.confirm = () => false; win.localStorage = {{ getItem: () => null, setItem: () => {{}} }};
win.eval(fs.readFileSync('{REPO}/gui/static/js/app.js', 'utf8'));
const base = {{ name: 'd', model: 'M', brand: 'Google', android_version: '15',
  console_port: 5554, rootable: true, rooted: true, camera: {{}} }};
(async () => {{
  for (const [label, extra] of Object.entries({{
    off:   {{ running: true }},
    saved: {{ running: false, proxy: 'socks5://u:p@1.2.3.4:1080' }},
    live:  {{ running: true, proxy: 'http://1.2.3.4:8080', proxy_live: true }},
  }})) {{
    win.__d = [Object.assign({{}}, base, extra)];
    await win.eval('loadDevices()');
    const g = win.document.getElementById('devicesGrid');
    const p = g.querySelector('.proxy-panel');
    const st = p && p.querySelector('.proxy-state');
    const inp = p && p.querySelector('.proxy-input');
    console.log(JSON.stringify({{
      label,
      panel: p ? p.className : null,
      state: st ? st.textContent.trim() : null,
      input: inp ? inp.value : null,
      toggleOn: p ? p.querySelector('.proxy-toggle').getAttribute('aria-checked') : null,
    }}));
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
    check("jsdom rendered all three states", len(rows) == 3, str(rows)[:200])
    check("off: panel is off and the field is hidden",
          rows.get("off", {}).get("panel", "").endswith("is-off")
          and rows.get("off", {}).get("input") is None, str(rows.get("off")))
    check("off: the switch reports off", rows.get("off", {}).get("toggleOn") == "false")
    check("saved: chip says Saved", rows.get("saved", {}).get("state") == "Saved",
          str(rows.get("saved")))
    check("saved: the field shows the stored proxy",
          rows.get("saved", {}).get("input") == "socks5://u:p@1.2.3.4:1080",
          str(rows.get("saved")))
    check("live: chip says Live", rows.get("live", {}).get("state") == "Live",
          str(rows.get("live")))
    check("live: the switch reports on", rows.get("live", {}).get("toggleOn") == "true")
except Exception as exc:  # noqa: BLE001
    check("the card can be rendered for verification", False, str(exc))

client.post("/api/devices/pxuidev/stop", json={"force": True})

if failures:
    print(f"\n{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("\nAll proxy-UI / live-apply checks passed.")
