"""GitHub webhook signature verification (``X-Hub-Signature-256``)."""

from __future__ import annotations

import hashlib
import hmac

PREFIX = "sha256="


def sign(secret: bytes, body: bytes) -> str:
    return PREFIX + hmac.new(secret, body, hashlib.sha256).hexdigest()


def verify_signature(secret: bytes, body: bytes, header: str | None) -> bool:
    """True only for a well-formed sha256 signature of exactly this body.

    The comparison is constant-time, and the legacy sha1 header is never accepted.
    """
    if not secret or not header or not header.startswith(PREFIX):
        return False
    return hmac.compare_digest(sign(secret, body), header)
