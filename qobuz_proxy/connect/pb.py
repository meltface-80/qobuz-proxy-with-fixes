"""
Typed handles to the generated Qobuz Connect protobuf modules.

protoc emits the ``qobuz_proxy.proto`` ``*_pb2`` modules without type stubs,
and they create their message classes at import time, so mypy cannot see any
of their attributes. They are re-exported here as ``Any`` in this one place
instead of silencing every use site.
"""

from typing import Any, TypeAlias

from qobuz_proxy.proto import common as _common
from qobuz_proxy.proto import envelope as _envelope
from qobuz_proxy.proto import payload as _payload

common_pb2: Any = _common
envelope_pb2: Any = _envelope
payload_pb2: Any = _payload

# Generated message classes, for annotations (dynamic, hence untyped).
QConnectBatch: TypeAlias = Any
RendererState: TypeAlias = Any
QConnectMessage: TypeAlias = Any
