/* The add-speaker flow: backend, device discovery, configuration. */
(function () {
    "use strict";

    var QP = window.QP;
    var escapeHtml = QP.escapeHtml;
    var escapeAttr = QP.escapeAttr;
    var showError = QP.showError;
    var qualityLabel = QP.qualityLabel;
    var qualityOptions = QP.qualityOptions;
    var deviceTypeOptions = QP.deviceTypeOptions;
    var gaplessOptions = QP.gaplessOptions;
    var checkedJson = QP.checkedJson;
    function fetchStatus() { return QP.fetchStatus.apply(null, arguments); }

    // -------------------------------------------------------------------------
    // Add speaker flow
    // -------------------------------------------------------------------------

    function showAddSpeaker() {
        QP.state.addPanelOpen = true;
        QP.state.selectedBackend = null;
        QP.state.selectedDevice = null;
        document.getElementById("add-speaker-btn").style.display = "none";

        var panel = document.getElementById("add-speaker-panel");
        panel.style.display = "";
        panel.innerHTML = renderStep1();
    }

    function hideAddSpeaker() {
        QP.state.addPanelOpen = false;
        QP.state.selectedBackend = null;
        QP.state.selectedDevice = null;
        QP.state.lastSpeakersJson = "";
        var panel = document.getElementById("add-speaker-panel");
        panel.style.display = "none";
        panel.innerHTML = "";
        document.getElementById("add-speaker-btn").style.display = "";
    }

    function renderStep1() {
        var html = '<div class="add-step-header">';
        html += '<div class="step-number">1</div>';
        html += '<span style="font-weight:600;color:#fff;">Choose backend</span>';
        html += '</div>';

        html += '<div class="backend-cards">';
        html += '<div class="backend-card" id="bc-dlna" onclick="selectBackend(\'dlna\')">';
        html += '<h3>DLNA</h3><p>Sonos, Denon HEOS, and other UPnP/DLNA renderers</p>';
        html += '</div>';
        html += '<div class="backend-card" id="bc-local" onclick="selectBackend(\'local\')">';
        html += '<h3>Local</h3><p>Built-in speakers or headphones via PortAudio</p>';
        html += '</div>';
        html += '</div>';

        html += '<div style="text-align:right;">';
        html += '<button class="button-secondary" onclick="hideAddSpeaker()">Cancel</button>';
        html += '</div>';
        return html;
    }

    function selectBackend(type) {
        QP.state.selectedBackend = type;
        var panel = document.getElementById("add-speaker-panel");

        if (type === "dlna") {
            panel.innerHTML = renderStep2DLNA();
            startDLNADiscovery();
        } else if (type === "local") {
            panel.innerHTML = renderStep2Local();
            startAudioDeviceDiscovery();
        }
    }

    function renderStep2DLNA() {
        var html = '<div class="add-step-header">';
        html += '<div class="step-number">2</div>';
        html += '<span style="font-weight:600;color:#fff;">Select DLNA device</span>';
        html += '</div>';

        html += '<div class="scan-status">';
        html += '<span id="scan-status-text">Scanning...</span>';
        html += '<div class="scan-actions">';
        html += '<button id="rescan-btn" class="manual-entry-link" onclick="startDLNADiscovery()" disabled>Rescan</button>';
        html += '<button class="manual-entry-link" onclick="selectManualDevice()">Enter URL manually</button>';
        html += '</div>';
        html += '</div>';

        html += '<div id="device-list" class="device-list"></div>';

        html += '<div style="text-align:right;">';
        html += '<button class="button-secondary" onclick="showAddSpeaker()">Back</button>';
        html += '</div>';
        return html;
    }

    function renderStep2Local() {
        var html = '<div class="add-step-header">';
        html += '<div class="step-number">2</div>';
        html += '<span style="font-weight:600;color:#fff;">Select audio device</span>';
        html += '</div>';

        html += '<div class="scan-status">';
        html += '<span id="scan-status-text">Scanning...</span>';
        html += '<button class="manual-entry-link" onclick="selectManualDevice()">Enter device name manually</button>';
        html += '</div>';

        html += '<div id="device-list" class="device-list"></div>';

        html += '<div style="text-align:right;">';
        html += '<button class="button-secondary" onclick="showAddSpeaker()">Back</button>';
        html += '</div>';
        return html;
    }

    function startDLNADiscovery() {
        var statusEl = document.getElementById("scan-status-text");
        var listEl = document.getElementById("device-list");
        var rescanBtn = document.getElementById("rescan-btn");
        if (statusEl) statusEl.textContent = "Scanning...";
        if (listEl) {
            listEl.innerHTML = "";
            listEl.removeAttribute("data-devices");
        }
        if (rescanBtn) rescanBtn.disabled = true;

        fetch("api/discover/dlna", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ timeout: 5 }),
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var statusEl = document.getElementById("scan-status-text");
                var listEl = document.getElementById("device-list");
                var rescanBtn = document.getElementById("rescan-btn");
                if (!statusEl || !listEl) return;

                var devices = data.devices || [];
                statusEl.textContent = devices.length + " device" + (devices.length !== 1 ? "s" : "") + " found";

                if (devices.length === 0) {
                    listEl.innerHTML = '<p class="muted" style="margin:0;">No DLNA devices found. Try entering the URL manually.</p>';
                } else {
                    var html = "";
                    for (var i = 0; i < devices.length; i++) {
                        var d = devices[i];
                        var encoded = escapeHtml(JSON.stringify(d).replace(/'/g, "&#39;"));
                        html += '<div class="device-item" id="di-' + i + '" onclick="selectDLNADevice(' + i + ')">';
                        html += '<div class="device-item-name">' + escapeHtml(d.friendly_name || d.name || "Unknown") + '</div>';
                        if (d.location || d.url) {
                            html += '<div class="device-item-detail">' + escapeHtml(d.location || d.url) + '</div>';
                        }
                        html += '</div>';
                    }
                    listEl.innerHTML = html;
                    listEl.setAttribute("data-devices", JSON.stringify(devices));
                }
                if (rescanBtn) rescanBtn.disabled = false;
            })
            .catch(function (err) {
                var statusEl = document.getElementById("scan-status-text");
                var rescanBtn = document.getElementById("rescan-btn");
                if (statusEl) statusEl.textContent = "Discovery failed";
                if (rescanBtn) rescanBtn.disabled = false;
            });
    }

    function startAudioDeviceDiscovery() {
        fetch("api/discover/audio-devices")
            .then(function (r) {
                if (r.status === 404) throw new Error("not_supported");
                return r.json();
            })
            .then(function (data) {
                var statusEl = document.getElementById("scan-status-text");
                var listEl = document.getElementById("device-list");
                if (!statusEl || !listEl) return;

                var devices = data.devices || [];
                statusEl.textContent = devices.length + " device" + (devices.length !== 1 ? "s" : "") + " found";

                if (devices.length === 0) {
                    listEl.innerHTML = '<p class="muted" style="margin:0;">No audio devices found.</p>';
                    return;
                }

                var html = "";
                for (var i = 0; i < devices.length; i++) {
                    var d = devices[i];
                    html += '<div class="device-item" id="di-' + i + '" onclick="selectLocalDevice(' + i + ')">';
                    html += '<div class="device-item-name">' + escapeHtml(d.name || "Unknown") + '</div>';
                    if (d.info) {
                        html += '<div class="device-item-detail">' + escapeHtml(d.info) + '</div>';
                    }
                    html += '</div>';
                }
                listEl.innerHTML = html;
                listEl.setAttribute("data-devices", JSON.stringify(devices));
            })
            .catch(function (err) {
                var statusEl = document.getElementById("scan-status-text");
                var listEl = document.getElementById("device-list");
                if (err.message === "not_supported") {
                    if (statusEl) statusEl.textContent = "Not available";
                    if (listEl) listEl.innerHTML = '<p class="muted" style="margin:0;">Local audio backend not installed. Use manual entry.</p>';
                } else {
                    if (statusEl) statusEl.textContent = "Discovery failed";
                }
            });
    }

    function selectDLNADevice(idx) {
        var listEl = document.getElementById("device-list");
        if (!listEl) return;
        var devices = JSON.parse(listEl.getAttribute("data-devices") || "[]");
        var d = devices[idx];
        if (!d) return;

        // Highlight selection
        var items = listEl.querySelectorAll(".device-item");
        for (var i = 0; i < items.length; i++) items[i].classList.remove("selected");
        var el = document.getElementById("di-" + idx);
        if (el) el.classList.add("selected");

        QP.state.selectedDevice = d;
        showConfigForm("dlna", d);
    }

    function selectLocalDevice(idx) {
        var listEl = document.getElementById("device-list");
        if (!listEl) return;
        var devices = JSON.parse(listEl.getAttribute("data-devices") || "[]");
        var d = devices[idx];
        if (!d) return;

        var items = listEl.querySelectorAll(".device-item");
        for (var i = 0; i < items.length; i++) items[i].classList.remove("selected");
        var el = document.getElementById("di-" + idx);
        if (el) el.classList.add("selected");

        QP.state.selectedDevice = d;
        showConfigForm("local", d);
    }

    function selectManualDevice() {
        showConfigForm(QP.state.selectedBackend, null);
    }

    function showConfigForm(backend, device) {
        var panel = document.getElementById("add-speaker-panel");
        if (!panel) return;

        var html = '<div class="add-step-header">';
        html += '<div class="step-number">3</div>';
        html += '<span style="font-weight:600;color:#fff;">Configure speaker</span>';
        html += '</div>';

        var rawName = device ? (device.friendly_name || "") : "";
        // If friendly_name looks like it contains an IP, prefer model_name
        var defaultName = (/\d+\.\d+\.\d+\.\d+/.test(rawName) && device && device.model_name)
            ? device.model_name : rawName;
        var defaultIp = device ? (device.ip || "") : "";
        var defaultPort = device ? (device.port || 1400) : 1400;
        var defaultUrl = device ? (device.location || "") : "";
        var defaultDevice = device ? (device.name || "") : "";

        html += '<div class="form-group">';
        html += '<label>Speaker name</label>';
        html += '<input type="text" id="new-speaker-name" value="' + escapeHtml(defaultName) + '" placeholder="My Speaker">';
        html += '</div>';

        if (backend === "dlna") {
            html += '<div class="form-row">';
            html += '<div class="form-group" style="flex:2;"><label>IP Address</label>';
            html += '<input type="text" id="new-dlna-ip" value="' + escapeHtml(defaultIp) + '" placeholder="192.168.1.50"></div>';
            html += '<div class="form-group" style="flex:1;"><label>Port</label>';
            html += '<input type="text" id="new-dlna-port" value="' + defaultPort + '"></div>';
            html += '</div>';
            html += '<div class="form-group">';
            html += '<label>Description URL <span style="color:#666">(optional — auto-discovered if empty)</span></label>';
            html += '<input type="text" id="new-dlna-url" value="' + escapeHtml(defaultUrl) + '" placeholder="Leave empty for auto-discovery">';
            html += '</div>';
            html += '<div class="form-group"><label><input type="checkbox" id="new-fixed-vol" style="width:auto;display:inline;margin-right:6px;"> Fixed volume</label></div>';
            html += '<div class="form-group">';
            html += '<label>Gapless playback</label>';
            html += '<select id="new-gapless">' + gaplessOptions("auto") + '</select>';
            html += '</div>';
        } else if (backend === "local") {
            html += '<div class="form-group">';
            html += '<label>Audio device (leave blank for default)</label>';
            html += '<input type="text" id="new-audio-device" value="' + escapeHtml(defaultDevice) + '" placeholder="default">';
            html += '</div>';
            html += '<div class="form-group"><label><input type="checkbox" id="new-fixed-vol" style="width:auto;display:inline;margin-right:6px;"> Fixed volume (output stays at 100%)</label></div>';
        }

        html += '<div class="form-group">';
        html += '<label>Max quality</label>';
        html += '<select id="new-quality">' + qualityOptions("auto") + '</select>';
        html += '</div>';

        html += '<div class="form-group">';
        html += '<label>Device type (icon in the Qobuz app)</label>';
        html += '<select id="new-device-type">' + deviceTypeOptions("speaker") + '</select>';
        html += '</div>';

        html += '<div class="button-group">';
        html += '<button id="add-speaker-submit" onclick="submitAddSpeaker()">Add Speaker</button>';
        html += '<button class="button-secondary" onclick="' + (backend === "dlna" ? "selectBackend(\'dlna\')" : "selectBackend(\'local\')") + '">Back</button>';
        html += '<button class="button-secondary" onclick="hideAddSpeaker()">Cancel</button>';
        html += '</div>';

        panel.innerHTML = html;
    }

    function submitAddSpeaker() {
        var nameEl = document.getElementById("new-speaker-name");
        var name = nameEl ? nameEl.value.trim() : "";
        if (!name) {
            showError("Speaker name is required.");
            return;
        }

        var quality = document.getElementById("new-quality");
        var deviceType = document.getElementById("new-device-type");
        var payload = {
            name: name,
            backend: QP.state.selectedBackend,
            max_quality: quality ? quality.value : "auto",
            device_type: deviceType ? deviceType.value : "speaker",
        };

        if (QP.state.selectedBackend === "dlna") {
            var ipEl = document.getElementById("new-dlna-ip");
            var ip = ipEl ? ipEl.value.trim() : "";
            if (!ip) {
                showError("IP address is required.");
                return;
            }
            payload.dlna_ip = ip;
            payload.dlna_port = parseInt(document.getElementById("new-dlna-port").value) || 1400;
            var urlEl = document.getElementById("new-dlna-url");
            payload.description_url = urlEl ? urlEl.value.trim() : "";
            var gaplessEl = document.getElementById("new-gapless");
            if (gaplessEl) payload.gapless = gaplessEl.value;
            // The renderer's UDN lets the proxy find it again if its IP changes;
            // only valid while the address still points at the found device.
            var found = QP.state.selectedDevice;
            if (found && found.udn && found.ip === ip) payload.dlna_udn = found.udn;
        } else if (QP.state.selectedBackend === "local") {
            var devEl = document.getElementById("new-audio-device");
            payload.audio_device = devEl ? devEl.value.trim() : "";
        }
        var fixedVolEl = document.getElementById("new-fixed-vol");
        payload.fixed_volume = fixedVolEl ? fixedVolEl.checked : false;

        var submitBtn = document.getElementById("add-speaker-submit");
        if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.textContent = "Adding…";
        }

        fetch("api/speakers", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        })
            .then(function (r) {
                if (!r.ok) {
                    return r.json().then(function (d) { throw new Error(d.error || "Failed to add speaker"); });
                }
                return r.json();
            })
            .then(function () {
                hideAddSpeaker();
                QP.state.lastSpeakersJson = null;
                fetchStatus();
            })
            .catch(function (err) {
                if (submitBtn) {
                    submitBtn.disabled = false;
                    submitBtn.textContent = "Add Speaker";
                }
                showError(err.message);
            });
    }

    window.showAddSpeaker = showAddSpeaker;
    window.hideAddSpeaker = hideAddSpeaker;
    window.selectBackend = selectBackend;
    window.selectDLNADevice = selectDLNADevice;
    window.selectLocalDevice = selectLocalDevice;
    window.selectManualDevice = selectManualDevice;
    window.startDLNADiscovery = startDLNADiscovery;
    window.submitAddSpeaker = submitAddSpeaker;
})();
