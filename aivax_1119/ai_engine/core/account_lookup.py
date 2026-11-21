"""
Account lookup cache with sharding, confidence grades, and key-separated HMAC fingerprints.

Grades: explicit | session_correlated | ip_predicted | anonymous | ambiguous
IP-only prediction is conservative and always flagged account_predicted=true.

By default entries are mirrored to `/run/aivax/account_cache.sqlite` so Gemini/Claude
session→account maps survive policy restarts (in-memory alone is wiped on restart).
Set `AIVAX_ACCOUNT_CACHE_PATH=` (empty) to disable; override path via the same env.
Also used under multi-worker SHM so shm_ring_consumer processes share the map.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import logging
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

_CacheValue = Tuple[str, str, float, str, float]  # user_id, email, expiry, source, confidence
_log = logging.getLogger(__name__)


@dataclass
class AccountResult:
    user_id: str
    email: str
    source: str  # explicit|session_correlated|ip_predicted|anonymous|ambiguous
    confidence: float
    account_predicted: bool
    account_type: str = ""  # corporate|personal|unknown
    display_name: str = ""


def _hmac_hex(key: bytes, data: str) -> str:
    return hmac.new(key, data.encode("utf-8", errors="ignore"), hashlib.sha256).hexdigest()


def prefer_identity(
    old_user_id: str,
    old_email: str,
    new_user_id: str,
    new_email: str,
) -> Tuple[str, str]:
    """Never let a concurrent empty write wipe a known account.

    Email is the stable user identity.  If two non-empty emails disagree,
    the first writer wins so a later cookie/org rotation cannot flip the user.
    """
    old_uid = str(old_user_id or "").strip()
    old_em = str(old_email or "").strip()
    new_uid = str(new_user_id or "").strip()
    new_em = str(new_email or "").strip()
    uid = new_uid or old_uid
    if old_em and new_em and old_em.lower() != new_em.lower():
        _log.warning(
            "account email conflict keep=%s ignore=%s",
            old_em,
            new_em,
        )
        return uid or old_uid, old_em
    return uid, (new_em or old_em)


class ShardedAccountLookupCache:
    """Bounded sharded LRU+TTL. Shard by hash(service_id|session_key)."""

    __slots__ = ("_maxsize", "_ttl_seconds", "_shards", "_locks", "_n", "_per")

    def __init__(self, maxsize: int = 10_000, ttl_seconds: int = 3600, shards: int = 64) -> None:
        self._n = max(1, int(shards))
        self._maxsize = max(self._n, int(maxsize))
        per = max(1, self._maxsize // self._n)
        self._ttl_seconds = max(0, int(ttl_seconds))
        self._shards = [OrderedDict() for _ in range(self._n)]
        self._locks = [threading.RLock() for _ in range(self._n)]
        self._per = per

    def _idx(self, service_id: str, session_key: str) -> int:
        h = hash((service_id.lower(), session_key))
        return h % self._n

    def _key(self, service_id: str, session_key: str) -> Tuple[str, str]:
        return (str(service_id or "").strip().lower(), str(session_key or "").strip())

    def _expired(self, expiry_ts: float) -> bool:
        if self._ttl_seconds <= 0:
            return False
        return time.monotonic() >= expiry_ts

    def get(self, service_id: str, session_key: str) -> Optional[AccountResult]:
        key = self._key(service_id, session_key)
        if not key[1]:
            return None
        i = self._idx(*key)
        with self._locks[i]:
            cache = self._shards[i]
            entry = cache.get(key)
            if entry is None:
                return None
            user_id, email, expiry_ts, source, confidence = entry[:5]
            display_name = str(entry[5] or "") if len(entry) > 5 else ""
            if self._expired(expiry_ts):
                cache.pop(key, None)
                return None
            cache.move_to_end(key)
            return AccountResult(
                user_id=user_id,
                email=email,
                source=source,
                confidence=confidence,
                account_predicted=(source == "ip_predicted"),
                display_name=display_name,
            )

    def set(
        self,
        service_id: str,
        session_key: str,
        user_id: str,
        email: str,
        source: str = "explicit",
        confidence: float = 1.0,
        ttl_override: Optional[int] = None,
        replace: bool = False,
        display_name: str = "",
    ) -> None:
        key = self._key(service_id, session_key)
        if not key[1]:
            return
        ttl = self._ttl_seconds if ttl_override is None else max(0, int(ttl_override))
        expiry_ts = time.monotonic() + ttl if ttl > 0 else 0.0
        display_name = str(display_name or "").strip()
        i = self._idx(*key)
        with self._locks[i]:
            cache = self._shards[i]
            prev = cache.get(key)
            if prev is not None:
                cache.move_to_end(key)
                if not replace:
                    user_id, email = prefer_identity(prev[0], prev[1], user_id, email)
                    if not source:
                        source = prev[3]
                    if confidence < float(prev[4] or 0.0) and (prev[0] or prev[1]):
                        confidence = float(prev[4] or 0.0)
                    if not display_name and len(prev) > 5:
                        display_name = str(prev[5] or "")
            cache[key] = (
                str(user_id or "").strip(),
                str(email or "").strip(),
                expiry_ts,
                str(source or "explicit"),
                float(confidence),
                display_name,
            )
            while len(cache) > self._per:
                cache.popitem(last=False)

    def clear(self) -> None:
        for i in range(self._n):
            with self._locks[i]:
                self._shards[i].clear()


class _SharedSqliteStore:
    """Cross-process account map (SHM multi-worker). Wall-clock TTL."""

    __slots__ = ("_path", "_ttl", "_local", "_lock")

    def __init__(self, path: str, ttl_seconds: int = 3600) -> None:
        self._path = path
        self._ttl = max(0, int(ttl_seconds))
        self._local = threading.local()
        self._lock = threading.RLock()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """One-process-at-a-time WAL + CREATE. PRAGMA journal_mode=WAL needs EXCLUSIVE."""
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, mode=0o750, exist_ok=True)
        lock_path = self._path + ".initlock"
        last_err: Optional[Exception] = None
        for attempt in range(8):
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o640)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                conn = sqlite3.connect(self._path, timeout=30.0, check_same_thread=False)
                try:
                    conn.execute("PRAGMA busy_timeout=30000")
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA synchronous=NORMAL")
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS accounts (
                            service_id TEXT NOT NULL,
                            session_key TEXT NOT NULL,
                            user_id TEXT NOT NULL,
                            email TEXT NOT NULL,
                            source TEXT NOT NULL,
                            confidence REAL NOT NULL,
                            expiry_wall REAL NOT NULL,
                            PRIMARY KEY (service_id, session_key)
                        )
                        """
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS accounts_expiry ON accounts(expiry_wall)"
                    )
                    columns = {
                        str(row[1])
                        for row in conn.execute("PRAGMA table_info(accounts)").fetchall()
                    }
                    if "display_name" not in columns:
                        conn.execute(
                            "ALTER TABLE accounts ADD COLUMN display_name TEXT NOT NULL DEFAULT ''"
                        )
                    conn.commit()
                finally:
                    conn.close()
                return
            except sqlite3.OperationalError as exc:
                last_err = exc
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                time.sleep(0.05 * (attempt + 1))
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(fd)
        raise sqlite3.OperationalError(
            f"account cache init locked path={self._path}: {last_err}"
        )

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.execute("SELECT 1")
                return conn
            except sqlite3.Error:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
                self._local.conn = None
        conn = sqlite3.connect(self._path, timeout=30.0, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        try:
            conn.close()
        except sqlite3.Error:
            pass
        self._local.conn = None

    def get(self, service_id: str, session_key: str) -> Optional[AccountResult]:
        sid = str(service_id or "").strip().lower()
        sk = str(session_key or "").strip()
        if not sid or not sk:
            return None
        now = time.time()
        try:
            with self._lock:
                row = self._conn().execute(
                    "SELECT user_id, email, source, confidence, expiry_wall, display_name "
                    "FROM accounts WHERE service_id=? AND session_key=?",
                    (sid, sk),
                ).fetchone()
                if row is None:
                    return None
                user_id, email, source, confidence, expiry_wall, display_name = row
                if self._ttl > 0 and float(expiry_wall) > 0 and now >= float(expiry_wall):
                    self._conn().execute(
                        "DELETE FROM accounts WHERE service_id=? AND session_key=?",
                        (sid, sk),
                    )
                    self._conn().commit()
                    return None
                return AccountResult(
                    user_id=str(user_id or ""),
                    email=str(email or ""),
                    source=str(source or "explicit"),
                    confidence=float(confidence or 0.0),
                    account_predicted=(source == "ip_predicted"),
                    display_name=str(display_name or ""),
                )
        except Exception:
            _log.exception("account shared get failed path=%s", self._path)
            return None

    def set(
        self,
        service_id: str,
        session_key: str,
        user_id: str,
        email: str,
        source: str = "explicit",
        confidence: float = 1.0,
        ttl_override: Optional[int] = None,
        replace: bool = False,
        display_name: str = "",
    ) -> None:
        sid = str(service_id or "").strip().lower()
        sk = str(session_key or "").strip()
        if not sid or not sk:
            return
        ttl = self._ttl if ttl_override is None else max(0, int(ttl_override))
        expiry_wall = time.time() + ttl if ttl > 0 else 0.0
        display_name = str(display_name or "").strip()
        conflict_sql = (
            """
                      user_id=excluded.user_id,
                      email=excluded.email,
                      display_name=excluded.display_name,
                      source=excluded.source,
                      confidence=excluded.confidence,
                      expiry_wall=excluded.expiry_wall
            """
            if replace
            else """
                      user_id=CASE
                        WHEN excluded.user_id != '' THEN excluded.user_id
                        ELSE accounts.user_id
                      END,
                      email=CASE
                        WHEN accounts.email != ''
                         AND excluded.email != ''
                         AND lower(accounts.email) != lower(excluded.email)
                        THEN accounts.email
                        WHEN excluded.email != '' THEN excluded.email
                        ELSE accounts.email
                      END,
                      display_name=CASE
                        WHEN excluded.display_name != '' THEN excluded.display_name
                        ELSE accounts.display_name
                      END,
                      source=excluded.source,
                      confidence=excluded.confidence,
                      expiry_wall=excluded.expiry_wall
            """
        )
        try:
            with self._lock:
                self._conn().execute(
                    f"""
                    INSERT INTO accounts
                      (service_id, session_key, user_id, email, source, confidence, expiry_wall, display_name)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(service_id, session_key) DO UPDATE SET
                    {conflict_sql}
                    """,
                    (
                        sid,
                        sk,
                        str(user_id or "").strip(),
                        str(email or "").strip(),
                        str(source or "explicit"),
                        float(confidence),
                        float(expiry_wall),
                        display_name,
                    ),
                )
                self._conn().commit()
        except Exception:
            _log.exception("account shared set failed path=%s", self._path)

    def clear(self) -> None:
        try:
            with self._lock:
                self._conn().execute("DELETE FROM accounts")
                self._conn().commit()
        except Exception:
            _log.exception("account shared clear failed path=%s", self._path)


# ── Key-separated HMAC material (do not reuse across data classes) ─────────
_account_key = os.environ.get("AIVAX_HMAC_ACCOUNT_KEY", "dev-account-key-change-me").encode()
_filename_key = os.environ.get("AIVAX_HMAC_FILENAME_KEY", "dev-filename-key-change-me").encode()
_url_key = os.environ.get("AIVAX_HMAC_URL_KEY", "dev-url-key-change-me").encode()
_KEY_VERSION = os.environ.get("AIVAX_HMAC_KEY_VERSION", "1")

# Prediction policy
CONFIG_ACCOUNT_PREDICTION = os.environ.get("CONFIG_ACCOUNT_PREDICTION", "ON").upper() == "ON"
# IP prediction TTL is intentionally short
_IP_PREDICT_TTL = int(os.environ.get("AIVAX_IP_PREDICT_TTL", "300"))

# Per (service, client_ip) last explicit account for prediction / ambiguity
_ip_last: Dict[Tuple[str, str], Tuple[str, str, float]] = {}
_ip_lock = threading.RLock()

_cache: Optional[ShardedAccountLookupCache] = None
_shared: Optional[_SharedSqliteStore] = None


def _default_shared_path() -> str:
    """Shared SQLite path. Default on; empty env disables; SHM workers same default."""
    env = os.environ.get("AIVAX_ACCOUNT_CACHE_PATH")
    if env is not None:
        return env.strip()
    return "/run/aivax/account_cache.sqlite"


def configure(maxsize: int = 10_000, ttl_seconds: int = 3600, shards: int = 64) -> None:
    global _cache, _shared
    _cache = ShardedAccountLookupCache(maxsize=maxsize, ttl_seconds=ttl_seconds, shards=shards)
    path = _default_shared_path()
    if _shared is not None:
        _shared.close()
    _shared = None
    if path:
        try:
            _shared = _SharedSqliteStore(path, ttl_seconds=ttl_seconds)
            _log.info("account_lookup shared store path=%s ttl=%s", path, ttl_seconds)
        except Exception:
            _log.exception("account_lookup shared store init failed path=%s", path)
            _shared = None


def prepare_shared_store(ttl_seconds: int = 3600) -> None:
    """Create WAL schema before SHM workers spawn so they do not race journal_mode=WAL."""
    path = _default_shared_path()
    if not path:
        return
    store = _SharedSqliteStore(path, ttl_seconds=ttl_seconds)
    store.close()
    _log.info("account_lookup shared store prepared path=%s", path)


def fingerprint_account_secret(secret: str) -> str:
    return f"v{_KEY_VERSION}:" + _hmac_hex(_account_key, secret)


def fingerprint_filename(name: str) -> str:
    return f"v{_KEY_VERSION}:" + _hmac_hex(_filename_key, name)


def fingerprint_url(url: str) -> str:
    return f"v{_KEY_VERSION}:" + _hmac_hex(_url_key, url)


def get(service_id: str, session_key: str) -> Optional[Tuple[str, str]]:
    """Backward-compatible (user_id, email) getter."""
    res = get_result(service_id, session_key)
    if res is None:
        return None
    return (res.user_id, res.email)


def _account_wait_ms(env_name: str = "") -> int:
    for name in (env_name, "AIVAX_ACCOUNT_WAIT_MS"):
        if not name:
            continue
        raw = os.environ.get(name)
        if raw is None or not str(raw).strip():
            continue
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            continue
    return 1000


def wait_for(
    service_id: str,
    session_keys: list,
    timeout_ms: Optional[int] = None,
    *,
    env_name: str = "",
) -> Optional[Tuple[str, str]]:
    """Poll the shared cache until another worker registers identity.

    SHM workers do not share memory.  Profile on worker A must become
    visible to a chat/output wait on worker B via sqlite.
    """
    keys = [str(k or "").strip() for k in (session_keys or []) if str(k or "").strip()]
    if not keys:
        return None

    def _hit() -> Optional[Tuple[str, str]]:
        for key in keys:
            info = get(service_id, key)
            if info and (info[0] or info[1]):
                return info
        return None

    found = _hit()
    if found:
        return found
    if timeout_ms is None:
        timeout_ms = _account_wait_ms(env_name)
    deadline = time.monotonic() + max(0, int(timeout_ms)) / 1000.0
    while time.monotonic() < deadline:
        time.sleep(0.02)
        found = _hit()
        if found:
            return found
    return None


def get_result(service_id: str, session_key: str) -> Optional[AccountResult]:
    if _cache is None:
        return None
    res = _cache.get(service_id, session_key)
    if res is not None:
        return res
    if _shared is not None:
        shared = _shared.get(service_id, session_key)
        if shared is not None:
            # Promote into local L1 so subsequent lookups stay cheap.
            _cache.set(
                service_id,
                session_key,
                shared.user_id,
                shared.email,
                source=shared.source,
                confidence=shared.confidence,
                display_name=shared.display_name,
            )
            return shared
    return None


def set_account(
    service_id: str,
    session_key: str,
    user_id: str,
    email: str,
    source: str = "explicit",
    confidence: float = 1.0,
    client_ip: str = "",
    replace: bool = False,
    display_name: str = "",
) -> None:
    if _cache is None:
        _log.warning(
            "account_lookup set_account ignored (configure not called) service=%s",
            service_id,
        )
        return
    # Prefer HMAC of secrets when session_key looks like a token/cookie value
    sk = session_key
    if session_key and (len(session_key) > 24 or " " not in session_key):
        # keep original key for lookup compatibility; callers should pass pre-hashed when ready
        sk = session_key
    ttl = None
    if source == "ip_predicted":
        ttl = _IP_PREDICT_TTL
    replace = bool(replace) or source == "ambiguous"
    _cache.set(
        service_id,
        sk,
        user_id,
        email,
        source=source,
        confidence=confidence,
        ttl_override=ttl,
        replace=replace,
        display_name=display_name,
    )
    if _shared is not None:
        _shared.set(
            service_id,
            sk,
            user_id,
            email,
            source=source,
            confidence=confidence,
            ttl_override=ttl,
            replace=replace,
            display_name=display_name,
        )

    if client_ip and source in ("explicit", "session_correlated"):
        with _ip_lock:
            ip_key = (str(service_id).lower(), str(client_ip).strip())
            prev = _ip_last.get(ip_key)
            if prev and (prev[0] != user_id or prev[1] != email):
                # mark ambiguity by storing empty sentinel with negative confidence via special key
                _ip_last[ip_key] = (user_id, email, time.monotonic())
                _cache.set(
                    service_id,
                    f"ip:{client_ip}",
                    "",
                    "",
                    source="ambiguous",
                    confidence=0.0,
                    ttl_override=_IP_PREDICT_TTL,
                    replace=True,
                )
                if _shared is not None:
                    _shared.set(
                        service_id,
                        f"ip:{client_ip}",
                        "",
                        "",
                        source="ambiguous",
                        confidence=0.0,
                        ttl_override=_IP_PREDICT_TTL,
                        replace=True,
                    )
            else:
                _ip_last[ip_key] = (user_id, email, time.monotonic())
                _cache.set(
                    service_id,
                    f"ip:{client_ip}",
                    user_id,
                    email,
                    source="explicit",
                    confidence=1.0,
                    ttl_override=_IP_PREDICT_TTL * 2,
                    display_name=display_name,
                )
                if _shared is not None:
                    _shared.set(
                        service_id,
                        f"ip:{client_ip}",
                        user_id,
                        email,
                        source="explicit",
                        confidence=1.0,
                        ttl_override=_IP_PREDICT_TTL * 2,
                        display_name=display_name,
                    )


def predict_from_ip(service_id: str, client_ip: str) -> AccountResult:
    """Conservative IP fallback. Never invents when ambiguous or disabled."""
    if not CONFIG_ACCOUNT_PREDICTION or not client_ip:
        return AccountResult("", "", "anonymous", 0.0, False)
    if _cache is None:
        return AccountResult("", "", "anonymous", 0.0, False)
    res = get_result(service_id, f"ip:{client_ip}")
    if res is None:
        return AccountResult("", "", "anonymous", 0.0, False)
    if res.source == "ambiguous" or not res.user_id:
        return AccountResult("", "", "ambiguous", 0.0, False)
    return AccountResult(
        user_id=res.user_id,
        email=res.email,
        source="ip_predicted",
        confidence=min(0.5, res.confidence),
        account_predicted=True,
        display_name=res.display_name,
    )


def classify_account_type(email: str, corporate_domains: Optional[list] = None) -> str:
    domains = corporate_domains or [
        d.strip().lower()
        for d in os.environ.get("AIVAX_CORPORATE_DOMAINS", "wins21.co.kr").split(",")
        if d.strip()
    ]
    em = (email or "").strip().lower()
    if "@" not in em:
        return "unknown"
    domain = em.rsplit("@", 1)[-1]
    if domain in domains:
        return "corporate"
    return "personal"
