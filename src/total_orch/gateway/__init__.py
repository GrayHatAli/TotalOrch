"""Execution gateway boundary."""

from .base import GatewayAdapter
from .errors import GatewayError
from .events import completion_event, error_event
from .fake import FakeGatewayAdapter
from .models import GatewayCompletion, GatewayModel, GatewayRequest, GatewayStreamChunk
from .omniroute import OmniRouteAdapter

__all__ = [
    "FakeGatewayAdapter",
    "GatewayAdapter",
    "GatewayCompletion",
    "GatewayError",
    "GatewayModel",
    "GatewayRequest",
    "GatewayStreamChunk",
    "OmniRouteAdapter",
    "completion_event",
    "error_event",
]
