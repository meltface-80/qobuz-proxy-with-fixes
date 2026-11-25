/* Shared state and helpers for the web UI (window.QP). */
(function () {
    "use strict";

    var QP = (window.QP = window.QP || {});

    // UI state shared by the speaker list and the add / edit forms.
    QP.state = {
        editingSpeakerId: null,
        lastSpeakersJson: null,
        addPanelOpen: false,
        selectedBackend: null,
        selectedDevice: null,
    };

    function escapeHtml(text) {
        var div = document.createElement("div");
        div.appendChild(document.createTextNode(String(text || "")));
        return div.innerHTML;
    }

    // Escape a value for embedding inside an HTML attribute. Text-node
    // escaping doesn't cover " and ', but those terminate attribute values
    // and break inline onclick handlers.
    function escapeAttr(text) {
        return String(text)
            .replace(/&/g, "&amp;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function showError(msg) {
        var el = document.getElementById("speaker-error");
        el.textContent = msg;
        el.style.display = "";
        setTimeout(function () {
            el.style.display = "none";
        }, 5000);
    }

    function qualityLabel(q) {
        var labels = { 27: "Hi-Res 192k", 7: "Hi-Res 96k", 6: "CD", 5: "MP3", auto: "Auto" };
        return labels[String(q)] || String(q || "");
    }

    function qualityOptions(selected) {
        var opts = [
            { value: "auto", label: "Auto" },
            { value: "27", label: "Hi-Res 192k" },
            { value: "7", label: "Hi-Res 96k" },
            { value: "6", label: "CD" },
            { value: "5", label: "MP3" },
        ];
        var html = "";
        for (var i = 0; i < opts.length; i++) {
            var sel = String(selected) === String(opts[i].value) ? ' selected' : '';
            html += '<option value="' + opts[i].value + '"' + sel + '>' + opts[i].label + '</option>';
        }
        return html;
    }

    function deviceTypeOptions(selected) {
        // Keys of DEVICE_TYPES in config.py; the type picks the Qobuz app icon.
        var opts = [
            { value: "speaker", label: "Speaker" },
            { value: "streamer", label: "Streamer" },
            { value: "soundbar", label: "Soundbar" },
            { value: "headphones", label: "Headphones" },
            { value: "tv", label: "TV" },
            { value: "computer", label: "Computer" },
            { value: "mobile", label: "Mobile" },
            { value: "tablet", label: "Tablet" },
            { value: "cast", label: "Cast" },
        ];
        var html = "";
        for (var i = 0; i < opts.length; i++) {
            var sel = String(selected) === opts[i].value ? ' selected' : '';
            html += '<option value="' + opts[i].value + '"' + sel + '>' + opts[i].label + '</option>';
        }
        return html;
    }

    function gaplessOptions(selected) {
        // Speaker "gapless" setting: auto lets the renderer's profile and what
        // it was seen to do decide (see backends/dlna/quirks.py).
        var opts = [
            { value: "auto", label: "Auto (detect)" },
            { value: "on", label: "On" },
            { value: "off", label: "Off" },
        ];
        var html = "";
        for (var i = 0; i < opts.length; i++) {
            var sel = String(selected || "auto") === opts[i].value ? ' selected' : '';
            html += '<option value="' + opts[i].value + '"' + sel + '>' + opts[i].label + '</option>';
        }
        return html;
    }

    // Reject with the server's error message for a non-2xx response.
    function checkedJson(r, fallback) {
        if (!r.ok) {
            return r.json().then(
                function (d) { throw new Error(d.error || fallback); },
                function () { throw new Error(fallback + " (HTTP " + r.status + ")"); }
            );
        }
        return r.status === 204 ? null : r.json();
    }

    QP.escapeHtml = escapeHtml;
    QP.escapeAttr = escapeAttr;
    QP.showError = showError;
    QP.qualityLabel = qualityLabel;
    QP.qualityOptions = qualityOptions;
    QP.deviceTypeOptions = deviceTypeOptions;
    QP.gaplessOptions = gaplessOptions;
    QP.checkedJson = checkedJson;
})();
