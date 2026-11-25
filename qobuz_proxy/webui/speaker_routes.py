"""Speaker management API routes: CRUD and discovery."""

import logging
from typing import Any, Awaitable, Callable

from aiohttp import web

from qobuz_proxy.backends.dlna.discovery import discover_dlna_devices
from qobuz_proxy.webui.config_writer import ConfigSaveError

logger = logging.getLogger(__name__)

# Longest SSDP search the web UI may ask for.
MAX_DISCOVERY_TIMEOUT_SECONDS = 15.0


async def _read_json(request: web.Request) -> Any:
    try:
        return await request.json()
    except Exception:
        return None


def _error(message: str, status: int) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def _call(
    callback: Callable[..., Awaitable[Any]],
    *args: Any,
    ok_status: int = 200,
    no_content: bool = False,
) -> web.Response:
    """Run a speaker callback and map its errors to HTTP statuses."""
    try:
        result = await callback(*args)
    except KeyError:
        return _error("speaker not found", 404)
    except ValueError as e:
        return _error(str(e), 400)
    except ConfigSaveError as e:
        # The change was rolled back: say so instead of reporting success.
        return _error(str(e), 500)
    if no_content:
        return web.Response(status=204)
    return web.json_response(result, status=ok_status)


async def _handle_discover_dlna(request: web.Request) -> web.Response:
    """Trigger SSDP discovery and return found DLNA devices."""
    body = await _read_json(request) or {}
    try:
        timeout = float(body.get("timeout", 5))
    except (TypeError, ValueError):
        return _error("invalid timeout", 400)
    timeout = max(1.0, min(timeout, MAX_DISCOVERY_TIMEOUT_SECONDS))
    devices = await discover_dlna_devices(
        timeout=timeout, bind_address=request.app.get("bind_address", "0.0.0.0")
    )
    return web.json_response(
        {
            "devices": [
                {
                    "friendly_name": d.friendly_name,
                    "ip": d.ip,
                    "port": d.port,
                    "model_name": d.model_name,
                    "manufacturer": d.manufacturer,
                    "udn": d.udn,
                    "location": d.location,
                }
                for d in devices
            ],
            "count": len(devices),
        }
    )


async def _handle_discover_audio_devices(request: web.Request) -> web.Response:
    """List local audio output devices. Returns 404 if disabled."""
    if not request.app.get("local_audio_enabled", False):
        return _error("local_audio_ui_disabled", 404)

    try:
        from qobuz_proxy.backends.local.device import list_audio_devices

        devices = list_audio_devices()
    except ImportError:
        return _error("local_audio_dependencies_missing", 404)
    return web.json_response(
        {
            "devices": [
                {
                    "name": d.name,
                    "index": d.index,
                    "channels": d.channels,
                    "sample_rate": int(d.default_samplerate),
                    "is_default": d.is_default,
                }
                for d in devices
            ],
        }
    )


async def _handle_get_speakers(request: web.Request) -> web.Response:
    """Return all speakers with config and live status."""
    return web.json_response(request.app["get_speakers"]())


def _check_dlna_ip(body: dict, required: bool) -> str:
    """Error message for a missing or empty DLNA IP, else ""."""
    if required and not str(body.get("dlna_ip") or "").strip():
        return "dlna_ip is required for DLNA backend"
    if "dlna_ip" in body and not str(body.get("dlna_ip") or "").strip():
        return "dlna_ip must not be empty"
    return ""


async def _handle_add_speaker(request: web.Request) -> web.Response:
    """Add a new speaker."""
    body = await _read_json(request)
    if not isinstance(body, dict):
        return _error("invalid_json", 400)
    if not str(body.get("name") or "").strip():
        return _error("name is required", 400)
    problem = _check_dlna_ip(body, required=body.get("backend", "dlna") == "dlna")
    if problem:
        return _error(problem, 400)
    return await _call(request.app["on_add_speaker"], body, ok_status=201)


async def _handle_edit_speaker(request: web.Request) -> web.Response:
    """Edit an existing speaker (validated like an added one, GitHub #54)."""
    body = await _read_json(request)
    if not isinstance(body, dict):
        return _error("invalid_json", 400)
    if "name" in body and not str(body.get("name") or "").strip():
        return _error("name must not be empty", 400)
    problem = _check_dlna_ip(body, required=False)
    if problem:
        return _error(problem, 400)
    return await _call(request.app["on_edit_speaker"], request.match_info["speaker_id"], body)


async def _handle_remove_speaker(request: web.Request) -> web.Response:
    """Remove a speaker."""
    return await _call(
        request.app["on_remove_speaker"], request.match_info["speaker_id"], no_content=True
    )


async def _handle_forget_quirks(request: web.Request) -> web.Response:
    """Forget learned renderer behaviour so it is detected again."""
    callback = request.app.get("on_forget_speaker_quirks")
    if callback is None:
        return _error("not supported", 404)
    return await _call(callback, request.match_info["speaker_id"])


def register_speaker_routes(app: web.Application) -> None:
    """Register speaker management routes."""
    app.router.add_post("/api/discover/dlna", _handle_discover_dlna)
    app.router.add_get("/api/discover/audio-devices", _handle_discover_audio_devices)
    app.router.add_get("/api/speakers", _handle_get_speakers)
    app.router.add_post("/api/speakers", _handle_add_speaker)
    app.router.add_put("/api/speakers/{speaker_id}", _handle_edit_speaker)
    app.router.add_delete("/api/speakers/{speaker_id}", _handle_remove_speaker)
    app.router.add_post("/api/speakers/{speaker_id}/forget-quirks", _handle_forget_quirks)
