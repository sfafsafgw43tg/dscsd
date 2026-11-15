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

    // Event delegation for device card buttons & clickable tags (safe for any device name)
    const grid = document.getElementById('devicesGrid');
    if (grid) {
        grid.addEventListener('click', (e) => {
            const target = e.target.closest('[data-action]');
            if (!target) return;
            const action = target.dataset.action;
            const name = target.dataset.device;
            if (!name) return;
            if (action === 'launch') launchDevice(name);
            else if (action === 'stop') stopDevice(name);
            else if (action === 'spoof') applyAntidetect(name);
            else if (action === 'hooks') applyFridaHooks(name);
            else if (action === 'details') showDeviceDetails(name);
            else if (action === 'delete') deleteDevice(name);
        });
    }

    // Event delegation for Details modal actions (Copy buttons, Save cameras)
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
            option.value = model.name;
            option.textContent = model.name;
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
            showToast('Device created successfully!', 'success');
            if (data.warning) {
                showToast(data.warning, 'warning');
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

async function launchDevice(name) {
    if (busyActions.has(name)) return;
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
            showToast(data.error || 'Failed to launch device', 'error');
            return;
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

// Poll the async launch state so the UI can report boot progress and the
// real emulator error instead of hanging on the launch request.
async function pollLaunchStatus(name, attempt) {
    const maxAttempts = 90;  // ~3 minutes at 2s intervals
    try {
        const response = await fetch(`/api/devices/${encodeURIComponent(name)}/status`);
        const status = await response.json();
        const chainState = status.launch_state || status.state;

        if (chainState === 'error') {
            showToast(`Launch failed: ${status.error}. Open Details for the log tail, or Settings → Run doctor.`, 'error');
            console.error('Emulator log tail:\n' + (status.log_tail || ''));
            loadDevices();
            return;
        }

        if (chainState === 'running') {
            showToast(`${name} is running (${status.serial || 'emulator'})`, 'success');
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

        if (data.success) {
            showToast(`Stopped ${name}`, 'success');
            await refreshStatus();
        } else {
            showToast(data.error || 'Failed to stop device', 'error');
        }
    } catch (error) {
        showToast('Failed to stop device', 'error');
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
        const spoofCell = report
            ? (failedCount === 0 && totalProps > 0
                ? { cls: 'ok', text: `${verifiedCount}/${totalProps} verified` }
                : { cls: 'warn', text: `${verifiedCount}/${totalProps} verified` })
            : (device.running ? { cls: 'warn', text: 'Not verified' } : { cls: '', text: 'Idle' });

        const hooksCell = device.running
            ? (device.hooks_applied === true
                ? { cls: 'ok', text: 'Active' }
                : (device.hooks_error ? { cls: 'warn', text: 'Failed' } : { cls: '', text: 'Not injected' }))
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
                    <div class="pipeline-label">ADB Serial</div>
                    <div class="pipeline-value" style="font-family:var(--font-mono);font-size:0.78rem;">${escapeHtml(serialText)}</div>
                </div>
            </div>

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
            </div>
            <div class="details-grid">
                ${renderCopyItem('IMEI', identity.imei)}
                ${renderCopyItem('Serial Number', identity.serial)}
                ${renderCopyItem('Android ID', identity.android_id)}
                ${renderCopyItem('MAC Address', identity.mac_address)}
                ${renderCopyItem('Build Fingerprint', identity.fingerprint, true)}
                ${renderCopyItem('Carrier', identity.operator)}
            </div>

            ${device.proxy ? `
                <div class="details-section-heading">
                    <h5>Network Proxy</h5>
                </div>
                <div class="details-grid">
                    ${renderCopyItem('Proxy', device.proxy, true)}
                </div>
            ` : ''}

            <div class="details-section-heading">
                <h5>Cameras</h5>
            </div>
            <div class="camera-grid">
                <div>
                    <small class="hint">Back camera</small>
                    <input type="text" id="detailsCameraBack" list="cameraOptions"
                        value="${escapeAttr((device.camera && device.camera.back) || 'virtualscene')}">
                </div>
                <div>
                    <small class="hint">Front camera</small>
                    <input type="text" id="detailsCameraFront" list="cameraOptions"
                        value="${escapeAttr((device.camera && device.camera.front) || 'emulated')}">
                </div>
            </div>
            <div class="inline-action-row">
                <button class="btn btn-small btn-secondary" onclick="useWebcamForBothDetails()">
                    Use webcam0 for both
                </button>
                <button class="btn btn-small btn-primary" data-save-cameras="${escapeAttr(device.name)}">
                    Save cameras
                </button>
            </div>
            <small class="hint"><code>webcam0</code> passes your real PC webcam through to the emulator.${device.running ? ' Restart the device to apply changes.' : ''}</small>

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

function useWebcamForBothDetails() {
    document.getElementById('detailsCameraFront').value = 'webcam0';
    document.getElementById('detailsCameraBack').value = 'webcam0';
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
                <p>Create your first isolated Android profile with hardware spoofing, unique IMEI/serial, and automatic Frida hooks.</p>
                <button class="btn btn-primary" onclick="showCreateDialog()">+ Create Your First Device</button>
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
                    ${device.starting ? 'Starting…' : (device.running ? 'Running' : 'Stopped')}
                </div>
            </div>

            <div class="device-details">
                <span class="device-tag">Android ${escapeHtml(device.android_version)}</span>
                <span class="device-tag">${escapeHtml(device.brand)}</span>
                ${buildSpoofTag(device)}
                ${device.launch_error && !device.running
                    ? `<span class="device-tag tag-warn tag-clickable" data-action="details" data-device="${nameAttr}" title="${escapeAttr(device.launch_error)}">⚠ Launch failed</span>`
                    : ''}
                ${device.running && device.hooks_applied === true
                    ? '<span class="device-tag tag-ok" title="Frida anti-detect hooks attached and active">✓ Hooks</span>'
                    : (device.running && device.hooks_error
                        ? `<span class="device-tag tag-warn tag-clickable" data-action="details" data-device="${nameAttr}" title="${escapeAttr(device.hooks_error)}">⚠ Hooks failed</span>`
                        : '')}
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
                    <div class="starting-banner">Booting AVD → auto-root → spoof → Frida hooks…</div>
                    <button class="btn btn-danger btn-sm" data-action="stop" data-device="${nameAttr}" ${isDisabled ? 'disabled' : ''} title="Abort launch and stop the emulator">${busy === 'stopping' ? 'Stopping…' : 'Stop'}</button>
                ` : device.running ? `
                    <button class="btn btn-primary btn-sm" data-action="spoof" data-device="${nameAttr}" ${isDisabled ? 'disabled' : ''} title="Re-apply identity spoofing and verify each prop with getprop">${busy === 'spoofing' ? 'Spoofing…' : '🎭 Spoof'}</button>
                    <button class="btn btn-warning btn-sm" data-action="hooks" data-device="${nameAttr}" ${isDisabled ? 'disabled' : ''} title="(Re)inject the anti-detect Frida hooks">${busy === 'hooking' ? 'Hooking…' : '🔧 Hooks'}</button>
                    <button class="btn btn-danger btn-sm" data-action="stop" data-device="${nameAttr}" ${isDisabled ? 'disabled' : ''} title="Shut down the emulator">${busy === 'stopping' ? 'Stopping…' : 'Stop'}</button>
                ` : `
                    <button class="btn btn-success btn-sm" data-action="launch" data-device="${nameAttr}" ${isDisabled ? 'disabled' : ''} title="Boot the emulator, then auto-root + spoof (verified) + Frida hooks, hands-free">${busy === 'launching' ? 'Launching…' : '🚀 Launch'}</button>
                `}
                <button class="btn btn-secondary btn-sm" data-action="details" data-device="${nameAttr}" title="Identity, root/spoof/hook status, verify report &amp; emulator log">📋 Details</button>
                <button class="btn btn-icon btn-icon-danger btn-sm" data-action="delete" data-device="${nameAttr}" ${isDisabled ? 'disabled' : ''} title="Delete this device and its AVD" aria-label="Delete ${nameAttr}">🗑️</button>
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
