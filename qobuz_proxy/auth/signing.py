"""
Qobuz API request signing.

Signed endpoints take a ``request_ts`` timestamp and a ``request_sig``: the MD5
of the endpoint name (object + action), the sorted request parameters as
key/value pairs, the timestamp and the application secret.
"""

import hashlib
from typing import Any, Mapping


def sign_request(endpoint: str, params: Mapping[str, Any], request_ts: str, app_secret: str) -> str:
    """Return the ``request_sig`` for a signed Qobuz API request.

    Args:
        endpoint: Object and action joined, e.g. "sessionstart" or "trackget".
        params: Request parameters (excluding request_ts / request_sig).
        request_ts: The request timestamp sent alongside the signature.
        app_secret: Qobuz application secret.
    """
    sig_string = endpoint
    for key in sorted(params.keys()):
        sig_string += key + str(params[key])
    sig_string += request_ts + app_secret
    return hashlib.md5(sig_string.encode()).hexdigest()
