"""
Adapter template.

Expected functions (all optional):
- preprocess(req: dict, payload: bytes) -> bytes
- extract(req: dict, payload: bytes) -> str | None
- build_policy_api_payload(req: dict, payload: bytes, extracted_text: str) -> dict | None
- normalize_upload(req: dict, payload: bytes) -> dict | None
  Adapter-owned upload lifecycle. Return {"handled": True, ...} to skip shared handle_upload_event.
- POLICY_ATTACHMENTS_FINAL: bool (optional module constant)
  When True, build_policy_api_payload attachments are sent to the policy API as-is (no engine resolve/strip).
- extract_session_id(req: dict, payload: bytes) -> str
- response_stream_done(req: dict, payload: bytes) -> bool
- rewrite_always(req: dict, payload: bytes) -> tuple[bytes, str] | None
- rewrite_prompt(req: dict, payload: bytes, replacement_text: str, policy_code: str) -> tuple[bytes, str] | None
- rewrite_prompt_masking(req, payload, replacement_text) -> tuple[bytes, str] | None  # optional; MASKING 시 우선(프롬프트만 치환)
- rewrite_response(req: dict, payload: bytes, replacement_text: str) -> tuple[bytes, str] | None
- clear_response_override_state(session_id: str) -> None
- rewrite(req: dict, payload: bytes, src: str, dst: str) -> tuple[bytes, int] | None

Return None to fall back to the generic handler in ai_policy_engine.py.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


def preprocess(req: Dict[str, Any], payload: bytes) -> bytes:
    return payload


def extract(req: Dict[str, Any], payload: bytes) -> Optional[str]:
    return None


def build_policy_api_payload(
    req: Dict[str, Any],
    payload: bytes,
    extracted_text: str,
) -> Optional[Dict[str, Any]]:
    return None


def rewrite_always(req: Dict[str, Any], payload: bytes) -> Optional[Tuple[bytes, str]]:
    return None


def extract_session_id(req: Dict[str, Any], payload: bytes) -> str:
    return ""


def response_stream_done(req: Dict[str, Any], payload: bytes) -> bool:
    return False


def rewrite_prompt(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
    policy_code: str,
) -> Optional[Tuple[bytes, str]]:
    return None


def rewrite_response(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
) -> Optional[Tuple[bytes, str]]:
    return None


def clear_response_override_state(session_id: str) -> None:
    return None


def rewrite(
    req: Dict[str, Any],
    payload: bytes,
    src: str,
    dst: str,
) -> Optional[Tuple[bytes, int]]:
    return None
