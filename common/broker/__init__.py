"""Broker adapters. One contract, two implementations — only one of which exists."""

from __future__ import annotations

from .base import Broker, BrokerError, Quote
from .costs import ChargesCalculator, CostRates
from .factory import LiveExecutionBlocked, build_broker
from .paper import PaperBroker, PaperFillConfig, PaperRejection

__all__ = [
    "Broker",
    "BrokerError",
    "ChargesCalculator",
    "CostRates",
    "LiveExecutionBlocked",
    "PaperBroker",
    "PaperFillConfig",
    "PaperRejection",
    "Quote",
    "build_broker",
]
