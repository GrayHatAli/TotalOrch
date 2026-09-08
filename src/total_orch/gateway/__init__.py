"""Execution gateway boundary.

The Orchestrator talks to exactly one gateway through ``GatewayAdapter``.
OmniRoute is the default managed gateway; any OpenAI-compatible router
(LiteLLM, 9Router, One API / New API, FreeLLMAPI, ...) is supported through
:class:`OpenAICompatAdapter` and selected via ``GATEWAY_TYPE`` /
:func:`build_gateway`.
"""

from .base import GatewayAdapter
from .errors import GatewayError
from .events import completion_event, error_event
from .factory import build_gateway, supported_gateway_types
from .fake import FakeGatewayAdapter
from .models import GatewayCompletion, GatewayModel, GatewayRequest, GatewayStreamChunk
from .omniroute import OmniRouteAdapter
from .openai_compat import OpenAICompatAdapter

__all__ = [
    "FakeGatewayAdapter",
    "GatewayAdapter",
    "GatewayCompletion",
    "GatewayError",
    "GatewayModel",
    "GatewayRequest",
    "GatewayStreamChunk",
    "OmniRouteAdapter",
    "OpenAICompatAdapter",
    "build_gateway",
    "completion_event",
    "error_event",
    "supported_gateway_types",
]
