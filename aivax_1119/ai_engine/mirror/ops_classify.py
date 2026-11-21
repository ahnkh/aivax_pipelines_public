#!/usr/bin/env python3
"""Coarse host/http classification for ops monitors (not policy routing)."""

from __future__ import annotations

from typing import Dict, Iterable, List, Tuple

SERVICE_KEYS: Tuple[str, ...] = (
    "chatgpt",
    "gemini",
    "claude",
    "grok",
    "perplexity",
    "notion",
    "github_copilot",
    "wrks",
    "timely",
    "genspark",
    "other",
)

HTTP_KEYS: Tuple[str, ...] = ("1.1", "2", "other")

_HOST_PATTERNS: Tuple[Tuple[str, str], ...] = (
    ("chatgpt.com", "chatgpt"),
    ("oaiusercontent.com", "chatgpt"),
    ("openai.com", "chatgpt"),
    ("gemini.google.com", "gemini"),
    ("bard.google.com", "gemini"),
    ("geminiweb-pa.clients6.google.com", "gemini"),
    ("push.clients6.google.com", "gemini"),
    ("claude.ai", "claude"),
    ("anthropic.com", "claude"),
    ("grok.com", "grok"),
    ("x.ai", "grok"),
    ("xai-enterprise", "grok"),
    ("perplexity.ai", "perplexity"),
    ("ppl-ai-file-upload", "perplexity"),
    ("notion.so", "notion"),
    ("notion.com", "notion"),
    ("prod-files-secure", "notion"),
    ("githubcopilot.com", "github_copilot"),
    ("uploads.github.com", "github_copilot"),
    ("copilot", "github_copilot"),
    ("wrks.ai", "wrks"),
    ("timelyai.io", "timely"),
)


def _is_genspark_host(host: str) -> bool:
    raw = (host or "").lower().split(":", 1)[0].strip().rstrip(".")
    return raw == "genspark.ai" or raw.endswith(".genspark.ai")


def classify_http_version(value: str) -> str:
    text = (value or "").strip().lower()
    if text.startswith("2") or text in ("http/2", "h2"):
        return "2"
    if text.startswith("1") or text in ("", "http/1.1", "http/1.0", "1.1", "1.0"):
        return "1.1"
    return "other"


def classify_http_version_index(value: str) -> int:
    key = classify_http_version(value)
    try:
        return HTTP_KEYS.index(key)
    except ValueError:
        return HTTP_KEYS.index("other")


def _is_google_ai_mode(host: str, uri: str) -> bool:
    """www.google.com AI Mode. Mirrors adapters.gemini_engine without importing it."""
    raw = (host or "").lower().split(":", 1)[0].strip().rstrip(".")
    if raw.startswith("www."):
        raw = raw[4:]
    if not raw.startswith("google.") or raw.startswith("googleapis.") or "." not in raw:
        return False
    raw_uri = uri or ""
    if "?" in raw_uri:
        base, query = raw_uri.split("?", 1)
    else:
        base, query = raw_uri, ""
    base = base.lower().rstrip("/") or "/"
    if base in ("/async/folif", "/async/folwr"):
        return True
    if base != "/search" or not query:
        return False
    for part in query.split("&"):
        key, _, value = part.partition("=")
        if key == "udm" and value == "50":
            return True
    return False


def classify_ai_service(host: str, uri: str = "") -> str:
    h = (host or "").lower()
    u = (uri or "").lower()
    if "accounts.google.com" in h and u.split("?", 1)[0].rstrip("/") == "/listaccounts":
        return "gemini"
    if "push.clients6.google.com" in h and "/upload" in u:
        return "gemini"
    if _is_google_ai_mode(h, u):
        return "gemini"
    if "storage.googleapis.com" in h and "xai-enterprise" in u:
        return "grok"
    if _is_genspark_host(h):
        return "genspark"
    if "api.github.com" in h and ("copilot" in u or "/user/copilot" in u):
        return "github_copilot"
    if "github.com" in h and "github-copilot/chat/attachments" in u:
        return "github_copilot"
    if "user-attachments.githubusercontent.com" in h or "private-user-images.githubusercontent.com" in h:
        return "github_copilot"
    for pattern, sid in _HOST_PATTERNS:
        if pattern in h:
            return sid
    return "other"


def classify_ai_service_index(host: str, uri: str = "") -> int:
    sid = classify_ai_service(host, uri)
    try:
        return SERVICE_KEYS.index(sid)
    except ValueError:
        return SERVICE_KEYS.index("other")


def counts_to_dict(keys: Iterable[str], values: List[int]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for idx, key in enumerate(keys):
        out[key] = int(values[idx]) if idx < len(values) else 0
    return out


def imbalance_ratio(values: List[int], threshold: float = 0.80) -> Tuple[bool, float, int]:
    """Return (unbalanced, max_share, max_index)."""
    total = sum(int(v) for v in values)
    if total <= 0 or not values:
        return False, 0.0, -1
    max_idx = max(range(len(values)), key=lambda i: int(values[i]))
    share = int(values[max_idx]) / float(total)
    return share >= threshold, share, max_idx


def ascii_bar(share: float, width: int = 20) -> str:
    share = max(0.0, min(1.0, float(share)))
    filled = int(round(share * width))
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"
