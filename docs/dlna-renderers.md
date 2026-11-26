# DLNA renderers: discovery and behaviour

DLNA renderers say little about themselves, and what they say is often
incomplete. This page describes how qobuz-proxy finds a renderer, how it
decides what the renderer can do, and how to see or override those decisions.

## Discovery

The web UI's scan, `qobuz-proxy --discover` and the automatic lookups all use
the same SSDP search (`qobuz_proxy/backends/dlna/discovery.py`):

- The M-SEARCH goes out on the LAN interface: the configured `bind_address`,
  else the address of the default route, skipping VPN tunnels and container
  bridges.
- It is sent twice, for both `MediaRenderer:1` and `AVTransport:1`, because
  SSDP is UDP and some renderers answer only one search target.
- Answers are grouped by UDN, so a renderer is listed once.
- Hosts in `QOBUZPROXY_DLNA_HOSTS` (comma-separated `ip`, `ip:port` or a
  description URL) get a unicast search, and a direct description fetch when a
  port or URL is given. Use this where multicast does not arrive (Docker bridge
  networks, VLANs).
- A renderer seen in an earlier scan but silent in this one is checked through
  its description URL before it is dropped from the list.

Each speaker stores its renderer's UDN (`dlna_udn`), learned on the first
connect. If the renderer does not answer at its saved address, or a different
device answers there, qobuz-proxy searches for the UDN and, when found,
connects to the new address and saves it.

The device description is read from the device that holds the AVTransport
service (the embedded MediaRenderer on Sonos). Relative control URLs resolve
against `URLBase` or the description URL.

## What a renderer can play

`max_quality: auto` is resolved in this order:

1. Sample rates and bit depths in the renderer's FLAC entries
   (`GetProtocolInfo`).
2. Rates and depths of the PCM formats it lists (`audio/L24;rate=192000`),
   when the FLAC entries give none.
3. A built-in model profile (`qobuz_proxy/backends/dlna/quirks.py`).
4. CD quality (16/44.1).

Sonos is capped at 16/48. Bare Sink entries (`http-get:*:audio/flac:*`) are
sent back with `DLNA.ORG_OP=01`: without it, some renderers treat the stream
as live and refuse Pause and Seek.

## How a renderer behaves

Renderer behaviour comes in layers, each overriding the one before:

| Layer | Source |
|---|---|
| Spec defaults | What the UPnP AVTransport spec promises |
| Service description | `SetNextAVTransportURI` missing from the AVTransport SCPD turns gapless off |
| Model profile | Built-in table: a brand anywhere in the device identity (manufacturer, model name, friendly name, UDN) plus the model in its model or friendly name (FiiO K17, FiiO SR11, Bose SoundTouch). Vendors shipping stock renderers keep the stock manufacturer: a FiiO K17 reports gmediarender's authors. |
| Learned | What this renderer was seen to do, saved per UDN in `dlna_quirks.json` in the data directory |
| User | The speaker's gapless setting (`auto`, `on`, `off`) |

A next track is queued only once the renderer is heard playing the current
one (position 5 s, or PLAYING for 5 s). That does not help renderers that
replace the playing track whenever one is queued (the FiiO K17 does so even
20 s in); for those, gapless must be off, which their profile or the first
early switch takes care of. A cleared next track is also cleared on the
renderer with an empty `SetNextAVTransportURI`, since the K17 otherwise keeps
it through Stop and later tracks.

What is learned at runtime:

| Seen | Learned | Effect |
|---|---|---|
| `SetNextAVTransportURI` refused (401/602) | `gapless=False` | Each track is started on its own |
| Stopped at a track end with the next track queued for 10 s or more | `gapless=False` | As above |
| Jumped to the queued track early (position back to the start, and the renderer fetched the queued track from the proxy) | `gapless=False` | The queued URI is cleared on the renderer and the current track is reloaded at its position |
| Stayed PLAYING with the position parked at the end | `stops_at_end=False` | Track ends are detected from the stalled position |
| Named the queued track as current while still playing the current one | `reports_next_early=True` | A track change counts only when the position returns to the start |
| Refused Pause (401/602) | `can_pause=False` | Pause stops the renderer; resume reloads the track at its position |

The edit form in the web UI shows the effective behaviour and where each value
came from. **Re-detect** forgets what was learned for that renderer.

## End of a track

A track ends when the renderer reports STOPPED near the end, or, for
renderers that never do (the FiiO K17 stays PLAYING with its position parked
at the track's length, even when the stream's length is known), when the
position stops moving:

| Position | Evidence | Wait |
|---|---|---|
| At or past the length | the proxy sent the whole file, or the renderer is known not to stop | 1 s |
| Within 2 s of the length | the proxy sent the whole file | 2 s |
| Within 2 s of the length | the renderer is known not to stop | 3 s |
| Within 2 s of the length | none, or a next track is queued | 6 s |
| 4 s past the length | — | at once |

With gapless off, this wait is most of the gap between tracks, which is why it
is short where the evidence allows it.

The check never runs while paused, in the first seconds of a track, or just
after a seek. Only one end is reported per playback, however the renderer
reports it. A failed state read (timeout, Wi-Fi blip) keeps the last known
state instead of counting as a stop.

## Polling and events

The backend polls the transport every 2 s (every 0.5 s near the end of a
track). Where the renderer supports UPnP eventing, qobuz-proxy subscribes to
AVTransport `LastChange` events, delivered to the audio proxy port, and a
state change wakes the poll at once. Subscriptions are renewed, and re-made
with back-off when a renderer stops answering. Polling stays on as the backup.

## Streams

The audio proxy answers HEAD and GET with `Content-Length` (kept even when the
CDN switches to chunked transfer, once the size is known), supports `Range`,
and answers `getcontentFeatures.dlna.org` and `transferMode.dlna.org`. The
DIDL-Lite `<res>` of a queued track carries its `size`. At debug log level
each renderer request is logged with its method, range, status and length.
