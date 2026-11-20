/**
 * AntiDetect Emulator - Frontend JavaScript
 */

// Socket.IO connection (the CDN may be unreachable in an offline desktop
// app; the UI also polls, so a no-op stub keeps everything working).
const socket = (typeof io !== 'undefined')
    ? io()
    : { on: () => {}, emit: () => {} };

// State
let devices = [];
let models = {};
let deviceSearchQuery = '';
let deviceStatusFilter = 'all'; // 'all' | 'running' | 'stopped'

// Per-device in-flight actions to prevent accidental double-clicks:
// name -> 'launching' | 'spoofing' | 'hooking' | 'stopping' | 'deleting'
const busyActions = new Map();

// Camera defaults saved via Settings; prefill for the Create Device form.
let cameraDefaults = { front: 'emulated', back: 'virtualscene' };

// Auto-root default saved via Settings; prefill for the Create Device form.
let autoRootDefault = true;

// Where the last card press started ({action, name, at}). The grid re-renders
// on every poll, so a click can otherwise be lost between mousedown and
// mouseup - which reads as "the button does nothing".
let pressedAction = null;

// Single dispatcher for every button/tag on a device card.
function runCardAction(action, name) {
    if (!action || !name) return;
    if (action === 'launch') launchDevice(name);
    else if (action === 'stop') stopDevice(name);
    else if (action === 'spoof') applyAntidetect(name);
    else if (action === 'hooks') applyFridaHooks(name);
    else if (action === 'restore') restoreDeviceState(name);
    else if (action === 'recover') recoverDevice(name);
    else if (action === 'details') showDeviceDetails(name);
    else if (action === 'delete') deleteDevice(name);
}

// Safe HTML & attribute escaping helpers
function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function escapeAttr(value) {
    return escapeHtml(value);
}

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    loadModels();
    refreshStatus();  // Load devices + ADB + Frida status

    // Socket events
    socket.on('connect', () => console.log('Connected to server'));
    socket.on('devices_updated', (data) => {
        devices = data;
        renderDevices();
    });
    socket.on('restore_finished', (payload) => {
        if (payload && payload.device) {
            showToast(
                payload.success
                    ? `Restored ${payload.device}: ${payload.message}`
                    : `Could not restore ${payload.device}: ${payload.message}`,
                payload.success ? 'success' : 'warning'
            );
            loadDevices();
        }
    });
    socket.on('hooks_updated', (payload) => {
        if (payload && payload.name) {
            if (payload.applied) {
                showToast(`Frida hooks active on ${payload.name}`, 'success');
            } else if (payload.error) {
                showToast(`Frida hooks failed on ${payload.name}: ${payload.error}`, 'warning');
            }
        }
        refreshStatus();
    });

    // Event delegation for device card buttons & clickable tags (safe for any device name)
    const grid = document.getElementById('devicesGrid');
    if (grid) {
        grid.addEventListener('click', (e) => {
            // The grid re-renders every 10s *and* on every action, which
            // replaces the buttons under the pointer. A click whose mousedown
            // landed on the old node and whose mouseup lands on the new one
            // fires on the grid itself - losing the action entirely, which
            // looks exactly like "I click Launch and nothing happens".
            // Recover the action from where the press started.
            if (!e.target.closest('[data-action]') && pressedAction) {
                const pressed = pressedAction;
                pressedAction = null;
                if (pressed.at > Date.now() - 1500) {
                    runCardAction(pressed.action, pressed.name);
                }
                return;
            }
            // A disabled button swallows its own clicks, but the card is the
            // only thing the user sees - so explain the no-op instead of
            // leaving a dead-looking button. (This was the "Launch does
            // nothing" report.)
            const dead = e.target.closest('.btn[aria-disabled="true"], .btn[disabled]');
            if (dead) {
                const action = dead.dataset.action;
                const who = dead.dataset.device || 'this device';
                showToast(
                    action === 'launch'
                        ? `Launch isn't available for ${who} right now — see Details for the reason.`
                        : `That action is busy for ${who} — wait for the current step to finish.`,
                    'warning'
                );
                return;
            }
            const target = e.target.closest('[data-action]');
            if (!target) return;
            runCardAction(target.dataset.action, target.dataset.device);
        });

        // Remember where a press started so a re-render between mousedown and
        // mouseup cannot swallow the click.
        grid.addEventListener('pointerdown', (e) => {
            const pressed = e.target.closest('[data-action]');
            pressedAction = pressed
                ? { action: pressed.dataset.action, name: pressed.dataset.device, at: Date.now() }
                : null;
        }, true);
    }

    // Event delegation for Details modal actions (Copy, Cameras, Proxy, Hook app, Regenerate, Crash log)
    const detailsEl = document.getElementById('deviceDetails');
    if (detailsEl) {
        detailsEl.addEventListener('click', (e) => {
            const copyBtn = e.target.closest('[data-copy-value]');
            if (copyBtn) {
                copyText(copyBtn.dataset.copyValue, copyBtn.dataset.copyLabel || 'Value');
                return;
            }
            const copyLogBtn = e.target.closest('[data-copy-log]');
            if (copyLogBtn) {
                const pre = document.getElementById('detailsLogPre');
                if (pre) copyText(pre.textContent || '', 'Emulator log');
                return;
            }
            const saveCamBtn = e.target.closest('[data-save-cameras]');
            if (saveCamBtn) {
                saveCameras(saveCamBtn.dataset.saveCameras);
                return;
            }
            const presetBtn = e.target.closest('[data-camera-preset]');
            if (presetBtn) {
                applyCameraPreset(presetBtn.dataset.cameraPreset);
                return;
            }
            const saveProxyBtn = e.target.closest('[data-save-proxy]');
            if (saveProxyBtn) {
                saveProxy(saveProxyBtn.dataset.saveProxy, false);
                return;
            }
            const clearProxyBtn = e.target.closest('[data-clear-proxy]');
            if (clearProxyBtn) {
                saveProxy(clearProxyBtn.dataset.clearProxy, true);
                return;
            }
            const regenBtn = e.target.closest('[data-regen-identity]');
            if (regenBtn) {
                regenerateIdentity(regenBtn.dataset.regenIdentity);
                return;
            }
            const hookPkgBtn = e.target.closest('[data-hook-package]');
            if (hookPkgBtn) {
                hookCustomPackage(hookPkgBtn.dataset.hookPackage);
                return;
            }
            const crashLogBtn = e.target.closest('[data-load-crash-log]');
            if (crashLogBtn) {
                loadCrashLog(crashLogBtn.dataset.loadCrashLog);
            }
            const airplaneBtn = e.target.closest('[data-airplane]');
            if (airplaneBtn) {
                setAirplane(airplaneBtn.dataset.device, airplaneBtn.dataset.airplane)
                    .catch(() => showToast('Could not change airplane mode', 'error'));
            }
        });
    }

    // Auto-refresh status & device state every 10 seconds
    setInterval(refreshStatus, 10000);
});

async function copyText(text, label = 'Value') {
    if (!text || text === 'N/A') {
        showToast(`No ${label.toLowerCase()} to copy`, 'info');
        return;
    }
    try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(text);
        } else {
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            ta.remove();
        }
        showToast(`${label} copied to clipboard`, 'success');
    } catch (err) {
        showToast(`Could not copy ${label}`, 'error');
    }
}

// =========================================
// ANTIDETECT STATUS FUNCTIONS
// =========================================

async function refreshStatus() {
    await Promise.all([
        loadDevices(),
        checkAdbStatus(),
        checkFridaStatus()
    ]);
}

async function manualRefresh() {
    const btn = document.getElementById('headerRefreshBtn');
    if (btn) {
        btn.disabled = true;
        btn.classList.add('spin');
    }
    try {
        await refreshStatus();
        showToast('Status refreshed', 'info');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.classList.remove('spin');
        }
    }
}

async function checkAdbStatus() {
    try {
        const response = await fetch('/api/status/adb');
        const data = await response.json();

        const card = document.getElementById('adbStatus');
        const indicator = card.querySelector('.status-indicator');
        const text = card.querySelector('.status-text');

        if (data.connected) {
            indicator.className = 'status-indicator connected';
            text.textContent = `Connected (${data.devices.length} device${data.devices.length > 1 ? 's' : ''})`;
        } else {
            indicator.className = 'status-indicator disconnected';
            text.textContent = data.error || 'No devices connected';
        }
    } catch (error) {
        console.error('ADB status check failed:', error);
    }
}

function currentDeviceSerial() {
    // Prefer a running device, then any device that already knows its serial.
    const ordered = [...devices].sort((a, b) => (b.running ? 1 : 0) - (a.running ? 1 : 0));
    for (const device of ordered) {
        if (device.serial) return device.serial;
    }
    return null;
}

async function checkFridaStatus() {
    try {
        const serial = currentDeviceSerial();
        const url = serial
            ? `/api/status/frida?serial=${encodeURIComponent(serial)}`
            : '/api/status/frida';
        const response = await fetch(url);
        const data = await response.json();

        const card = document.getElementById('fridaStatus');
        const indicator = card.querySelector('.status-indicator');
        const text = card.querySelector('.status-text');
        const installBtn = document.getElementById('fridaInstallBtn');
        const fileBtn = document.getElementById('fridaFileBtn');

        if (!data.serial) {
            indicator.className = 'status-indicator disconnected';
            text.textContent = 'No device (start an emulator)';
            if (!fridaInstallInFlight) {
                installBtn.style.display = 'inline-flex';
                installBtn.textContent = 'Install';
                fileBtn.style.display = 'inline-flex';
            }
            return;
        }

        if (fridaInstallInFlight) {
            return;  // installFrida() owns the button until it resolves
        }

        if (data.running) {
            const ver = data.remote_version || data.version || data.server_version;
            indicator.className = 'status-indicator connected';
            text.textContent = `Running on ${data.serial}${ver ? ` (v${ver})` : ''}`;
            installBtn.style.display = 'none';
            fileBtn.style.display = 'none';
        } else if (data.installed) {
            indicator.className = 'status-indicator warning';
            text.textContent = `Installed on ${data.serial} (not running)`;
            installBtn.style.display = 'inline-flex';
            installBtn.textContent = 'Start Frida';
            fileBtn.style.display = 'none';
        } else if (data.downloaded) {
            indicator.className = 'status-indicator warning';
            text.textContent = 'Downloaded (not installed)';
            installBtn.style.display = 'inline-flex';
            installBtn.textContent = 'Install Frida';
            fileBtn.style.display = 'inline-flex';
        } else {
            indicator.className = 'status-indicator disconnected';
            text.textContent = `Not installed (${data.serial})`;
            installBtn.style.display = 'inline-flex';
            installBtn.textContent = 'Download & Install';
            fileBtn.style.display = 'inline-flex';
        }

        if (data.last_error) {
            installBtn.title = data.last_error;
        }
    } catch (error) {
        console.error('Frida status check failed:', error);
    }
}

let fridaInstallInFlight = false;

async function installFrida(localPath) {
    const btn = document.getElementById('fridaInstallBtn');
    const fileBtn = document.getElementById('fridaFileBtn');
    if (fridaInstallInFlight) return;   // double-click guard
    fridaInstallInFlight = true;
    btn.disabled = true;
    if (fileBtn) fileBtn.disabled = true;
    btn.textContent = 'Installing…';

    const payload = {};
    const serial = currentDeviceSerial();
    if (serial) payload.serial = serial;
    if (localPath) payload.local_path = localPath;

    try {
        const response = await fetch('/api/frida/install', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await response.json();
        if (!data.success) {
            showToast(data.error || 'Installation failed to start', 'error');
            fridaInstallInFlight = false;
            btn.disabled = false;
            if (fileBtn) fileBtn.disabled = false;
            await checkFridaStatus();
            return;
        }
        showToast('Installing frida-server on the device…', 'info');
        await pollFridaInstall();
    } catch (error) {
        showToast('Installation failed: ' + error, 'error');
    } finally {
        fridaInstallInFlight = false;
        btn.disabled = false;
        if (fileBtn) fileBtn.disabled = false;
        await checkFridaStatus();
    }
}

async function pollFridaInstall() {
    for (let attempt = 0; attempt < 240; attempt++) {  // ~6 minutes
        await new Promise(resolve => setTimeout(resolve, 1500));
        let status;
        try {
            const response = await fetch('/api/frida/install/status');
            status = await response.json();
        } catch (error) {
            continue;
        }
        if (status.running) continue;

        if (status.ok) {
            showToast('Frida server installed and running', 'success');
        } else {
            const message = status.error || 'Installation failed';
            showToast(message, 'error');
            console.error('frida install log:\n' + (status.log || ''));
            if (status.diagnostics) console.error('frida diagnostics:', status.diagnostics);
        }
        return;
    }
    showToast('Frida installation is taking unusually long - check the terminal output', 'error');
}

async function installFridaFromFile() {
    try {
        const response = await fetch('/api/frida/pick-server', { method: 'POST' });
        const data = await response.json();
        if (!data.success || !data.path) {
            showToast(data.error || 'No file selected', 'error');
            return;
        }
        await installFrida(data.path);
    } catch (error) {
        showToast('Could not open the file picker', 'error');
    }
}

async function applyAntidetect(name) {
    if (busyActions.has(name)) return;
    busyActions.set(name, 'spoofing');
    renderDevices();
    showToast(`Applying antidetect to ${name}…`, 'info');

    try {
        const response = await fetch(`/api/devices/${encodeURIComponent(name)}/apply-antidetect`, {
            method: 'POST'
        });
        const data = await response.json();

        if (data.success) {
            const report = data.spoof_report || {};
            const failed = report.failed || [];
            const verifiedCount = Object.keys(report.verified || {}).length;
            const totalCount = verifiedCount + failed.length;
            showToast(data.message || 'Antidetect applied!', failed.length ? 'warning' : 'success');

            // Update spoofing status immediately
            const card = document.getElementById('spoofStatus');
            const indicator = card.querySelector('.status-indicator');
            const text = card.querySelector('.status-text');
            if (failed.length) {
                indicator.className = 'status-indicator warning';
                text.textContent = `Incomplete (${verifiedCount}/${totalCount} props)`;
            } else {
                indicator.className = 'status-indicator connected';
                text.textContent = totalCount
                    ? `Verified (${verifiedCount}/${totalCount} props)`
                    : 'Applied to ' + name + (data.serial ? ' (' + data.serial + ')' : '');
            }
            await loadDevices();
        } else {
            showToast(data.error || data.message || 'Failed to apply antidetect', 'error');
        }
    } catch (error) {
        showToast('Failed to apply antidetect', 'error');
    } finally {
        busyActions.delete(name);
        renderDevices();
    }
}

async function restoreDeviceState(name) {
    if (busyActions.has(name)) return;
    busyActions.set(name, 'restoring');
    renderDevices();
    showToast(`Restoring saved spoofing + hooks for ${name}…`, 'info');

    try {
        const response = await fetch(`/api/devices/${encodeURIComponent(name)}/restore`, {
            method: 'POST'
        });
        const data = await response.json();
        if (data.success) {
            showToast(data.message || `Restored ${name}`, 'success');
        } else {
            showToast(data.message || data.error || `Could not restore ${name}`, 'warning');
        }
        await loadDevices();
        await refreshStatus();
    } catch (error) {
        showToast('Restore failed', 'error');
    } finally {
        busyActions.delete(name);
        renderDevices();
    }
}

async function recoverDevice(name) {
    // Unattended self-heal: re-apply whatever this device asked to keep and
    // re-check the radio gate. Failures must be readable, never silent.
    if (busyActions.has(name)) return;
    busyActions.set(name, 'recovering');
    renderDevices();
    showToast(`Recovering spoof + hooks for ${name}…`, 'info');
    try {
        const response = await fetch(`/api/devices/${encodeURIComponent(name)}/autorecover`, {
            method: 'POST'
        });
        const data = await response.json();
        if (data.success) {
            const parts = [];
            if (data.spoofed) parts.push('spoof verified');
            if (data.hooks) parts.push('hooks attached');
            showToast(data.message || (parts.length
                ? `${name}: ${parts.join(', ')}`
                : `${name} already up to date`), 'success');
        } else {
            showToast(data.message || data.error || `Could not recover ${name}`, 'warning');
        }
        await loadDevices();
        await refreshStatus();
    } catch (error) {
        showToast('Recovery failed', 'error');
    } finally {
        busyActions.delete(name);
        renderDevices();
    }
}

async function setAirplane(name, enable) {
    try {
        // `enable` comes from a data attribute, so it is the STRING "on"/"off"/
        // "auto" - "off" is truthy, so it must be parsed, not coerced.
        const body = enable === 'auto'
            ? { auto: true }
            : { enable: enable === true || enable === 'on' || enable === 'true' || enable === 1 };
        const response = await fetch(`/api/devices/${encodeURIComponent(name)}/airplane`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const data = await response.json();
        showToast(data.message || data.error || 'Airplane mode updated',
                  data.success ? 'success' : 'error');
        await loadDevices();
        if (document.getElementById('detailsModal').classList.contains('active')) {
            await showDeviceDetails(name);
        }
    } catch (error) {
        showToast('Could not change airplane mode', 'error');
    }
}

async function setRestoreIntent(name, { spoof, hooks }) {
    try {
        const body = {};
        if (spoof !== undefined) body.spoof = spoof;
        if (hooks !== undefined) body.hooks = hooks;
        const response = await fetch(`/api/devices/${encodeURIComponent(name)}/restore-intent`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const data = await response.json();
        showToast(data.message || data.error || 'Saved', data.success ? 'success' : 'error');
        await loadDevices();
        return data.success;
    } catch (error) {
        showToast('Could not save the restore setting', 'error');
        return false;
    }
}

async function applyFridaHooks(name) {
    if (busyActions.has(name)) return;
    busyActions.set(name, 'hooking');
    renderDevices();
    showToast(`Applying Frida hooks to ${name}…`, 'info');

    try {
        const response = await fetch(`/api/devices/${encodeURIComponent(name)}/apply-frida-hooks`, {
            method: 'POST'
        });
        const data = await response.json();

        if (data.success) {
            showToast(data.message || 'Frida hooks applied!', 'success');
            await checkFridaStatus();
            await loadDevices();
        } else {
            showToast(data.error || data.message || 'Failed to apply hooks', 'error');
        }
    } catch (error) {
        showToast('Failed to apply Frida hooks', 'error');
    } finally {
        busyActions.delete(name);
        renderDevices();
    }
}

// =========================================
// API FUNCTIONS
// =========================================

async function loadDevices() {
    try {
        const response = await fetch('/api/devices');
        devices = await response.json();
        renderDevices();
        updateStats();
    } catch (error) {
        showToast('Failed to load devices', 'error');
    }
}

async function loadModels() {
    try {
        const response = await fetch('/api/models');
        models = await response.json();
        populateModelSelect();
    } catch (error) {
        console.error('Failed to load models:', error);
    }
}

function populateModelSelect() {
    const select = document.getElementById('phoneModel');
    select.innerHTML = '';

    for (const [brand, brandModels] of Object.entries(models)) {
        const group = document.createElement('optgroup');
        group.label = brand;

        brandModels.forEach(model => {
            const option = document.createElement('option');
            // The value is the exact profile name, never a fuzzy alias: the
            // backend resolves it 1:1, so "Pixel 8" can't become "Pixel 8 Pro".
            option.value = model.name;
            option.textContent = model.model
                ? `${model.name} (${model.model})`
                : model.name;
            option.dataset.modelCode = model.model || '';
            group.appendChild(option);
        });

        select.appendChild(group);
    }
}

function useWebcamForBoth() {
    document.getElementById('cameraFront').value = 'webcam0';
    document.getElementById('cameraBack').value = 'webcam0';
    showToast('Both cameras set to webcam0', 'info');
}

// Custom system images discovered in/imported into the SDK.
let customSystemImages = [];

async function loadSystemImages() {
    try {
        const res = await fetch('/api/system-images');
        const data = await res.json();
        customSystemImages = data.custom || [];

        const select = document.getElementById('imageVariant');
        if (!select) return;
        const previous = select.value;
        select.innerHTML = '';
        for (const b of (data.builtin || [])) {
            const option = document.createElement('option');
            option.value = b.variant;
            option.textContent = b.label;
            select.appendChild(option);
        }
        if (customSystemImages.length) {
            const divider = document.createElement('option');
            divider.disabled = true;
            divider.textContent = '── images found in the SDK ──';
            select.appendChild(divider);
            for (const c of customSystemImages) {
                const option = document.createElement('option');
                option.value = 'pkg:' + c.package;
                option.textContent = 'Custom: ' + c.label + (c.rootable ? '' : ' (no root)');
                select.appendChild(option);
            }
        }
        if ([...select.options].some(o => o.value === previous)) select.value = previous;
        updateImageVariantHint();
    } catch (e) {
        customSystemImages = [];
    }
}

function selectedCustomImage() {
    const select = document.getElementById('imageVariant');
    if (select && select.value.startsWith('pkg:')) {
        return customSystemImages.find(c => 'pkg:' + c.package === select.value) || null;
    }
    return null;
}

function updateImageVariantHint() {
    const select = document.getElementById('imageVariant');
    const hint = document.getElementById('imageVariantHint');
    if (!select || !hint) return;
    const autoRoot = document.getElementById('autoRoot');
    const autoRootHint = document.getElementById('autoRootHint');
    const versionSelect = document.getElementById('androidVersion');

    const custom = selectedCustomImage();
    if (custom) {
        if (custom.android_version && versionSelect &&
                [...versionSelect.options].some(o => o.value === custom.android_version)) {
            versionSelect.value = custom.android_version;
        }
        hint.innerHTML = 'Custom image: <code>' + escapeHtml(custom.package) + '</code> — the Android ' +
            'version follows this selection' + (custom.rootable ? '.' :
                '; <strong>⚠ no root</strong> (playstore tag).');
        if (autoRoot) {
            autoRoot.disabled = !custom.rootable;
            autoRoot.checked = custom.rootable ? autoRootDefault : false;
        }
        if (autoRootHint) {
            autoRootHint.innerHTML = custom.rootable
                ? 'Runs <code>adb root</code> as soon as the device boots.'
                : '⚠ This image tag refuses <code>adb root</code>.';
        }
    } else if (select.value === 'google_apis_playstore') {
        hint.innerHTML = '<strong>⚠ No root:</strong> frida-server and property spoofing ' +
            'will <em>not</em> work on this device. Pick "Google APIs" if you need Frida.';
        if (autoRoot) {
            autoRoot.checked = false;
            autoRoot.disabled = true;
        }
        if (autoRootHint) {
            autoRootHint.innerHTML = '⚠ Play Store images refuse <code>adb root</code> — ' +
                'automatic rooting is disabled for this image.';
        }
    } else {
        hint.innerHTML = 'Frida and property spoofing need <code>adb root</code>, which only ' +
            'the Google APIs (userdebug) image allows.';
        if (autoRoot) {
            autoRoot.disabled = false;
            autoRoot.checked = autoRootDefault;
        }
        if (autoRootHint) {
            autoRootHint.innerHTML = 'Runs <code>adb root</code> as soon as the device boots, ' +
                'so Frida hooks and property spoofing work without any manual step.';
        }
    }
}

async function loadAdbStatus() {
    const hint = document.getElementById('adbStatusHint');
    const log = document.getElementById('adbRepairLog');
    try {
        const response = await fetch('/api/adb/status');
        const data = await response.json();
        const version = (data.version || 'unknown').split('\n')[0];
        if (hint) hint.textContent = `adb: ${data.path} — ${version}`;
        if (log) log.textContent = data.listing || '';
    } catch (error) {
        if (hint) hint.textContent = 'Could not read adb status';
    }
}

async function runDoctor() {
    const btn = document.getElementById('doctorBtn');
    const box = document.getElementById('doctorResults');
    btn.disabled = true;
    btn.textContent = 'Checking…';
    box.innerHTML = '';
    try {
        const response = await fetch('/api/doctor');
        const data = await response.json();
        const icons = { ok: '✅', info: 'ℹ️', warn: '⚠️', error: '❌' };
        box.innerHTML = (data.checks || []).map(c => `
            <div class="doctor-row doctor-${escapeAttr(c.severity)}">
                <div class="doctor-title">${icons[c.severity] || ''} <strong>${escapeHtml(c.title)}</strong></div>
                <div class="doctor-detail">${escapeHtml(c.detail || '')}</div>
                ${c.fix ? `<div class="doctor-fix">→ ${escapeHtml(c.fix)}</div>` : ''}
            </div>`).join('');
        if (data.worst === 'error') {
            showToast('Health check found errors — see the Health check section', 'error');
        } else if (data.worst === 'warn') {
            showToast('Health check has warnings — see the Health check section', 'warning');
        } else {
            showToast('All health checks pass ✓', 'success');
        }
    } catch (error) {
        showToast('Health check failed to run', 'error');
    }
    btn.disabled = false;
    btn.textContent = 'Run doctor';
}

async function repairAdb() {
    const button = document.getElementById('repairAdbBtn');
    const log = document.getElementById('adbRepairLog');
    if (button) { button.disabled = true; button.textContent = 'Repairing…'; }
    if (log) log.textContent = 'Reconnecting…';
    try {
        const response = await fetch('/api/adb/repair', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });
        const data = await response.json();
        if (log) log.textContent = (data.devices || '') + '\n' + (data.detail || '');
        if (data.success) {
            showToast(`ADB connected to ${data.serial}`, 'success');
        } else {
            showToast(data.detail || `ADB could not recover the device (state: ${data.state})`, 'error');
        }
        await refreshStatus();
    } catch (error) {
        showToast('ADB repair failed: ' + error, 'error');
    } finally {
        if (button) { button.disabled = false; button.textContent = 'Repair ADB'; }
    }
}

async function createDevice() {
    const name = document.getElementById('deviceName').value.trim();
    const model = document.getElementById('phoneModel').value;
    const android = document.getElementById('androidVersion').value;
    const proxy = document.getElementById('proxyAddress').value.trim();
    const cameraFront = document.getElementById('cameraFront').value.trim();
    const cameraBack = document.getElementById('cameraBack').value.trim();
    const imageVariantEl = document.getElementById('imageVariant');
    const selectedImage = imageVariantEl ? imageVariantEl.value : undefined;
    const customImage = selectedImage && selectedImage.startsWith('pkg:')
        ? selectedImage.slice(4) : undefined;
    const imageVariant = customImage ? undefined : selectedImage;
    const autoRootEl = document.getElementById('autoRoot');
    const autoRoot = autoRootEl ? autoRootEl.checked : undefined;
    const submitBtn = document.getElementById('createDeviceSubmitBtn');

    if (!model) {
        showToast('Please select a phone model', 'error');
        return;
    }

    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = 'Creating…';
    }

    try {
        const response = await fetch('/api/devices', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name: name || undefined,
                model: model,
                android_version: android,
                proxy: proxy || undefined,
                camera_front: cameraFront || undefined,
                camera_back: cameraBack || undefined,
                image_variant: imageVariant || undefined,
                custom_image: customImage || undefined,
                auto_root: autoRoot
            })
        });

        const data = await response.json();

        if (data.success) {
            const resolved = data.model || (data.device && data.device.model);
            showToast(`Device created: ${resolved || 'ok'}`, 'success');
            if (data.model_note) {
                showToast(data.model_note, 'info');
            }
            if (data.warning) {
                showToast(data.warning, 'warning');
            }
            if (data.camera_warning) {
                showToast(data.camera_warning, 'warning');
            }
            hideCreateDialog();
            await loadDevices();

            // Clear form (cameras go back to the saved defaults)
            document.getElementById('deviceName').value = '';
            document.getElementById('proxyAddress').value = '';
            document.getElementById('cameraFront').value = cameraDefaults.front || 'emulated';
            document.getElementById('cameraBack').value = cameraDefaults.back || 'virtualscene';
        } else {
            showToast(data.error || 'Failed to create device', 'error');
        }
    } catch (error) {
        showToast('Failed to create device', 'error');
    } finally {
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = 'Create Device';
        }
    }
}

async function launchDevice(name, { retried = false } = {}) {
    if (busyActions.has(name)) {
        showToast(`${name} is already ${busyActions.get(name)} — hang on.`, 'info');
        return;
    }
    const current = devices.find(d => d.name === name);
    if (current && (current.running || current.starting)) {
        // Never swallow the click: say what the card is showing.
        showToast(
            current.starting
                ? `${name} is still starting — wait for it to finish booting.`
                : `${name} is already running.`,
            'info'
        );
        return;
    }
    busyActions.set(name, 'launching');
    renderDevices();

    try {
        const response = await fetch(`/api/devices/${encodeURIComponent(name)}/launch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ frida: true })
        });

        const data = await response.json();

        if (!data.success) {
            // e.g. "Device already running" because adb still lists a dead
            // emulator: show the reason and offer the fix instead of nothing.
            const message = data.error || 'Failed to launch device';
            showToast(message, 'error');
            await refreshStatus();
            return;
        }

        if (/already in progress/i.test(data.message || '')) {
            // The server refused to start a second chain. If the device is
            // really not running, that state is stale: re-ask once (the
            // server clears a chain that has no thread behind it) and, if it
            // still refuses, say so instead of pretending to launch.
            const alreadyRunning = await deviceIsRunning(name);
            if (!alreadyRunning && !retried) {
                busyActions.delete(name);
                renderDevices();
                await new Promise(r => setTimeout(r, 600));
                return launchDevice(name, { retried: true });
            }
            if (!alreadyRunning) {
                showToast(
                    `${name} didn't start: the server still thinks a launch is in ` +
                    `progress. Open Details → Diagnose, then stop the device and try again.`,
                    'error'
                );
                return;
            }
            // Otherwise it is already up - just follow it.
        }

        showToast(`Launching ${name}… waiting for boot & spoofing`, 'info');
        await loadDevices();
        pollLaunchStatus(name, 0);
    } catch (error) {
        showToast('Failed to launch device', 'error');
    } finally {
        busyActions.delete(name);
        renderDevices();
    }
}

// Ask the server whether this device's emulator is actually up right now.
async function deviceIsRunning(name) {
    try {
        const response = await fetch(`/api/devices/${encodeURIComponent(name)}/status`);
        const status = await response.json();
        return Boolean(status.running);
    } catch (error) {
        return false;
    }
}

// Poll the async launch state so the UI can report boot progress and the
// real emulator error instead of hanging on the launch request.
async function pollLaunchStatus(name, attempt) {
    const maxAttempts = 90;  // ~3 minutes at 2s intervals
    try {
        const response = await fetch(`/api/devices/${encodeURIComponent(name)}/status`);
        const status = await response.json();
        const chainState = status.launch_state || status.state;

        if (chainState === 'error') {
            console.error('Emulator log tail:\n' + (status.log_tail || ''));
            // The toast gets the *reason*, not a generic follow-up: "open
            // Details for the log tail" is useless when the toast already
            // says what went wrong.
            const raw = String(status.error || 'Unknown error');
            const firstLine = raw.split('\n').map(l => l.trim()).filter(Boolean)[0] || raw;
            showToast(`Launch failed: ${firstLine}`, 'error');
            loadDevices();
            return;
        }

        if (chainState === 'running') {
            if (status.hooks_in_flight && attempt < maxAttempts) {
                // Keep refreshing until the background Frida hook thread finishes
                loadDevices();
                setTimeout(() => pollLaunchStatus(name, attempt + 1), 2000);
                return;
            }
            if (status.hooks_applied === false && status.hooks_error) {
                showToast(`${name} is running (${status.serial || 'emulator'}), hooks warning: ${status.hooks_error}`, 'warning');
            } else {
                showToast(`${name} is running (${status.serial || 'emulator'})`, 'success');
            }
            refreshStatus();
            return;
        }

        if (attempt < maxAttempts) {
            setTimeout(() => pollLaunchStatus(name, attempt + 1), 2000);
        } else {
            showToast(`${name} is still booting — check Details if it never appears`, 'warning');
            loadDevices();
        }
    } catch (error) {
        if (attempt < maxAttempts) {
            setTimeout(() => pollLaunchStatus(name, attempt + 1), 2000);
        }
    }
}

async function stopDevice(name) {
    if (busyActions.has(name)) return;
    busyActions.set(name, 'stopping');
    renderDevices();

    try {
        const response = await fetch(`/api/devices/${encodeURIComponent(name)}/stop`, {
            method: 'POST'
        });

        const data = await response.json();

        if (data.success && data.still_running !== true) {
            showToast(`Stopped ${name}`, 'success');
            await refreshStatus();
            await confirmDeviceStopped(name);
        } else {
            // A lingering emulator must not look like nothing happened: the
            // error is actionable and a force stop usually fixes it.
            const message = data.error || data.message || 'Failed to stop device';
            showToast(message, 'error');
            if (confirm(`${message}\n\nForce stop (kill the emulator process tree and free its ports)?`)) {
                await forceStopDevice(name);
            } else {
                await refreshStatus();
            }
        }
    } catch (error) {
        showToast('Failed to stop device', 'error');
    } finally {
        busyActions.delete(name);
        renderDevices();
    }
}

// The server says "stopped" - make sure the card agrees. A real emulator can
// linger for a moment (adb is lazy, ports take a beat to free up), so wait for
// the device to actually report not-running instead of trusting one response.
async function confirmDeviceStopped(name, { attempts = 6, delay = 700 } = {}) {
    for (let i = 0; i < attempts; i++) {
        const running = await deviceIsRunning(name);
        if (!running) {
            if (i > 0) {
                showToast(`${name} is fully stopped.`, 'success');
                refreshStatus();
            }
            return true;
        }
        if (i === attempts - 1) {
            // Still up: say it plainly and leave the user a one-click retry
            // rather than a card that claims the device is stopped.
            showToast(
                `${name} is still running after the stop — click Stop again or ` +
                `run \`python main.py doctor\` to see which process holds it.`,
                'warning'
            );
            refreshStatus();
            return false;
        }
        await new Promise(r => setTimeout(r, delay));
    }
    return false;
}

async function forceStopDevice(name) {
    busyActions.set(name, 'stopping');
    renderDevices();
    try {
        const response = await fetch(`/api/devices/${encodeURIComponent(name)}/stop`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ force: true })
        });
        const data = await response.json();
        const ok = data.success && data.still_running !== true;
        showToast(
            ok ? `Force-stopped ${name}` : (data.message || data.error || 'Force stop failed'),
            ok ? 'success' : 'error'
        );
        await refreshStatus();
        if (ok) await confirmDeviceStopped(name);
    } catch (error) {
        showToast('Force stop failed', 'error');
    } finally {
        busyActions.delete(name);
        renderDevices();
    }
}

async function deleteDevice(name) {
    if (busyActions.has(name)) return;
    if (!confirm(`Delete device "${name}" and its AVD? This cannot be undone.`)) {
        return;
    }

    busyActions.set(name, 'deleting');
    renderDevices();

    try {
        const response = await fetch(`/api/devices/${encodeURIComponent(name)}`, {
            method: 'DELETE'
        });

        const data = await response.json();

        if (data.success) {
            showToast(`Deleted ${name}`, 'success');
            await refreshStatus();
        } else {
            showToast(data.error || 'Failed to delete device', 'error');
        }
    } catch (error) {
        showToast('Failed to delete device', 'error');
    } finally {
        busyActions.delete(name);
        renderDevices();
    }
}

function renderCopyItem(label, value, smallMono = false) {
    const val = value || 'N/A';
    const canCopy = val !== 'N/A';
    return `
        <div class="details-item">
            <div class="details-item-head">
                <div class="details-label">${escapeHtml(label)}</div>
                ${canCopy ? `<button type="button" class="copy-btn" data-copy-value="${escapeAttr(val)}" data-copy-label="${escapeAttr(label)}" title="Copy ${escapeAttr(label)}">Copy</button>` : ''}
            </div>
            <div class="details-value${smallMono ? ' small-mono' : ''}">${escapeHtml(val)}</div>
        </div>
    `;
}

async function showDeviceDetails(name) {
    try {
        const device = devices.find(d => d.name === name);
        if (!device) {
            showToast('Device not found', 'error');
            return;
        }
        const identityResponse = await fetch(`/api/device/${encodeURIComponent(name)}/identity`);
        const identity = await identityResponse.json();

        let runtime = null;
        try {
            const statusResponse = await fetch(`/api/devices/${encodeURIComponent(name)}/status`);
            runtime = await statusResponse.json();
        } catch (e) {
            runtime = null;
        }

        const modal = document.getElementById('detailsModal');
        const content = document.getElementById('deviceDetails');

        // Pipeline summary cells
        const rootCell = device.rootable === false
            ? { cls: 'danger', text: 'Unavailable (Play Store)' }
            : (device.rooted === true
                ? { cls: 'ok', text: 'Active (uid=0)' }
                : (device.rooted === false && device.root_error
                    ? { cls: 'danger', text: 'Failed' }
                    : (device.auto_root !== false
                        ? { cls: '', text: 'Auto on launch' }
                        : { cls: '', text: 'Disabled' })));

        const report = device.spoof_report;
        const verifiedCount = report ? Object.keys(report.verified || {}).length : 0;
        const failedCount = report ? (report.failed || []).length : 0;
        const totalProps = verifiedCount + failedCount;
        const spoofCell = device.running && device.runtime_stale
            ? { cls: 'warn', text: 'Lost on restart' }
            : (report
                ? (failedCount === 0 && totalProps > 0
                    ? { cls: 'ok', text: `${verifiedCount}/${totalProps} verified` }
                    : { cls: 'warn', text: `${verifiedCount}/${totalProps} verified` })
                : (device.running ? { cls: 'warn', text: 'Not verified' } : { cls: '', text: 'Idle' }));

        const hooksCell = device.running
            ? (device.runtime_stale
                ? { cls: 'warn', text: 'Lost on restart' }
                : (device.hooks_applied === true
                    ? { cls: 'ok', text: 'Active' }
                    : (device.hooks_error ? { cls: 'warn', text: 'Failed' } : { cls: '', text: 'Not injected' })))
            : { cls: '', text: 'Idle' };

        const serialText = (runtime && runtime.serial) || device.serial || 'Not running';
        const statusCls = device.starting ? 'starting' : (device.running ? 'running' : 'stopped');
        const statusLabel = device.starting ? 'Starting…' : (device.running ? 'Running' : 'Stopped');

        content.innerHTML = `
            <div class="details-hero">
                <div>
                    <h4>${escapeHtml(device.name)}</h4>
                    <div class="details-sub">
                        ${escapeHtml(device.model)} • Android ${escapeHtml(device.android_version)} • ${escapeHtml(device.brand)}
                    </div>
                    <div class="details-sub" style="margin-top:0.35rem;">
                        Image: <code>${escapeHtml(device.image || 'n/a')}</code>
                    </div>
                </div>
                <div class="device-status ${statusCls}">
                    <span class="status-dot"></span>
                    ${statusLabel}
                </div>
            </div>

            <div class="pipeline-strip">
                <div class="pipeline-cell ${rootCell.cls}">
                    <div class="pipeline-label">ADB Root</div>
                    <div class="pipeline-value">${escapeHtml(rootCell.text)}</div>
                </div>
                <div class="pipeline-cell ${spoofCell.cls}">
                    <div class="pipeline-label">Hardware Spoof</div>
                    <div class="pipeline-value">${escapeHtml(spoofCell.text)}</div>
                </div>
                <div class="pipeline-cell ${hooksCell.cls}">
                    <div class="pipeline-label">Frida Hooks</div>
                    <div class="pipeline-value">${escapeHtml(hooksCell.text)}</div>
                </div>
                <div class="pipeline-cell ${device.running ? 'ok' : ''}">
                    <div class="pipeline-label">Radios</div>
                    <div class="pipeline-value">${escapeHtml(
                        device.running
                            ? (device.airplane_mode === true
                                ? '✈ Airplane'
                                : (device.airplane_mode === false ? 'Online' : '—'))
                            : 'Idle')}</div>
                </div>
                <div class="pipeline-cell ${device.running ? 'ok' : ''}">
                    <div class="pipeline-label">ADB Serial</div>
                    <div class="pipeline-value" style="font-family:var(--font-mono);font-size:0.78rem;">${escapeHtml(serialText)}</div>
                </div>
            </div>

            ${device.airplane_error ? `
                <div class="details-alert">
                    <div class="details-alert-title">Airplane mode could not be changed</div>
                    <pre>${escapeHtml(device.airplane_error)}</pre>
                </div>
            ` : ''}

            ${device.running && device.runtime_stale ? `
                <div class="details-alert">
                    <div class="details-alert-title">Spoofing/hooks lost on restart</div>
                    <pre>${escapeHtml(device.runtime_stale_reason || 'The emulator restarted, so the RAM-only property spoof and frida-server are gone.')}</pre>
                    <div class="inline-action-row" style="margin-top:0.5rem;">
                        <button class="btn btn-success btn-sm" data-action="restore" data-device="${escapeAttr(device.name)}">
                            ↻ Restore spoofing + hooks
                        </button>
                    </div>
                </div>
            ` : ''}

            <div class="details-section-heading">
                <h5>Keep Applied After Restart</h5>
            </div>
            <div class="inline-action-row">
                <label class="checkbox-label" title="Re-apply the identity spoof automatically after a reboot or app restart">
                    <input type="checkbox" ${device.auto_spoof !== false ? 'checked' : ''}
                           onchange="setRestoreIntent('${escapeAttr(device.name)}', { spoof: this.checked })">
                    <span>Spoofing</span>
                </label>
                <label class="checkbox-label" title="Re-attach the Frida anti-detect hooks automatically after a reboot or app restart">
                    <input type="checkbox" ${device.auto_hooks !== false ? 'checked' : ''}
                           onchange="setRestoreIntent('${escapeAttr(device.name)}', { hooks: this.checked })">
                    <span>Frida hooks</span>
                </label>
                <button class="btn btn-secondary btn-sm" data-action="restore" data-device="${escapeAttr(device.name)}"
                        ${device.running ? '' : 'disabled'} title="Apply them now">
                    Apply now
                </button>
                <button class="btn btn-primary btn-sm" data-action="recover" data-device="${escapeAttr(device.name)}"
                        ${device.running ? '' : 'disabled'}
                        title="Re-apply the spoof + hooks and re-check the radios in one unattended pass">
                    ↻ Recover now
                </button>
            </div>
            <small class="hint">
                ${device.spoof_applied_at ? `Spoof applied ${escapeHtml(device.spoof_applied_at.slice(0, 19).replace('T', ' '))}. ` : ''}
                ${device.hooks_applied_at ? `Hooks applied ${escapeHtml(device.hooks_applied_at.slice(0, 19).replace('T', ' '))}. ` : ''}
                The property spoof lives in RAM and frida-server is a process, so a reboot clears both —
                with these on, the app re-applies them automatically (on launch and when the dashboard starts).
            </small>

            ${device.rooted === true ? `
                <small class="hint" style="margin-top:0;margin-bottom:0.65rem;">
                    Root-checker apps stay clean on purpose: no <code>su</code> binary is installed, while ADB &amp; Frida run as <code>uid=0</code>${device.rooted_at ? ` (since ${escapeHtml(device.rooted_at.slice(0, 19).replace('T', ' '))})` : ''}.
                </small>
            ` : ''}

            ${device.rooted === false && device.root_error ? `
                <div class="details-alert danger">
                    <div class="details-alert-title">Root Error</div>
                    <pre>${escapeHtml(device.root_error)}</pre>
                </div>
            ` : ''}

            ${device.launch_error ? `
                <div class="details-alert danger">
                    <div class="details-alert-title">Last Launch Error</div>
                    <pre>${escapeHtml(device.launch_error)}</pre>
                </div>
            ` : ''}

            ${device.stop_error ? `
                <div class="details-alert danger">
                    <div class="details-alert-title">Last Stop Problem</div>
                    <pre>${escapeHtml(device.stop_error)}</pre>
                    <small class="hint">Stop could not prove the emulator was gone. Run
                        <code>python main.py doctor</code> for the exact process holding
                        this device's ports, then stop it again.</small>
                </div>
            ` : ''}

            ${device.spoof_error ? `
                <div class="details-alert">
                    <div class="details-alert-title">Spoofing Warning</div>
                    <pre>${escapeHtml(device.spoof_error)}</pre>
                </div>
            ` : ''}

            ${device.hooks_error ? `
                <div class="details-alert">
                    <div class="details-alert-title">Frida Hooks Warning</div>
                    <pre>${escapeHtml(device.hooks_error)}</pre>
                </div>
            ` : ''}

            ${report ? `
                <details class="spoof-disclosure" ${failedCount > 0 ? 'open' : ''}>
                    <summary>
                        <span>Spoof Verification Report — <strong>${verifiedCount}/${totalProps}</strong> verified</span>
                        <span style="color:var(--text-muted);font-weight:400;font-size:0.76rem;">${escapeHtml(report.method || 'unknown')}</span>
                    </summary>
                    <div class="spoof-prop-list">
                        ${(report.failed || []).map(k =>
                            `<div class="spoof-prop-fail">✗ <code>${escapeHtml(k)}</code> (stayed default)</div>`).join('')}
                        ${Object.entries(report.verified || {}).map(([k, v]) =>
                            `<div class="spoof-prop-ok">✓ <code>${escapeHtml(k)}</code> = ${escapeHtml(v)}</div>`).join('')}
                    </div>
                </details>
            ` : ''}

            <div class="details-section-heading">
                <h5>Spoofed Device Identity</h5>
                <button type="button" class="copy-btn" data-regen-identity="${escapeAttr(device.name)}" title="Generate fresh unique IMEI, Android ID, serial, MAC, and build identifiers">↻ New identity</button>
            </div>
            <div class="details-grid">
                ${renderCopyItem('IMEI', identity.imei)}
                ${renderCopyItem('Serial Number', identity.serial)}
                ${renderCopyItem('Android ID', identity.android_id)}
                ${renderCopyItem('MAC Address', identity.mac_address)}
                ${renderCopyItem('Build Fingerprint', identity.fingerprint, true)}
                ${renderCopyItem('Carrier', identity.operator)}
                ${renderCopyItem('GPU', [identity.gl_vendor, identity.gl_renderer].filter(Boolean).join(' '))}
                ${renderCopyItem('GL Version', identity.gl_version)}
            </div>
            ${(() => {
                // The GPU must answer every query from ONE chip: renderer,
                // vendor, EGL vendor, driver version, extensions and limits.
                const limits = identity.gl_limits || {};
                const exts = (identity.gl_extensions || '').split(' ').filter(Boolean);
                if (!identity.gl_renderer) return '';
                return `
                <div class="gpu-facts">
                    <div class="gpu-fact"><span>EGL vendor</span><b>${escapeHtml(identity.egl_vendor || identity.gl_vendor || '—')}</b></div>
                    <div class="gpu-fact"><span>Max texture</span><b>${limits.max_texture_size || '—'}</b></div>
                    <div class="gpu-fact"><span>Max viewport</span><b>${(limits.max_viewport_dims || []).join('×') || '—'}</b></div>
                    <div class="gpu-fact"><span>GLSL</span><b>${escapeHtml((limits.glsl_version || '—').replace('OpenGL ES GLSL ES ', 'ES '))}</b></div>
                    <div class="gpu-fact"><span>Extensions</span><b>${exts.length || 0}</b></div>
                    <div class="gpu-fact"><span>Hooked in</span><b>app + WebGL/GPU process</b></div>
                    <div class="gpu-fact"><span>Emulator renderer</span><b>${escapeHtml(device.gpu_mode || 'swiftshader_indirect')}</b></div>
                </div>
                <small class="hint">
                    Reported by the Frida GLES20/EGL14/native <code>glGetString</code> hooks from one chip, so
                    vendor, renderer, driver version, extension list and GL limits can't contradict each other.
                    <code>ro.hardware.egl</code> is deliberately left alone — changing it makes RenderThread
                    load a missing driver and crashes apps. The emulator itself renders with
                    <code>${escapeHtml(device.gpu_mode || 'swiftshader_indirect')}</code>, never host-GL
                    passthrough, so your desktop card never reaches the guest.
                </small>`;
            })()}

            ${device.running ? `
                <div class="details-section-heading">
                    <h5>Inject Hooks Into Any App</h5>
                </div>
                <div class="inline-action-row">
                    <input type="text" id="detailsHookPackage" placeholder="e.g. com.zhiliaoapp.musically or com.example.app" style="flex:1;">
                    <button type="button" class="btn btn-small btn-warning" data-hook-package="${escapeAttr(device.name)}">
                        Inject hooks
                    </button>
                </div>
                <small class="hint">
                    Launches/attaches cleanly without touching zygote64 and injects all anti-detect + SafetyNet + native hooks.
                    The browser's <strong>WebGL/GPU process</strong> is hooked too (that process is where a fingerprint
                    reads the <code>gpu:</code> vendor/renderer, e.g. Chrome's <code>:privileged_process0</code>) —
                    if the app had not rendered a page yet, inject again after it has.
                </small>
            ` : ''}

            <div class="details-section-heading">
                <h5>Network Proxy</h5>
            </div>
            <div class="inline-action-row">
                <input type="text" id="detailsProxyInput" placeholder="http://host:port or socks5://host:port" value="${escapeAttr(device.proxy || '')}" style="flex:1;">
                <button type="button" class="btn btn-small btn-primary" data-save-proxy="${escapeAttr(device.name)}">Save</button>
                <button type="button" class="btn btn-small btn-secondary" data-clear-proxy="${escapeAttr(device.name)}">Clear</button>
            </div>
            <small class="hint">HTTP/HTTPS proxies apply immediately on a running device; SOCKS5 proxies apply on next launch.</small>

            <div class="details-section-heading">
                <h5>Cameras — two independent sensors</h5>
            </div>
            ${(() => {
                // Each facing is spoofed as its own physical sensor
                // (camera_hooks.js); show what apps will actually see.
                const specs = (identity && identity.camera_specs) || device.camera_specs || {};
                const b = specs.back, f = specs.front;
                if (!b && !f) {
                    return '<small class="hint">Per-camera sensor identity is generated on the next launch (relaunch or re-apply hooks).</small>';
                }
                const row = (label, s) => s ? `
                    <div class="camera-spec-row">
                        <span class="camera-spec-face">${label}</span>
                        <span class="camera-spec-model">${escapeHtml(s.model || 'sensor')}</span>
                        <span class="camera-spec-meta">${s.res_width}×${s.res_height} · f/${escapeHtml(String(s.aperture))} · ${escapeHtml(String(s.focal_length))} mm · ${escapeHtml(String(s.orientation))}°</span>
                        <span class="camera-spec-meta mono">sn ${escapeHtml(s.serial || '')}</span>
                    </div>` : '';
                return `<div class="camera-specs">${row('Back', b)}${row('Front', f)}</div>`;
            })()}
            <div class="camera-grid">
                <div>
                    <small class="hint">Back camera source</small>
                    <input type="text" id="detailsCameraBack" list="cameraOptions"
                        value="${escapeAttr((device.camera && device.camera.back) || 'virtualscene')}">
                </div>
                <div>
                    <small class="hint">Front camera source</small>
                    <input type="text" id="detailsCameraFront" list="cameraOptions"
                        value="${escapeAttr((device.camera && device.camera.front) || 'emulated')}">
                </div>
            </div>
            <div class="inline-action-row">
                <button class="btn btn-small btn-secondary" data-camera-preset="independent" data-device="${escapeAttr(device.name)}"
                        title="Back = host webcam0, front = host webcam1: two physically different real cameras">
                    Two different webcams
                </button>
                <button class="btn btn-small btn-secondary" data-camera-preset="synthetic" data-device="${escapeAttr(device.name)}"
                        title="Back = virtual scene, front = emulated: two different virtual sources">
                    Two virtual sources
                </button>
                <button class="btn btn-small btn-primary" data-save-cameras="${escapeAttr(device.name)}">
                    Save cameras
                </button>
            </div>
            <small class="hint">
                <code>webcam0</code>/<code>webcam1</code> pass different PC webcams through, one per facing —
                the only way to get two genuinely different real images. Either way the <em>identity</em>
                (sensor model, serial, resolution, focal length, orientation) is independently spoofed per camera by the Frida hooks.
                ${device.running ? ' Restart the device to apply source changes.' : ''}
            </small>

            ${device.running ? `
                <div class="details-section-heading">
                    <h5>Android Crash Log (logcat -b crash)</h5>
                    <button type="button" class="copy-btn" data-load-crash-log="${escapeAttr(device.name)}">Fetch crash log</button>
                </div>
                <pre class="log-pre" id="detailsCrashLogPre" style="display:none;"></pre>
            ` : ''}

            ${runtime && runtime.log_tail ? `
                <div class="details-section-heading">
                    <h5>Emulator Log (Tail)</h5>
                    <button type="button" class="copy-btn" data-copy-log="1">Copy log</button>
                </div>
                <pre class="log-pre" id="detailsLogPre">${escapeHtml(runtime.log_tail)}</pre>
            ` : ''}
        `;

        modal.classList.add('active');
    } catch (error) {
        showToast('Failed to load device details', 'error');
    }
}

async function saveProxy(name, clear) {
    const input = document.getElementById('detailsProxyInput');
    const proxy = clear ? '' : (input ? input.value.trim() : '');
    try {
        const res = await fetch(`/api/devices/${encodeURIComponent(name)}/proxy`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ proxy })
        });
        const data = await res.json();
        if (data.success) {
            if (clear && input) input.value = '';
            showToast(data.message || 'Proxy updated', 'success');
            await loadDevices();
        } else {
            showToast(data.error || 'Failed to update proxy', 'error');
        }
    } catch (e) {
        showToast('Failed to update proxy', 'error');
    }
}

async function regenerateIdentity(name) {
    if (!confirm(`Generate a brand-new unique identity (IMEI, Android ID, serial, MAC, fingerprint) for "${name}"?`)) {
        return;
    }
    try {
        const res = await fetch(`/api/devices/${encodeURIComponent(name)}/regenerate-identity`, {
            method: 'POST'
        });
        const data = await res.json();
        if (data.success) {
            showToast(data.message || 'New identity generated', 'success');
            await loadDevices();
            showDeviceDetails(name);
        } else {
            showToast(data.error || 'Failed to regenerate identity', 'error');
        }
    } catch (e) {
        showToast('Failed to regenerate identity', 'error');
    }
}

async function hookCustomPackage(name) {
    const input = document.getElementById('detailsHookPackage');
    const pkg = input ? input.value.trim() : '';
    if (!pkg) {
        showToast('Enter an Android package name first (e.g. com.example.app)', 'warning');
        return;
    }
    showToast(`Injecting hooks into ${pkg}…`, 'info');
    try {
        const res = await fetch(`/api/devices/${encodeURIComponent(name)}/hook-app`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ package: pkg, spawn: false })
        });
        const data = await res.json();
        if (data.success) {
            showToast(data.message || `Hooks active in ${pkg}`, 'success');
        } else {
            showToast(data.error || `Failed to hook ${pkg}`, 'error');
        }
    } catch (e) {
        showToast(`Failed to hook ${pkg}`, 'error');
    }
}

async function loadCrashLog(name) {
    const pre = document.getElementById('detailsCrashLogPre');
    if (pre) {
        pre.style.display = 'block';
        pre.textContent = 'Reading logcat -b crash…';
    }
    try {
        const res = await fetch(`/api/devices/${encodeURIComponent(name)}/crash-log`);
        const data = await res.json();
        if (pre) {
            pre.textContent = data.success ? data.crash_log : (data.error || 'Failed to fetch crash log');
        }
    } catch (e) {
        if (pre) pre.textContent = 'Failed to fetch crash log';
    }
}

function useWebcamForBothDetails() {
    // Both facings then see the *same* host webcam - one shared camera. The
    // sensors still get independent identities, but the images are identical.
    document.getElementById('detailsCameraFront').value = 'webcam0';
    document.getElementById('detailsCameraBack').value = 'webcam0';
    showToast('Both cameras now use webcam0 (same images). Use webcam0 + webcam1 for independent real cameras.', 'warning');
}

function applyCreateCameraPreset(preset) {
    const frontEl = document.getElementById('cameraFront');
    const backEl = document.getElementById('cameraBack');
    if (!frontEl || !backEl) return;
    const pair = preset === 'independent' ? ['webcam1', 'webcam0'] : ['emulated', 'virtualscene'];
    frontEl.value = pair[0];
    backEl.value = pair[1];
    showToast(preset === 'independent'
        ? 'Back = webcam0, front = webcam1 — two different real cameras'
        : 'Back = virtual scene, front = emulated — two different virtual sources', 'info');
}

function applySettingsCameraPreset(preset) {
    const frontEl = document.getElementById('settingsCameraFront');
    const backEl = document.getElementById('settingsCameraBack');
    if (!frontEl || !backEl) return;
    frontEl.value = preset === 'independent' ? 'webcam1' : 'emulated';
    backEl.value = preset === 'independent' ? 'webcam0' : 'virtualscene';
    showToast('Defaults updated — click Save to keep them', 'info');
}

function applyCameraPreset(preset) {
    // Presets for "front and back are independent": either two different
    // host webcams (two genuinely different real images) or two different
    // synthetic sources (distinct scenes, still spoofed as distinct sensors).
    const frontEl = document.getElementById('detailsCameraFront');
    const backEl = document.getElementById('detailsCameraBack');
    if (!frontEl || !backEl) return;
    if (preset === 'independent') {
        backEl.value = 'webcam0';
        frontEl.value = 'webcam1';
        showToast('Back = webcam0, front = webcam1 (two different real cameras) — click Save cameras', 'info');
    } else if (preset === 'synthetic') {
        backEl.value = 'virtualscene';
        frontEl.value = 'emulated';
        showToast('Back = virtual scene, front = emulated (two different virtual sources) — click Save cameras', 'info');
    }
    [frontEl, backEl].forEach(el => {
        el.classList.add('pulse-once');
        setTimeout(() => el.classList.remove('pulse-once'), 1200);
    });
}

async function saveCameras(name) {
    const front = document.getElementById('detailsCameraFront').value.trim();
    const back = document.getElementById('detailsCameraBack').value.trim();

    try {
        const response = await fetch(`/api/devices/${encodeURIComponent(name)}/camera`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ front, back })
        });
        const data = await response.json();

        if (data.success) {
            const restart = data.needs_restart ? ' Restart the device to apply.' : '';
            showToast(`Cameras saved: front=${data.camera.front}, back=${data.camera.back}.${restart}`, 'success');
            if (data.warning) {
                // e.g. both facings use the same source -> one shared camera
                showToast(data.warning, 'warning');
            }
            await loadDevices();
        } else {
            showToast(data.error || 'Invalid camera settings', 'error');
        }
    } catch (error) {
        showToast('Failed to save cameras', 'error');
    }
}

// =========================================
// SEARCH, FILTER & RENDER
// =========================================

function setDeviceSearch(value) {
    deviceSearchQuery = (value || '').trim().toLowerCase();
    renderDevices();
}

function setDeviceFilter(filter) {
    deviceStatusFilter = filter || 'all';
    document.querySelectorAll('.filter-pill').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.filter === deviceStatusFilter);
    });
    renderDevices();
}

function getFilteredDevices() {
    return devices.filter(d => {
        if (deviceStatusFilter === 'running' && !d.running && !d.starting) return false;
        if (deviceStatusFilter === 'stopped' && (d.running || d.starting)) return false;
        if (deviceSearchQuery) {
            const hay = `${d.name || ''} ${d.model || ''} ${d.brand || ''} android ${d.android_version || ''} ${d.serial || ''}`.toLowerCase();
            if (!hay.includes(deviceSearchQuery)) return false;
        }
        return true;
    });
}

function buildSpoofTag(device) {
    if (!device.running) return '';
    const report = device.spoof_report;
    if (report && report.verified) {
        const verified = Object.keys(report.verified || {}).length;
        const failed = (report.failed || []).length;
        const total = verified + failed;
        const method = report.method || 'spoof';
        if (total > 0 && failed === 0) {
            return `<span class="device-tag tag-ok tag-clickable" data-action="details" data-device="${escapeAttr(device.name)}" title="All ${total} hardware props verified (${escapeAttr(method)}) — click to inspect">✓ ${verified}/${total} props</span>`;
        }
        if (total > 0) {
            return `<span class="device-tag tag-warn tag-clickable" data-action="details" data-device="${escapeAttr(device.name)}" title="${failed} prop(s) did not match (${escapeAttr(method)}) — click to inspect">⚠ ${verified}/${total} props</span>`;
        }
    }
    if (device.spoof_error) {
        return `<span class="device-tag tag-warn tag-clickable" data-action="details" data-device="${escapeAttr(device.name)}" title="${escapeAttr(device.spoof_error)}">⚠ Spoof incomplete</span>`;
    }
    return '';
}

// ---------------------------------------------------------------------------
// Boot progress panel
// ---------------------------------------------------------------------------
// A first boot takes minutes. The card used to say only "Starting…" for the
// whole chain, so there was no way to tell a slow boot from a hung one. These
// phases mirror `DeviceManager.LAUNCH_PHASES`; the server sends which one is
// running plus everything that has already finished.
const BOOT_PHASES = [
    ['preparing', 'Checking SDK, AVD and ports'],
    ['starting', 'Starting the emulator'],
    ['booting', 'Booting Android'],
    ['rooting', 'Enabling root'],
    ['spoofing', 'Applying & verifying the spoof'],
    ['hooking', 'Attaching Frida hooks'],
    ['finishing', 'Finishing up'],
];

let bootPollTimer = null;

function formatElapsed(seconds) {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    if (total < 60) return `${total}s`;
    const m = Math.floor(total / 60);
    return `${m}m ${String(total % 60).padStart(2, '0')}s`;
}

// Fast refresh while something is booting, so the panel moves every second
// instead of jumping every 10s (the idle auto-refresh interval).
function ensureBootPolling() {
    const starting = devices.some(d => d.starting);
    if (starting && !bootPollTimer) {
        bootPollTimer = setInterval(() => { loadDevices(); }, 1200);
    } else if (!starting && bootPollTimer) {
        clearInterval(bootPollTimer);
        bootPollTimer = null;
    }
}

function buildBootPanel(device) {
    const bp = device.boot_progress;
    if (!bp) {
        // No record yet (or an older backend): never leave the card silent.
        return `<div class="starting-banner">Booting AVD → auto-root → spoof → Frida hooks…</div>`;
    }

    const phases = (bp.phases && bp.phases.length)
        ? bp.phases
        : BOOT_PHASES.map(([key, label]) => ({ key, label }));
    const index = Number.isInteger(bp.index) ? bp.index : 0;
    const percent = Math.max(0, Math.min(100, Math.round(Number(bp.percent) || 0)));
    const failed = Boolean(bp.failed);

    const rows = phases.map((phase, i) => {
        let state = 'pending';
        if (failed && i === index) state = 'failed';
        else if (i < index) state = 'done';
        else if (i === index) state = 'active';
        const icon = state === 'done'
            ? '<span class="boot-icon boot-icon-done" aria-hidden="true">✓</span>'
            : state === 'failed'
                ? '<span class="boot-icon boot-icon-failed" aria-hidden="true">✕</span>'
                : state === 'active'
                    ? '<span class="boot-icon boot-icon-active" aria-hidden="true"></span>'
                    : '<span class="boot-icon boot-icon-pending" aria-hidden="true">○</span>';
        const verb = state === 'done' ? 'done' : (state === 'failed' ? 'failed'
            : (state === 'active' ? 'in progress' : 'waiting'));
        // What this step actually reported, so a finished step proves it ran
        // instead of leaving a bare tick with no evidence.
        const last = (bp.history || []).filter(h => h.phase === phase.key).pop();
        const note = (state === 'done' && last && last.text)
            ? `<span class="boot-step-note">${escapeHtml(last.text)}</span>`
            : '';
        return `<li class="boot-step ${state}" title="${escapeAttr(phase.label)} — ${verb}">
                    ${icon}<span class="boot-step-body">
                        <span class="boot-step-label">${escapeHtml(phase.label)}</span>
                        ${note}
                    </span>
                </li>`;
    }).join('');

    const quiet = Number(bp.since_change || 0);
    const stalled = Boolean(bp.stalled) || (!failed && quiet >= 20 && index > 0);
    // Saying how long a step has been quiet is what separates "slow" from
    // "wedged" - the panel used to repeat one sentence for minutes.
    const quietNote = (!failed && quiet >= 20)
        ? `<span class="boot-quiet">${stalled ? 'no new step for ' : 'quiet for '}${formatElapsed(quiet)}</span>`
        : '';
    const detail = bp.detail
        ? `<div class="boot-now ${failed ? 'is-failed' : (stalled ? 'is-quiet' : '')}">
               <span class="boot-now-label">${failed ? 'Failed' : (stalled ? 'Still' : 'Now')}</span>
               <span class="boot-now-text">${escapeHtml(bp.detail)}</span>
               ${quietNote}
           </div>`
        : '';

    return `
        <div class="boot-panel ${failed ? 'is-failed' : ''}" role="status" aria-live="polite"
             aria-label="Boot progress for ${escapeAttr(device.name)}">
            <div class="boot-panel-head">
                <span class="boot-title">${failed ? '⚠ Launch failed' : 'Starting device'}</span>
                <span class="boot-meta">${formatElapsed(bp.elapsed)}${failed ? '' : ` · step ${Math.min(index + 1, phases.length)}/${phases.length}`}</span>
            </div>
            <div class="boot-bar-track" role="progressbar" aria-valuemin="0" aria-valuemax="100"
                 aria-valuenow="${percent}" aria-valuetext="${escapeAttr(bp.label || '')} ${percent}%">
                <div class="boot-bar-fill" style="width:${percent}%"></div>
            </div>
            <ol class="boot-steps">${rows}</ol>
            ${detail}
        </div>`;
}

function renderDevices() {
    const grid = document.getElementById('devicesGrid');
    const countBadge = document.getElementById('deviceCountBadge');
    const filtered = getFilteredDevices();

    if (countBadge) {
        countBadge.textContent = filtered.length === devices.length
            ? String(devices.length)
            : `${filtered.length}/${devices.length}`;
    }

    if (devices.length === 0) {
        grid.innerHTML = `
            <div class="empty-state">
                <div class="empty-state-icon" aria-hidden="true">
                    <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="2" width="14" height="20" rx="2" ry="2"/><line x1="12" y1="18" x2="12.01" y2="18"/></svg>
                </div>
                <h3>No virtual devices yet</h3>
                <p>
                    Nothing is created for you - the starter device this app used to
                    invent on first start is gone, so the first phone in this list is
                    always one <em>you</em> picked: model, cameras and all.
                </p>
                <p class="hint">
                    Pick a model, then create it with hardware spoofing, a unique
                    IMEI/serial and automatic Frida hooks.
                </p>
                <button class="btn btn-primary" onclick="showCreateDialog()">+ Choose a model &amp; create a device</button>
            </div>
        `;
        updateStats();
        return;
    }

    if (filtered.length === 0) {
        grid.innerHTML = `
            <div class="empty-state">
                <h3>No matching devices</h3>
                <p>No devices match your current search or filter.</p>
                <button class="btn btn-secondary btn-sm" onclick="clearDeviceFilters()">Clear filters</button>
            </div>
        `;
        updateStats();
        return;
    }

    grid.innerHTML = filtered.map(device => {
        const nameAttr = escapeAttr(device.name);
        const busy = busyActions.get(device.name);
        const isDisabled = Boolean(busy);
        // A failed launch must be retryable in one click: "Launch" is shown
        // as "↻ Retry launch" (and stops nagging until the next attempt).
        const failed = Boolean(device.launch_error) && !device.running && !device.starting;
        const stateCls = device.starting ? 'starting' : (device.running ? 'running' : 'stopped');
        const cardStateCls = device.starting ? 'is-starting' : (device.running ? 'is-running' : '');
        const usesWebcam = device.camera &&
            (device.camera.front === 'webcam0' || device.camera.back === 'webcam0');

        return `
        <div class="device-card ${cardStateCls}">
            <div class="device-header">
                <div class="device-info">
                    <h3 title="${nameAttr}">${escapeHtml(device.name)}</h3>
                    <div class="device-model">
                        <span>${escapeHtml(device.model)}</span>
                        ${device.running && device.serial
                            ? `<span class="device-serial-chip">${escapeHtml(device.serial)}</span>`
                            : ''}
                    </div>
                </div>
                <div class="device-status ${stateCls}">
                    <span class="status-dot"></span>
                    ${device.starting
                        ? `Starting${device.boot_progress && device.boot_progress.percent ? ` ${device.boot_progress.percent}%` : '…'}`
                        : (device.running ? 'Running' : 'Stopped')}
                </div>
            </div>

            ${device.starting ? buildBootPanel(device) : ''}

            <div class="device-details">
                <span class="device-tag">Android ${escapeHtml(device.android_version)}</span>
                <span class="device-tag">${escapeHtml(device.brand)}</span>
                ${buildSpoofTag(device)}
                ${device.launch_error && !device.running
                    ? `<span class="device-tag tag-warn tag-clickable" data-action="details" data-device="${nameAttr}" title="${escapeAttr(device.launch_error)}">⚠ Launch failed</span>`
                    : ''}
                ${device.stop_error
                    ? `<span class="device-tag tag-warn tag-clickable" data-action="details" data-device="${nameAttr}" title="${escapeAttr(device.stop_error)}">⚠ Stop didn't finish</span>`
                    : ''}
                ${device.running && device.runtime_stale
                    ? `<span class="device-tag tag-warn tag-clickable" data-action="details" data-device="${nameAttr}" title="${escapeAttr(device.runtime_stale_reason || 'The emulator restarted and lost its spoofing/hooks - click Restore')}">⚠ Needs restore</span>`
                    : ''}
                ${device.running && device.hooks_in_flight
                    ? '<span class="device-tag tag-info" title="Injecting Frida anti-detect hooks in background…">◌ Hooks…</span>'
                    : (device.running && device.hooks_applied === true
                        ? '<span class="device-tag tag-ok" title="Frida anti-detect hooks attached and active">✓ Hooks</span>'
                        : (device.running && device.hooks_error
                            ? `<span class="device-tag tag-warn tag-clickable" data-action="details" data-device="${nameAttr}" title="${escapeAttr(device.hooks_error)}">⚠ Hooks failed</span>`
                            : ''))}
                ${device.rootable === false
                    ? '<span class="device-tag tag-warn" title="Play Store image: adb root unavailable, Frida will not work">⚠ No root</span>'
                    : (device.rooted === true
                        ? '<span class="device-tag tag-ok" title="Rooted with adb root (auto-root after boot)">✓ Root</span>'
                        : (device.rooted === false && device.root_error
                            ? `<span class="device-tag tag-warn tag-clickable" data-action="details" data-device="${nameAttr}" title="${escapeAttr(device.root_error)}">⚠ Root failed</span>`
                            : '<span class="device-tag tag-ok" title="Google APIs image: adb root available (auto-roots on launch)">✓ Root capable</span>'))}
                ${device.proxy ? '<span class="device-tag tag-info" title="Proxy configured">Proxy</span>' : ''}
                ${usesWebcam ? '<span class="device-tag tag-info" title="Real webcam passthrough (webcam0)">Webcam</span>' : ''}
            </div>

            <div class="device-actions">
                ${device.starting ? `
                    <button class="btn btn-danger btn-sm" data-action="stop" data-device="${nameAttr}" ${isDisabled ? 'aria-disabled="true"' : ''} title="Abort launch and stop the emulator">${busy === 'stopping' ? 'Stopping…' : 'Stop'}</button>
                ` : device.running ? `
                    <button class="btn btn-primary btn-sm" data-action="spoof" data-device="${nameAttr}" ${isDisabled ? 'aria-disabled="true"' : ''} title="Re-apply identity spoofing and verify each prop with getprop">${busy === 'spoofing' ? 'Spoofing…' : '🎭 Spoof'}</button>
                    <button class="btn btn-warning btn-sm" data-action="hooks" data-device="${nameAttr}" ${isDisabled ? 'aria-disabled="true"' : ''} title="(Re)inject the anti-detect Frida hooks">${busy === 'hooking' ? 'Hooking…' : '🔧 Hooks'}</button>
                    ${device.needs_restore || device.runtime_stale
                        ? `<button class="btn btn-success btn-sm" data-action="restore" data-device="${nameAttr}" ${isDisabled ? 'aria-disabled="true"' : ''} title="Re-apply the spoofing + Frida hooks saved for this device (they were lost on reboot)">${busy === 'restoring' ? 'Restoring…' : '↻ Restore'}</button>`
                        : ''}
                    <button class="btn btn-danger btn-sm" data-action="stop" data-device="${nameAttr}" ${isDisabled ? 'aria-disabled="true"' : ''} title="${device.stop_error ? 'The last stop did not finish - click to try again' : 'Shut down the emulator'}">${busy === 'stopping' ? 'Stopping…' : (device.stop_error ? '↻ Retry stop' : 'Stop')}</button>
                ` : `
                    <button class="btn btn-success btn-sm${busy === 'launching' ? ' is-busy' : ''}" data-action="launch" data-device="${nameAttr}" ${isDisabled ? 'aria-disabled="true"' : ''} title="${failed ? 'The last launch failed - click to try again (Details shows the error)' : 'Boot the emulator, then auto-root + spoof (verified) + Frida hooks, hands-free'}">${busy === 'launching' ? '⏳ Launching…' : (failed ? '↻ Retry launch' : '🚀 Launch')}</button>
                `}
                <button class="btn btn-secondary btn-sm" data-action="details" data-device="${nameAttr}" title="Identity, root/spoof/hook status, verify report &amp; emulator log">📋 Details</button>
                <button class="btn btn-icon btn-icon-danger btn-sm" data-action="delete" data-device="${nameAttr}" ${isDisabled ? 'aria-disabled="true"' : ''} title="Delete this device and its AVD" aria-label="Delete ${nameAttr}">🗑️</button>
            </div>
        </div>
        `;
    }).join('');

    updateStats();
}

function clearDeviceFilters() {
    const searchInput = document.getElementById('deviceSearch');
    if (searchInput) searchInput.value = '';
    deviceSearchQuery = '';
    setDeviceFilter('all');
}

function updateStats() {
    ensureBootPolling();
    document.getElementById('totalDevices').textContent = devices.length;
    const running = devices.filter(d => d.running);
    document.getElementById('runningDevices').textContent = running.length;
    // Honest count: only devices whose hook chain actually completed.
    document.getElementById('protectedDevices').textContent =
        devices.filter(d => d.running && d.hooks_applied === true).length;

    // Keep the Device Spoofing status card synchronized with live spoof reports
    const card = document.getElementById('spoofStatus');
    if (card) {
        const indicator = card.querySelector('.status-indicator');
        const text = card.querySelector('.status-text');
        const starting = devices.some(d => d.starting);
        const reported = running.filter(d => d.spoof_report && d.spoof_report.verified);
        if (starting) {
            indicator.className = 'status-indicator warning';
            text.textContent = 'Applying spoof…';
        } else if (reported.length) {
            let totalVerified = 0;
            let totalFailed = 0;
            for (const d of reported) {
                totalVerified += Object.keys(d.spoof_report.verified || {}).length;
                totalFailed += (d.spoof_report.failed || []).length;
            }
            const total = totalVerified + totalFailed;
            if (totalFailed === 0 && total > 0) {
                indicator.className = 'status-indicator connected';
                text.textContent = `Verified (${totalVerified}/${total} props)`;
            } else {
                indicator.className = 'status-indicator warning';
                text.textContent = `Incomplete (${totalVerified}/${total} props)`;
            }
        } else if (running.length) {
            indicator.className = 'status-indicator warning';
            text.textContent = 'Running (not verified yet)';
        } else {
            indicator.className = 'status-indicator disconnected';
            text.textContent = 'Ready';
        }
    }
}

// =========================================
// MODAL FUNCTIONS
// =========================================

function showCreateDialog() {
    document.getElementById('cameraFront').value = cameraDefaults.front || 'emulated';
    document.getElementById('cameraBack').value = cameraDefaults.back || 'virtualscene';
    const autoRoot = document.getElementById('autoRoot');
    if (autoRoot) autoRoot.checked = autoRootDefault;
    loadSystemImages().then(updateImageVariantHint);
    document.getElementById('createModal').classList.add('active');
    setTimeout(() => {
        const nameInput = document.getElementById('deviceName');
        if (nameInput) nameInput.focus();
    }, 60);
}

// ---- Custom system-image import (folder containing system.img) ----

function showImageImport() {
    const box = document.getElementById('imageImportBox');
    const shown = box.style.display !== 'none';
    box.style.display = shown ? 'none' : 'block';
    if (!shown) {
        const versionSel = document.getElementById('importImageVersion');
        if (versionSel && !versionSel.options.length) {
            const androidSel = document.getElementById('androidVersion');
            for (const opt of androidSel.options) {
                versionSel.appendChild(new Option(opt.textContent, opt.value));
            }
        }
    }
}

async function pickImageFolder() {
    try {
        const response = await fetch('/api/system-images/pick-folder', { method: 'POST' });
        const data = await response.json();
        if (data.success && data.path) {
            document.getElementById('importImagePath').value = data.path;
        } else if (data.error && !/no folder selected/i.test(data.error)) {
            showToast(data.error, 'error');
        }
    } catch (e) {
        showToast('Folder picker failed — paste the path manually', 'error');
    }
}

// Android version key -> API level (matches config.ANDROID_VERSIONS).
const VERSION_TO_API = { '15': 35, '14': 34, '13': 33, '12': 31, '11': 30, '10': 29, '9': 28 };

async function importImage() {
    const path = document.getElementById('importImagePath').value.trim();
    const version = document.getElementById('importImageVersion').value;
    const tag = document.getElementById('importImageTag').value;
    const goBtn = document.getElementById('importImageGo');
    if (!path) {
        showToast('Pick (or paste) the folder containing system.img', 'error');
        return;
    }
    const apiLevel = VERSION_TO_API[version];
    if (!apiLevel) {
        showToast('Pick the Android version this image matches', 'error');
        return;
    }
    goBtn.disabled = true;
    goBtn.textContent = 'Copying… (can be GBs)';
    showToast('Importing image — a 3–5 GB copy can take a while', 'info');
    try {
        const response = await fetch('/api/system-images/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                source_dir: path,
                api_level: apiLevel,
                tag: tag,
                abi: 'x86_64'
            })
        });
        const data = await response.json();
        if (data.success) {
            showToast(data.message || 'Image imported', 'success');
            hideImageImport();
            await loadSystemImages();
            const select = document.getElementById('imageVariant');
            select.value = 'pkg:' + data.package;
            updateImageVariantHint();
        } else {
            showToast(data.error || 'Import failed', 'error');
        }
    } catch (e) {
        showToast('Import request failed', 'error');
    }
    goBtn.disabled = false;
    goBtn.textContent = 'Import';
}

function hideImageImport() {
    const box = document.getElementById('imageImportBox');
    if (box) box.style.display = 'none';
}

function hideCreateDialog() {
    document.getElementById('createModal').classList.remove('active');
}

function hideDetailsModal() {
    document.getElementById('detailsModal').classList.remove('active');
}

function showHelp() {
    const modal = document.getElementById('helpModal');
    if (modal) modal.classList.add('active');
}

function hideHelp() {
    const modal = document.getElementById('helpModal');
    if (modal) modal.classList.remove('active');
}

let sdkInstallPoller = null;

async function showSettings() {
    document.getElementById('settingsModal').classList.add('active');
    await loadSettings();
    await loadAdbStatus();
}

function hideSettings() {
    document.getElementById('settingsModal').classList.remove('active');
    if (sdkInstallPoller) {
        clearInterval(sdkInstallPoller);
        sdkInstallPoller = null;
    }
}

async function loadSettings() {
    const hint = document.getElementById('sdkPathHint');
    try {
        const response = await fetch('/api/settings');
        const data = await response.json();

        const input = document.getElementById('sdkPathInput');
        if (data.sdk_root) {
            input.value = data.sdk_root;
        }

        const missing = [];
        if (!data.emulator_found) missing.push('emulator');
        if (!data.adb_found) missing.push('adb');
        if (!data.avdmanager_found) missing.push('avdmanager');

        if (missing.length === 0) {
            hint.className = 'hint ok';
            hint.textContent = `SDK ready (${data.sdk_root_source}). AVDs: ${(data.avds || []).length}`;
        } else if (data.sdk_root) {
            hint.className = 'hint warn';
            hint.textContent = `SDK found (${data.sdk_root_source}) but missing: ${missing.join(', ')}. Use Install below.`;
        } else {
            hint.className = 'hint warn';
            hint.textContent = 'No SDK selected yet. Click Browse… and pick your Android SDK folder.';
        }

        const versionSelect = document.getElementById('sdkInstallVersion');
        if (versionSelect.options.length === 0) {
            const vres = await fetch('/api/android-versions');
            const versions = await vres.json();
            versionSelect.innerHTML = '';
            for (const v of versions) {
                const option = document.createElement('option');
                option.value = v.version;
                option.textContent = `Android ${v.version} (API ${v.api})${v.installed ? ' ✓ installed' : ''}`;
                if (v.installed) option.selected = true;
                versionSelect.appendChild(option);
            }
        }

        const desktopHint = document.getElementById('desktopHint');
        desktopHint.className = 'hint';
        if (data.desktop_mode) {
            desktopHint.textContent = 'Running as a desktop app window.';
        } else if (data.desktop_available) {
            desktopHint.textContent = 'Desktop window available — start with "python main.py gui --desktop" (or START.bat on Windows).';
        } else {
            desktopHint.textContent = 'Browser mode. For a standalone app window run "pip install pywebview" then "python main.py gui --desktop" (START.bat does it for you on Windows).';
        }

        // Default cameras (saved in Settings, used for new devices)
        const savedCameras = (data.settings) || {};
        if (savedCameras.camera_front) cameraDefaults.front = savedCameras.camera_front;
        if (savedCameras.camera_back) cameraDefaults.back = savedCameras.camera_back;
        const settingsFront = document.getElementById('settingsCameraFront');
        const settingsBack = document.getElementById('settingsCameraBack');
        if (settingsFront) settingsFront.value = cameraDefaults.front;
        if (settingsBack) settingsBack.value = cameraDefaults.back;

        // Auto-root default (saved in Settings, used for new devices)
        if (typeof savedCameras.auto_root === 'boolean') {
            autoRootDefault = savedCameras.auto_root;
        }
        const settingsAutoRoot = document.getElementById('settingsAutoRoot');
        if (settingsAutoRoot) settingsAutoRoot.checked = autoRootDefault;
    } catch (error) {
        hint.className = 'hint warn';
        hint.textContent = 'Could not load settings: ' + error;
    }
}

async function saveAutoRoot() {
    const btn = document.getElementById('saveAutoRootBtn');
    const enabled = document.getElementById('settingsAutoRoot').checked;

    btn.disabled = true;
    try {
        const response = await fetch('/api/settings/auto-root', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ auto_root: enabled })
        });
        const data = await response.json();

        if (data.success) {
            autoRootDefault = enabled;
            showToast(data.message || 'Auto-root setting saved', 'success');
        } else {
            showToast(data.error || 'Failed to save auto-root setting', 'error');
        }
    } catch (error) {
        showToast('Failed to save auto-root setting', 'error');
    }
    btn.disabled = false;
}

function useWebcamForBothSettings() {
    document.getElementById('settingsCameraFront').value = 'webcam0';
    document.getElementById('settingsCameraBack').value = 'webcam0';
    showToast('Both cameras set to webcam0 — click "Save defaults"', 'info');
}

async function saveCameraDefaults() {
    const btn = document.getElementById('saveCameraDefaultsBtn');
    const front = document.getElementById('settingsCameraFront').value.trim();
    const back = document.getElementById('settingsCameraBack').value.trim();

    btn.disabled = true;
    try {
        const response = await fetch('/api/settings/cameras', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ camera_front: front, camera_back: back })
        });
        const data = await response.json();

        if (data.success) {
            cameraDefaults = { front: data.camera_front, back: data.camera_back };
            showToast(data.message || 'Default cameras saved', 'success');
        } else {
            showToast(data.error || 'Failed to save camera defaults', 'error');
        }
    } catch (error) {
        showToast('Failed to save camera defaults', 'error');
    }
    btn.disabled = false;
}

async function browseSdkFolder() {
    showToast('Opening folder picker…', 'info');
    try {
        const response = await fetch('/api/settings/pick-sdk', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });
        const data = await response.json();
        if (data.success && data.path) {
            document.getElementById('sdkPathInput').value = data.path;
            showToast('Folder selected — click Save', 'success');
        } else {
            showToast(data.error || 'No folder selected', 'error');
        }
    } catch (error) {
        showToast('Could not open the folder picker', 'error');
    }
}

async function saveSdkPath() {
    const path = document.getElementById('sdkPathInput').value.trim();
    if (!path) {
        showToast('Enter or browse to your Android SDK folder', 'error');
        return;
    }
    try {
        const response = await fetch('/api/settings/sdk', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path })
        });
        const data = await response.json();
        if (data.success) {
            showToast('SDK folder saved', 'success');
            await loadSettings();
            await refreshStatus();
        } else {
            showToast(data.error || 'Invalid SDK folder', 'error');
        }
    } catch (error) {
        showToast('Failed to save the SDK folder', 'error');
    }
}

async function installSdkComponents() {
    const versionSelect = document.getElementById('sdkInstallVersion');
    const log = document.getElementById('sdkInstallLog');
    const button = document.getElementById('sdkInstallBtn');
    const path = document.getElementById('sdkPathInput').value.trim();

    if (!path) {
        showToast('Pick your Android SDK folder first', 'error');
        return;
    }

    log.classList.add('visible');
    log.textContent = 'Starting installation…';
    button.disabled = true;

    try {
        const response = await fetch('/api/sdk/install', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ android_version: versionSelect.value, path })
        });
        const data = await response.json();
        if (!data.success) {
            showToast(data.error || 'Installation failed to start', 'error');
            button.disabled = false;
            return;
        }
    } catch (error) {
        showToast('Installation failed to start', 'error');
        button.disabled = false;
        return;
    }

    if (sdkInstallPoller) clearInterval(sdkInstallPoller);
    sdkInstallPoller = setInterval(async () => {
        try {
            const response = await fetch('/api/sdk/install/status');
            const status = await response.json();
            log.textContent = status.log || 'Working…';
            log.scrollTop = log.scrollHeight;
            if (!status.running) {
                clearInterval(sdkInstallPoller);
                sdkInstallPoller = null;
                button.disabled = false;
                if (status.error) {
                    showToast('Install finished with errors', 'error');
                } else {
                    showToast('SDK components installed', 'success');
                }
                await loadSettings();
                await refreshStatus();
            }
        } catch (error) {
            clearInterval(sdkInstallPoller);
            sdkInstallPoller = null;
            button.disabled = false;
        }
    }, 1500);
}

// =========================================
// TOAST NOTIFICATIONS
// =========================================

function showToast(message, type = 'info') {
    const container = document.getElementById('toastContainer');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast ${type}`;

    const icons = {
        success: '✅',
        error: '❌',
        warning: '⚠️',
        info: 'ℹ️'
    };

    const iconSpan = document.createElement('span');
    iconSpan.className = 'toast-icon';
    iconSpan.textContent = icons[type] || icons.info;

    const msgSpan = document.createElement('span');
    msgSpan.className = 'toast-msg';
    msgSpan.textContent = String(message ?? '');

    toast.appendChild(iconSpan);
    toast.appendChild(msgSpan);

    // Click a toast to dismiss it immediately
    toast.addEventListener('click', () => toast.remove());

    container.appendChild(toast);

    const duration = type === 'error' ? 12000 : (type === 'warning' ? 6000 : 3400);
    setTimeout(() => {
        if (!toast.isConnected) return;
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(24px)';
        toast.style.transition = 'opacity 0.25s ease, transform 0.25s ease';
        setTimeout(() => toast.remove(), 260);
    }, duration);
}

// =========================================
// MODAL BACKDROP & KEYBOARD SHORTCUTS
// =========================================

document.querySelectorAll('.modal').forEach(modal => {
    modal.addEventListener('click', (e) => {
        if (e.target === modal) {
            modal.classList.remove('active');
            if (modal.id === 'settingsModal' && sdkInstallPoller) {
                clearInterval(sdkInstallPoller);
                sdkInstallPoller = null;
            }
        }
    });
});

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        document.querySelectorAll('.modal.active').forEach(modal => {
            modal.classList.remove('active');
        });
        if (sdkInstallPoller) {
            clearInterval(sdkInstallPoller);
            sdkInstallPoller = null;
        }
        return;
    }

    // Ignore shortcuts while typing in any input/select/textarea or with modifiers
    const tag = (e.target && e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.ctrlKey || e.metaKey || e.altKey) {
        return;
    }

    // Ignore shortcuts if a modal is already open
    if (document.querySelector('.modal.active')) {
        return;
    }

    if (e.key === 'n' || e.key === 'N') {
        e.preventDefault();
        showCreateDialog();
    } else if (e.key === 'r' || e.key === 'R') {
        e.preventDefault();
        manualRefresh();
    } else if (e.key === 's' || e.key === 'S') {
        e.preventDefault();
        showSettings();
    } else if (e.key === '?') {
        e.preventDefault();
        showHelp();
    }
});
