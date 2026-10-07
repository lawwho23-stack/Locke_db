"""Pure text matching and privacy-preserving suppression digests."""

import hashlib
import hmac
import unicodedata


def normalize_text(text: str) -> str:
    """Normalize for comparison while keeping the original stored text unchanged."""
    normalized = unicodedata.normalize("NFC", text)
    # Burmese copy/paste often includes these invisible separators.
    normalized = normalized.replace("\u200b", "").replace("\ufeff", "")
    return " ".join(normalized.casefold().split())


def content_hash(normalized: str) -> str:
    """SHA-256 digest of already-normalized UTF-8 text."""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def suppression_hmac(normalized: str, key: bytes) -> str:
    """A keyed digest lets us recognize forgotten text without storing it."""
    return hmac.new(key, normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def normalize_fact_key(fact_key: str) -> str:
    """Strip and lowercase a fact key."""
    return fact_key.strip().lower()
