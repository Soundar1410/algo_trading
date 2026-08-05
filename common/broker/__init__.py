"""Broker adapters. One contract, two implementations — only one of which exists."""

from __future__ import annotations

from .base import Broker, BrokerError, Quote
from .costs import ChargesCalculator, CostRates
from .factory import LiveExecutionBlocked, build_broker
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
    "ChargesCalculator",
    "CostRates",
    "InstrumentRules",
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
