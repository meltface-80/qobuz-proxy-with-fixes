"""
Timing rules for the DLNA backend: polling, start grace, gapless arming,
end-of-track detection and early-switch detection.
"""

# State polling interval
STATE_POLL_INTERVAL_SECONDS = 2.0

# In the last seconds of a track, poll faster so the natural end is noticed
# (and the next track started) sooner. The window covers the normal interval
# plus a renderer position that lags a little.
NEAR_END_POLL_INTERVAL_SECONDS = 0.5
NEAR_END_WINDOW_MS = 6_000

# Grace period after starting playback to ignore STOPPED state (seconds)
# This prevents false track-ended events while the device is loading
PLAYBACK_START_GRACE_PERIOD_SECONDS = 5.0

# A stop with more than this much of the track left is not a natural track end
# (another controller or the device's own buttons stopped it). Covers the poll
# interval plus renderers whose reported position lags a little.
EARLY_STOP_MARGIN_MS = 15_000

# Gapless arming waits until the renderer itself has been heard playing the
# current track: some renderers replace a track that is still loading with a
# NextURI that arrives during TRANSITIONING. Confirmed once the reported
# position reaches ARM_AFTER_POSITION_MS, or (for renderers that never report
# a position) after PLAYING has been seen for ARM_AFTER_PLAYING_SECONDS.
ARM_AFTER_POSITION_MS = 3_000
ARM_AFTER_PLAYING_SECONDS = 4.0

# A renderer that stays PLAYING at the end of a track (position parked at the
# track length, never STOPPED). The track counts as ended when its position is
# within END_WINDOW_MS of the length and has not moved for:
#   END_STALL_DELIVERED_SECONDS  the proxy has sent the whole file
#   END_STALL_KNOWN_SECONDS      the renderer is known not to report STOPPED
#   END_STALL_SECONDS            otherwise
# or once the position runs PAST_END_MS beyond the length.
END_WINDOW_MS = 2_000
END_STALL_DELIVERED_SECONDS = 2.0
END_STALL_KNOWN_SECONDS = 4.0
END_STALL_SECONDS = 6.0
PAST_END_MS = 4_000
# A position counts as "not moving" while it stays within this of where the
# stall started (RelTime is reported in whole seconds).
STALL_TOLERANCE_MS = 500

# A renderer that starts the queued track as soon as it gets it: with a next
# track armed and the current URI unchanged, the position falls back by at
# least EARLY_SWITCH_BACK_MS to the start of a track (at most
# EARLY_SWITCH_TO_MS) while plenty of the current one was left. Arming waits
# for ARM_AFTER_POSITION_MS, so the jump back is visible even when the switch
# follows the arming at once.
EARLY_SWITCH_BACK_MS = 2_000
EARLY_SWITCH_TO_MS = 3_000
EARLY_SWITCH_REMAINING_MS = 15_000
# Our own seeks also move the position back; ignore jumps this soon after one.
SEEK_SETTLE_SECONDS = 5.0

# A stop at the end of a track with a next track armed this long before it
# means the renderer ignores SetNextAVTransportURI.
IGNORED_NEXT_ARMED_FOR_SECONDS = 10.0

# After a renderer that refuses Pause was stopped instead, a resume reloads
# the track; wait this long (in steps) for it to play before seeking.
RELOAD_PLAYING_WAIT_STEPS = 20
RELOAD_PLAYING_STEP_SECONDS = 0.25
