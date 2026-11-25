/* Speaker cards, the edit form, and edit / remove actions. */
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
    // Speaker rendering
    // -------------------------------------------------------------------------

    function renderSpeakerHeader(s) {
        var html = '<div class="speaker-header">';
        html += '<span class="speaker-name" style="font-weight:500;color:#fff;font-size:14px;">' + escapeHtml(s.name) + '</span>';

        // Status badge
        var state = (s.status || "idle").toLowerCase();
        var badgeClass = "badge-idle";
        var badgeLabel = "Idle";
        if (state === "disconnected") {
            badgeClass = "badge-disconnected";
            badgeLabel = "Disconnected";
        } else if (state === "starting") {
            badgeClass = "badge-starting";
            badgeLabel = "Starting";
        } else if (state === "playing") {
            badgeClass = "badge-playing";
            badgeLabel = "Playing";
        } else if (state === "paused") {
            badgeClass = "badge-paused";
            badgeLabel = "Paused";
        } else if (state === "idle" || state === "stopped") {
            badgeClass = "badge-idle";
            badgeLabel = "Idle";
        }
        html += '<span class="speaker-badge ' + badgeClass + '">' + badgeLabel + '</span>';

        // Backend badge
        if (s.backend) {
            var backendClass = s.backend === "dlna" ? "badge-dlna" : "badge-local";
            html += '<span class="speaker-badge ' + backendClass + '">' + escapeHtml(s.backend.toUpperCase()) + '</span>';
        }

        html += '</div>';
        return html;
    }

    function renderActions(s) {
        var html = '<div class="speaker-actions">';
        var idArg = escapeAttr(JSON.stringify(s.id));
        html += '<button onclick="editSpeaker(' + idArg + ')">Edit</button>';
        html += '</div>';
        return html;
    }

    function renderSpeakerCard(s) {
        var state = (s.status || "idle").toLowerCase();
        var np = s.now_playing;
        var cfg = s.config || {};
        var isActive = np && (state === "playing" || state === "paused");

        var html = '<div class="speaker-card">';

        if (isActive) {
            html += '<div class="speaker-card-playing">';

            // Album art
            if (np.album_art_url) {
                html += '<img class="speaker-album-art" src="' + escapeHtml(np.album_art_url) + '" alt="Album art">';
            } else {
                html += '<div class="speaker-album-art-placeholder">&#9835;</div>';
            }

            html += '<div class="speaker-info">';
            html += renderSpeakerHeader(s);

            if (np.title) {
                html += '<div class="speaker-track">' + escapeHtml(np.title) + '</div>';
            }
            var artistAlbum = [];
            if (np.artist) artistAlbum.push(escapeHtml(np.artist));
            if (np.album) artistAlbum.push(escapeHtml(np.album));
            if (artistAlbum.length) {
                html += '<div class="speaker-artist-album">' + artistAlbum.join(' &mdash; ') + '</div>';
            }

            var meta = [];
            if (np.quality) meta.push(escapeHtml(np.quality));
            if (cfg.fixed_volume) {
                meta.push('Fixed volume');
            } else if (np.volume !== undefined) {
                meta.push('Vol ' + np.volume + '%');
            }
            if (meta.length) {
                html += '<div class="speaker-meta">' + meta.join(' · ') + '</div>';
            }

            html += '</div>'; // speaker-info
            html += renderActions(s);
            html += '</div>'; // speaker-card-playing
        } else {
            // Idle / disconnected layout
            html += '<div style="display:flex;align-items:flex-start;gap:8px;">';
            html += '<div style="flex:1;min-width:0;">';
            html += renderSpeakerHeader(s);

            var idleParts = [];
            if (s.backend === "dlna" && cfg.dlna_ip) {
                idleParts.push(escapeHtml(cfg.dlna_ip + ':' + (cfg.dlna_port || 1400)));
            } else if (s.backend === "local" && cfg.audio_device) {
                idleParts.push(escapeHtml(cfg.audio_device));
            }
            if (cfg.effective_quality) {
                var qText = qualityLabel(cfg.effective_quality);
                if (cfg.max_quality === "auto") {
                    qText += " (auto)";
                }
                idleParts.push(escapeHtml(qText));
            }
            if (idleParts.length) {
                html += '<div class="speaker-idle-info">' + idleParts.join(' · ') + '</div>';
            }
            if (cfg.quality_source === "auto_fallback" && state !== "disconnected") {
                html += '<div class="speaker-idle-info" style="color:#e0a030;">' +
                    'Couldn\'t detect this device\'s hi-res support. ' +
                    'Pick a quality in Edit if it supports better than CD.</div>';
            }

            html += '</div>'; // flex child
            html += renderActions(s);
            html += '</div>';
        }

        html += '</div>'; // speaker-card
        return html;
    }

    // What the renderer is assumed or known to do (profile / learned / user),
    // so users can see why gapless is on or off, and re-detect it.
    function renderBehaviour(s) {
        var cfg = s.config || {};
        if (!cfg.renderer_behaviour) return "";
        var html = '<div class="form-group">';
        html += '<label>Renderer behaviour</label>';
        html += '<div class="muted" style="font-size:12px;">' + escapeHtml(cfg.renderer_behaviour) + '</div>';
        html += '<button class="button-secondary" style="margin-top:6px;" onclick="forgetQuirks(' +
            escapeAttr(JSON.stringify(s.id)) + ')">Re-detect</button>';
        html += '</div>';
        return html;
    }

    function forgetQuirks(id) {
        fetch("api/speakers/" + encodeURIComponent(id) + "/forget-quirks", { method: "POST" })
            .then(function (r) { return checkedJson(r, "Failed to reset renderer behaviour"); })
            .then(function () {
                QP.state.lastSpeakersJson = null;
                QP.state.editingSpeakerId = null;
                fetchStatus();
            })
            .catch(function (err) { showError(err.message); });
    }

    function renderEditForm(s) {
        var html = '<div class="speaker-edit-card">';
        html += '<div style="font-weight:600;margin-bottom:12px;color:#fff;">Edit Speaker</div>';

        html += '<div class="form-group">';
        html += '<label>Name</label>';
        html += '<input type="text" id="edit-name" value="' + escapeHtml(s.name) + '">';
        html += '</div>';

        var cfg = s.config || {};
        if (s.backend === "dlna") {
            html += '<div class="form-group">';
            html += '<label>IP Address</label>';
            html += '<input type="text" id="edit-dlna-ip" value="' + escapeHtml(cfg.dlna_ip || "") + '" placeholder="192.168.1.x">';
            html += '</div>';
            html += '<div class="form-group">';
            html += '<label>Port</label>';
            html += '<input type="number" id="edit-dlna-port" value="' + escapeHtml(String(cfg.dlna_port || 1400)) + '">';
            html += '</div>';
            html += '<div class="form-group">';
            html += '<label>Description URL (optional)</label>';
            html += '<input type="text" id="edit-dlna-url" value="' + escapeHtml(cfg.description_url || "") + '" placeholder="http://192.168.1.x:1400/xml/device_description.xml">';
            html += '</div>';
            html += '<div class="form-group"><label><input type="checkbox" id="edit-fixed-vol" style="width:auto;display:inline;margin-right:6px;"' + (cfg.fixed_volume ? ' checked' : '') + '> Fixed volume</label></div>';
            html += '<div class="form-group">';
            html += '<label>Gapless playback</label>';
            html += '<select id="edit-gapless">' + gaplessOptions(cfg.gapless) + '</select>';
            html += '</div>';
            html += renderBehaviour(s);
        } else if (s.backend === "local") {
            html += '<div class="form-group">';
            html += '<label>Audio Device (leave blank for default)</label>';
            html += '<input type="text" id="edit-audio-device" value="' + escapeHtml(cfg.audio_device || "") + '" placeholder="default">';
            html += '</div>';
            html += '<div class="form-group"><label><input type="checkbox" id="edit-fixed-vol" style="width:auto;display:inline;margin-right:6px;"' + (cfg.fixed_volume ? ' checked' : '') + '> Fixed volume (output stays at 100%)</label></div>';
        }

        html += '<div class="form-group">';
        html += '<label>Max Quality</label>';
        html += '<select id="edit-quality">' + qualityOptions(cfg.max_quality || "auto") + '</select>';
        if (cfg.quality_source === "auto_fallback") {
            html += '<div class="muted" style="font-size:12px;margin-top:4px;color:#e0a030;">' +
                'This device doesn\'t report its hi-res support, so Auto uses CD (16/44). ' +
                'Pick a quality if it supports better than CD.</div>';
        }
        html += '</div>';

        html += '<div class="form-group">';
        html += '<label>Device type (icon in the Qobuz app)</label>';
        html += '<select id="edit-device-type">' + deviceTypeOptions(cfg.device_type || "speaker") + '</select>';
        html += '</div>';

        html += '<div class="button-group">';
        html += '<button id="edit-speaker-submit" onclick="submitEditSpeaker(' + escapeAttr(JSON.stringify(s.id)) + ', ' + escapeAttr(JSON.stringify(s.backend)) + ')">Save</button>';
        html += '<button class="button-secondary" onclick="cancelEdit()">Cancel</button>';
        html += '<button class="button-danger" style="margin-left:auto;" onclick="removeSpeaker(' + escapeAttr(JSON.stringify(s.id)) + ')">Remove</button>';
        html += '</div>';

        html += '</div>';
        return html;
    }

    function updateSpeakers(speakers) {
        if (QP.state.addPanelOpen) return;

        // While editing, skip re-render only after the form is in the DOM —
        // otherwise the initial click-to-edit never gets a chance to render it.
        if (QP.state.editingSpeakerId && document.querySelector(".speaker-edit-card")) return;

        var json = JSON.stringify(speakers);
        if (json === QP.state.lastSpeakersJson) return;
        QP.state.lastSpeakersJson = json;

        var container = document.getElementById("speakers-list");

        if (!speakers || speakers.length === 0) {
            container.innerHTML = '<p class="muted">No speakers configured.</p>';
            return;
        }

        var html = "";
        for (var i = 0; i < speakers.length; i++) {
            var s = speakers[i];
            if (s.id === QP.state.editingSpeakerId) {
                html += renderEditForm(s);
            } else {
                html += renderSpeakerCard(s);
            }
        }
        container.innerHTML = html;
    }

    // -------------------------------------------------------------------------
    // Edit speaker
    // -------------------------------------------------------------------------

    function editSpeaker(id) {
        QP.state.editingSpeakerId = id;
        QP.state.lastSpeakersJson = null;
        fetchStatus();
    }

    function cancelEdit() {
        QP.state.editingSpeakerId = null;
        QP.state.lastSpeakersJson = null;
        fetchStatus();
    }

    function submitEditSpeaker(id, backend) {
        var nameEl = document.getElementById("edit-name");
        var name = nameEl ? nameEl.value.trim() : "";
        if (!name) {
            showError("Speaker name is required.");
            return;
        }

        var quality = document.getElementById("edit-quality");
        var payload = {
            name: name,
            max_quality: quality ? quality.value : "auto",
        };
        var deviceType = document.getElementById("edit-device-type");
        if (deviceType) payload.device_type = deviceType.value;

        if (backend === "dlna") {
            var ipEl = document.getElementById("edit-dlna-ip");
            var portEl = document.getElementById("edit-dlna-port");
            var urlEl = document.getElementById("edit-dlna-url");
            var ip = ipEl ? ipEl.value.trim() : "";
            if (!ip) {
                showError("IP address is required.");
                return;
            }
            payload.dlna_ip = ip;
            payload.dlna_port = portEl ? (parseInt(portEl.value) || 1400) : 1400;
            payload.description_url = urlEl ? urlEl.value.trim() : "";
            var gaplessEl = document.getElementById("edit-gapless");
            if (gaplessEl) payload.gapless = gaplessEl.value;
        } else if (backend === "local") {
            var devEl = document.getElementById("edit-audio-device");
            payload.audio_device = devEl ? devEl.value.trim() : "";
        }
        var fixedVolEl = document.getElementById("edit-fixed-vol");
        if (fixedVolEl) payload.fixed_volume = fixedVolEl.checked;

        var submitBtn = document.getElementById("edit-speaker-submit");
        if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.textContent = "Saving…";
        }

        fetch("api/speakers/" + encodeURIComponent(id), {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        })
            .then(function (r) {
                if (!r.ok) {
                    return r.json().then(function (d) { throw new Error(d.error || "Failed to update speaker"); });
                }
                return r.json();
            })
            .then(function (data) {
                QP.state.editingSpeakerId = null;
                QP.state.lastSpeakersJson = null;
                if (data && data.warning) {
                    showError(data.warning);
                }
                fetchStatus();
            })
            .catch(function (err) {
                if (submitBtn) {
                    submitBtn.disabled = false;
                    submitBtn.textContent = "Save";
                }
                showError(err.message);
            });
    }

    // -------------------------------------------------------------------------
    // Remove speaker
    // -------------------------------------------------------------------------

    function removeSpeaker(id) {
        if (!confirm("Remove this speaker?")) return;

        fetch("api/speakers/" + encodeURIComponent(id), { method: "DELETE" })
            .then(function (r) {
                if (!r.ok) {
                    return r.json().then(function (d) { throw new Error(d.error || "Failed to remove speaker"); });
                }
            })
            .then(function () {
                QP.state.editingSpeakerId = null;
                QP.state.lastSpeakersJson = null;
                fetchStatus();
            })
            .catch(function (err) {
                showError(err.message);
            });
    }

    QP.updateSpeakers = updateSpeakers;
    window.editSpeaker = editSpeaker;
    window.cancelEdit = cancelEdit;
    window.submitEditSpeaker = submitEditSpeaker;
    window.removeSpeaker = removeSpeaker;
    window.forgetQuirks = forgetQuirks;
})();
