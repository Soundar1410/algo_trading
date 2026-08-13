"""Broker adapters. One contract, two implementations."""

from __future__ import annotations

from .base import Broker, BrokerError, BrokerPosition, Quote
from .costs import ChargesCalculator, CostRates
from .dhan_live import DhanApiResponse, DhanLiveBroker, DhanOrderClient
from .factory import LiveBrokerDependencies, LiveExecutionBlocked, build_broker
from .paper import (
    InstrumentRules,
    PaperBroker,
    PaperFillConfig,
    PaperRejection,
    PaperRejectionCode,
    SlippageConfig,
)
from .quotes import QuoteBook, quote_from_tick

__all__ = [
    "Broker",
    "BrokerError",
    "BrokerPosition",
    "ChargesCalculator",
    "CostRates",
    "DhanApiResponse",
    "DhanLiveBroker",
    "DhanOrderClient",
    "InstrumentRules",
    "LiveBrokerDependencies",
    "LiveExecutionBlocked",
    "PaperBroker",
    "PaperFillConfig",
    "PaperRejection",
    "PaperRejectionCode",
    "Quote",
    "QuoteBook",
    "SlippageConfig",
    "build_broker",
    "quote_from_tick",
]
