"""Cross-process ChatGPT stream_handoff → parent request_id map (Valkey).

Mirror SHM consumers run as many processes. The in-memory dict in
``chatgpt_engine`` was process-local, so HTTP handoff registration on worker A
was invisible to the WS final answer on worker B (parent=- → merge park).

Valkey (same daemon as pipeline log-merge) is the shared backend. A small
process-local L1 cache remains for same-worker hits and Valkey blips.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Dict, Optional, Tuple

_log = logging.getLogger(__name__)

_DEFAULT_PREFIX = "aivax:chatgpt_resume_parent:"
_DEFAULT_TTL_SEC = 3600.0
_DEFAULT_MAX_LOCAL = 20000

# Process-local L1 (always on).
_local: Dict[str, Tuple[float, str]] = {}
_local_lock = threading.Lock()
_local_ttl_sec = _DEFAULT_TTL_SEC
_local_max = _DEFAULT_MAX_LOCAL

_client = None
_enabled = False
_key_prefix = _DEFAULT_PREFIX
_ttl_sec = _DEFAULT_TTL_SEC
_configured = False
_config_lock = threading.Lock()
_warn_once = False


def enabled() -> bool:
    return _enabled


def backend_name() -> str:
    return "valkey" if _enabled else "memory"


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def configure(
    *,
    url: str = "",
    host: str = "127.0.0.1",
    port: int = 6379,
    db: int = 0,
    password: str = "",
    key_prefix: str = _DEFAULT_PREFIX,
    ttl_seconds: float = _DEFAULT_TTL_SEC,
    socket_connect_timeout_ms: int = 200,
    socket_timeout_ms: int = 200,
    force_memory: bool = False,
) -> bool:
    """Connect to Valkey. Returns True if shared backend is active."""
    global _client, _enabled, _key_prefix, _ttl_sec, _configured
    global _local_ttl_sec, _warn_once

    ttl = max(60.0, float(ttl_seconds or _DEFAULT_TTL_SEC))
    _ttl_sec = ttl
    _local_ttl_sec = ttl
    _key_prefix = str(key_prefix or _DEFAULT_PREFIX)
    if not _key_prefix.endswith(":"):
        _key_prefix = _key_prefix + ":"
    _warn_once = False

    if force_memory:
        _close_client()
        _enabled = False
        _configured = True
        _log.info("resume_parent_store backend=memory (forced)")
        return False

    try:
        from valkey import Valkey
    except ImportError as exc:
        _close_client()
        _enabled = False
        _configured = True
        _log.warning("resume_parent_store valkey package missing; memory only (%s)", exc)
        return False

    common = {
        "decode_responses": True,
        "socket_connect_timeout": max(0.05, float(socket_connect_timeout_ms) / 1000.0),
        "socket_timeout": max(0.05, float(socket_timeout_ms) / 1000.0),
        "health_check_interval": 30,
    }
    client = None
    try:
        url = (url or "").strip()
        if url:
            client = Valkey.from_url(url, **common)
        else:
            pw = password or None
            if isinstance(pw, str) and not pw:
                pw = None
            client = Valkey(
                host=str(host or "127.0.0.1"),
                port=int(port or 6379),
                db=int(db or 0),
                password=pw,
                **common,
            )
        client.ping()
    except Exception as exc:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        _close_client()
        _enabled = False
        _configured = True
        _log.warning(
            "resume_parent_store valkey connect failed; memory only "
            "(multi-worker handoff remap will break): %s",
            exc,
        )
        return False

    _close_client()
    _client = client
    _enabled = True
    _configured = True
    _log.info(
        "resume_parent_store backend=valkey prefix=%s ttl=%.0fs",
        _key_prefix,
        _ttl_sec,
    )
    return True


def configure_from_env() -> bool:
    """Configure from AIVAX_VALKEY_* / AIVAX_RESUME_PARENT_* env vars."""
    backend = (os.environ.get("AIVAX_RESUME_PARENT_BACKEND") or "valkey").strip().lower()
    force_memory = backend in ("memory", "mem", "local", "0", "off", "false")
    return configure(
        url=(os.environ.get("AIVAX_VALKEY_URL") or "").strip(),
        host=os.environ.get("AIVAX_VALKEY_HOST", "127.0.0.1"),
        port=_env_int("AIVAX_VALKEY_PORT", 6379),
        db=_env_int("AIVAX_VALKEY_DB", 0),
        password=os.environ.get("AIVAX_VALKEY_PASSWORD", ""),
        key_prefix=os.environ.get("AIVAX_RESUME_PARENT_PREFIX", _DEFAULT_PREFIX),
        ttl_seconds=_env_float("AIVAX_RESUME_PARENT_TTL_SEC", _DEFAULT_TTL_SEC),
        socket_connect_timeout_ms=_env_int("AIVAX_RESUME_PARENT_CONNECT_TIMEOUT_MS", 200),
        socket_timeout_ms=_env_int("AIVAX_RESUME_PARENT_SOCKET_TIMEOUT_MS", 200),
        force_memory=force_memory,
    )


def ensure_configured() -> None:
    global _configured
    if _configured:
        return
    with _config_lock:
        if _configured:
            return
        configure_from_env()


def _close_client() -> None:
    global _client, _enabled
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
    _client = None
    _enabled = False


def _vk_key(logical_key: str) -> str:
    return f"{_key_prefix}{logical_key}"


def _local_put(key: str, parent_request_id: str) -> None:
    now = time.monotonic()
    with _local_lock:
        if len(_local) >= _local_max:
            stale = [
                k for k, (ts, _) in _local.items() if now - ts > _local_ttl_sec
            ]
            for k in stale:
                _local.pop(k, None)
            while len(_local) >= _local_max:
                _local.pop(next(iter(_local)), None)
        _local[key] = (now, parent_request_id)


def _local_get(key: str) -> str:
    with _local_lock:
        ent = _local.get(key)
        if not ent:
            return ""
        ts, rid = ent
        if time.monotonic() - ts > _local_ttl_sec:
            _local.pop(key, None)
            return ""
        return rid


def put(key: str, parent_request_id: str) -> None:
    """Bind handoff topic/conversation key → parent /f/conversation request_id."""
    ensure_configured()
    k = (key or "").strip()
    rid = (parent_request_id or "").strip()
    if not k or not rid:
        return
    _local_put(k, rid)
    if not _enabled or _client is None:
        return
    try:
        _client.set(_vk_key(k), rid, ex=int(_ttl_sec))
    except Exception as exc:
        global _warn_once
        if not _warn_once:
            _warn_once = True
            _log.warning("resume_parent_store valkey SET failed (using L1): %s", exc)


def get(key: str) -> str:
    """Lookup parent request_id for a handoff key."""
    ensure_configured()
    k = (key or "").strip()
    if not k:
        return ""
    hit = _local_get(k)
    if hit:
        return hit
    if not _enabled or _client is None:
        return ""
    try:
        val = _client.get(_vk_key(k))
    except Exception as exc:
        global _warn_once
        if not _warn_once:
            _warn_once = True
            _log.warning("resume_parent_store valkey GET failed (using L1): %s", exc)
        return ""
    if not val:
        return ""
    rid = str(val).strip()
    if rid:
        _local_put(k, rid)
    return rid


def clear_local_for_tests() -> None:
    """Test helper: drop L1 only (does not flush Valkey)."""
    with _local_lock:
        _local.clear()


def reset_for_tests() -> None:
    """Test helper: full reset including Valkey client."""
    global _configured, _warn_once
    with _local_lock:
        _local.clear()
    _close_client()
    _configured = False
    _warn_once = False
