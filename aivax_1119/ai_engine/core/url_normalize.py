"""
URL normalization helpers.

Preserve raw request-target; never invent https solely from port 443.
Signed URL query order / percent-encoding must not be rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit, urlunsplit


@dataclass
class NormalizedUrl:
    request_target_raw: str
    request_url_absolute: str
    url_source: str  # observed|decryptor_metadata|configured|unknown
    url_normalization_status: str  # ok|incomplete|invalid
    redirect_location: str = ""
    embedded_upload_url: str = ""


def classify_request_target(raw: str) -> str:
    t = raw or ""
    if t == "*":
        return "asterisk-form"
    if t.startswith("http://") or t.startswith("https://"):
        return "absolute-form"
    if t.startswith("/") or t == "":
        return "origin-form"
    # CONNECT authority-form: host:port without scheme
    if "/" not in t and ":" in t:
        return "authority-form"
    return "origin-form"


def build_absolute_url(
    request_target_raw: str,
    host: str = "",
    scheme: str = "",
    scheme_source: str = "unknown",
) -> NormalizedUrl:
    raw = request_target_raw if request_target_raw is not None else ""
    form = classify_request_target(raw)

    if form == "absolute-form":
        # Preserve bytes of path/query as given; only trust observed scheme in URL.
        return NormalizedUrl(
            request_target_raw=raw,
            request_url_absolute=raw,
            url_source="observed",
            url_normalization_status="ok",
        )

    if form == "asterisk-form":
        return NormalizedUrl(
            request_target_raw=raw,
            request_url_absolute="",
            url_source=scheme_source if scheme else "unknown",
            url_normalization_status="incomplete",
        )

    if form == "authority-form":
        # CONNECT — not a normal retrieval URL
        abs_url = ""
        if scheme and scheme_source in ("observed", "decryptor_metadata"):
            abs_url = f"{scheme}://{raw}"
        return NormalizedUrl(
            request_target_raw=raw,
            request_url_absolute=abs_url,
            url_source=scheme_source if abs_url else "unknown",
            url_normalization_status="ok" if abs_url else "incomplete",
        )

    # origin-form
    if not host:
        return NormalizedUrl(
            request_target_raw=raw,
            request_url_absolute="",
            url_source="unknown",
            url_normalization_status="incomplete",
        )

    # Do NOT invent https from port 443 alone.
    if not scheme or scheme_source == "unknown":
        return NormalizedUrl(
            request_target_raw=raw,
            request_url_absolute="",
            url_source="unknown",
            url_normalization_status="incomplete",
        )
    if scheme_source == "configured" and scheme not in ("http", "https"):
        return NormalizedUrl(
            request_target_raw=raw,
            request_url_absolute="",
            url_source="configured",
            url_normalization_status="invalid",
        )

    path = raw if raw.startswith("/") else "/" + raw
    abs_url = f"{scheme}://{host}{path}"
    return NormalizedUrl(
        request_target_raw=raw,
        request_url_absolute=abs_url,
        url_source=scheme_source,
        url_normalization_status="ok",
    )


def preserve_signed_url(url: str) -> str:
    """Return URL unchanged — no query reordering / re-encoding."""
    return url or ""
