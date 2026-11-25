/* Web UI entry point: authentication, status polling, system info. */
(function () {
    "use strict";

    var QP = window.QP;
    var pollTimer = null;

    // -------------------------------------------------------------------------
    // Auth
    // -------------------------------------------------------------------------

    function showAuthState(state) {
        document.getElementById("auth-disconnected").style.display = "none";
        document.getElementById("auth-connected").style.display = "none";

        if (state === "disconnected") {
            document.getElementById("auth-disconnected").style.display = "";
            document.getElementById("add-speaker-btn").style.display = "none";
        } else if (state === "connected") {
            document.getElementById("auth-connected").style.display = "";
            if (!QP.state.addPanelOpen) {
                document.getElementById("add-speaker-btn").style.display = "";
            }
        }
    }

    function startLogin() {
        // Use the document base (honors HA ingress <base href>) so the OAuth
        // callback returns to this exact UI, prefix and all. Strip the trailing
        // slash so the server can append "/auth/callback".
        var base = document.baseURI.replace(/\/$/, "");
        var url = "auth/login?origin=" + encodeURIComponent(base);
        // Behind Home Assistant ingress the UI runs in an iframe. Qobuz (and the
        // third-party providers on its sign-in page, e.g. Google) refuse to be
        // framed, AND the OAuth callback can't return through ingress (HA's
        // ingress_session cookie isn't sent on the cross-site return from Qobuz,
        // so the callback 401s). So run login against the add-on's DIRECT port
        // in a new top-level tab — host networking exposes it and /data (the
        // saved token) is shared with this panel, which flips to "connected" via
        // the /api/status poll once login completes.
        if (window.self !== window.top) {
            var port = window.QOBUZ_DIRECT_PORT || "8689";
            var direct = window.location.protocol + "//" + window.location.hostname + ":" + port;
            window.open(direct + "/auth/login?origin=" + encodeURIComponent(direct), "_blank");
        } else {
            window.location.href = url;
        }
    }

    function logout() {
        fetch("api/auth/logout", { method: "POST" })
            .then(function () {
                showAuthState("disconnected");
                QP.state.lastSpeakersJson = null;
                document.getElementById("speakers-list").innerHTML =
                    '<p class="muted">Waiting for authentication...</p>';
            })
            .catch(function () {
                showAuthState("disconnected");
            });
    }

    // -------------------------------------------------------------------------
    // System info
    // -------------------------------------------------------------------------

    function updateSystemInfo(system) {
        if (!system) return;
        var versionLabel = system.version ? "v" + system.version : "--";
        if (system.version && system.commit) {
            versionLabel += " (" + system.commit + ")";
        }
        document.getElementById("system-version").textContent = versionLabel;
        document.getElementById("system-uptime").textContent = system.uptime || "--";
    }

    // -------------------------------------------------------------------------
    // Polling
    // -------------------------------------------------------------------------

    function fetchStatus() {
        fetch("api/status")
            .then(function (response) {
                if (!response.ok) throw new Error("HTTP " + response.status);
                return response.json();
            })
            .then(function (data) {
                var auth = data.auth || {};

                if (auth.authenticated) {
                    var displayName = auth.name || auth.email || "User " + auth.user_id;
                    document.getElementById("auth-name").textContent = displayName;
                    document.getElementById("auth-email").textContent = auth.email && auth.name ? auth.email : "";
                    var avatarEl = document.getElementById("auth-avatar");
                    if (auth.avatar) {
                        avatarEl.src = auth.avatar;
                        avatarEl.style.display = "";
                    } else {
                        avatarEl.style.display = "none";
                    }
                    showAuthState("connected");
                } else {
                    showAuthState("disconnected");
                }

                if (auth.authenticated && data.speakers) {
                    QP.updateSpeakers(data.speakers);
                } else if (!auth.authenticated) {
                    QP.state.lastSpeakersJson = null;
                    document.getElementById("speakers-list").innerHTML =
                        '<p class="muted">Waiting for authentication...</p>';
                }

                updateSystemInfo({ version: data.version, commit: data.commit, uptime: data.uptime });
            })
            .catch(function () {
                // Silently ignore fetch errors (server may be restarting)
            });
    }

    QP.fetchStatus = fetchStatus;
    window.startLogin = startLogin;
    window.logout = logout;

    // Show OAuth error if redirected back with one
    (function checkOAuthError() {
        var params = new URLSearchParams(window.location.search);
        var error = params.get("error");
        if (error) {
            var messages = {
                missing_code: "Login was cancelled or the authorization code was missing.",
                exchange_failed: "Failed to exchange authorization code. Please try again.",
                auth_failed: "Authentication failed. Please try again.",
            };
            var errorEl = document.getElementById("login-error");
            errorEl.textContent = messages[error] || "Login failed. Please try again.";
            errorEl.style.display = "";
            // Clean up URL
            window.history.replaceState({}, "", "/");
        }
    })();

    // Start polling on page load
    fetchStatus();
    pollTimer = setInterval(fetchStatus, 3000);
})();
