"""Configuration fingerprinting.

Every worker session and order intent records the fingerprint of the resolved
configuration that produced it (spec section 9). Without it, a record from last
week is unreadable: you cannot tell whether a trade came from the parameters
currently in the YAML file or from an earlier edit.

The digest is over a canonical JSON form — keys sorted, no insignificant
whitespace — so reordering a YAML file does not change the fingerprint but
changing any value does.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel

FINGERPRINT_LENGTH = 16


def _canonical(value: Any) -> Any:
    """Reduce a config object to JSON-serialisable primitives, deterministically."""
    if isinstance(value, BaseModel):
        return _canonical(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


def fingerprint(config: Any, *, length: int = FINGERPRINT_LENGTH) -> str:
    """Return a short, stable SHA-256 digest of a resolved configuration.

    Truncated to ``length`` hex characters: this identifies a configuration in
    logs and database rows, it is not a security control.
    """
    payload = json.dumps(_canonical(config), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]
