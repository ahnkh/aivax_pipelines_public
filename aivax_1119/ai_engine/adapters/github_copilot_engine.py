"""
GitHub Copilot Adapter (Business / VS Code agent chat).

API:
  VS Code / Copilot Chat (CAPI): POST /chat/completions and /v1/chat/completions
    messages[].role=user → content string or content[].text
    User text is often wrapped in <userRequest> or <user_query>
    SSE: choices[].delta.content, terminated by [DONE]
  Copilot agent / business: POST /responses and /v1/responses
    input[].role=user → content[].type=input_text → <userRequest>
    SSE: response.output_text.delta / response.completed
  Inline ghost text (POST /v1/engines/.../completions) is code context, not a chat prompt.
  The user message is a document: the typed text, then renderer blocks
  (system_reminder, tagged_files, ide_selection, image paths). Only the typed
  text is the prompt. Activity summary, progress-message, and next-edit calls
  are Chat Completions one-shots (max_tokens or prediction, no user_query and
  no assistant turn) and are not a chat turn.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple, Union

from ai_service_codes import AI_SERVICE_GITHUB_COPILOT

try:
    import account_lookup as _account_lookup
except ImportError:
    _account_lookup = None
from adapters.github_copilot_upload import (
    POLICY_ATTACHMENTS_FINAL,
    build_policy_attachments,
    is_github_copilot_skip_upload_path,
    is_image_like_filename,
    normalize_upload,
    strip_request_attachments,
    user_text_has_image_attachment,
)

logger = logging.getLogger(__name__)

_USER_REQUEST_RE = re.compile(
    r"(<(?:userRequest|user_query|userQuery)>\s*)(.*?)(\s*</(?:userRequest|user_query|userQuery)>)",
    re.DOTALL,
)
try:
    from block_copy import MASK_TOKEN as _AIVAX_MASK_TAG
except ImportError:
    from core.block_copy import MASK_TOKEN as _AIVAX_MASK_TAG
_TITLE_REQUEST_MARKER = "following request:\n\n"
_TITLE_TURN_RE = re.compile(
    r"please write a brief title for the following",
    re.IGNORECASE,
)
_DATETIME_RE = re.compile(
    r"<current_datetime\b[^>]*>.*?</current_datetime>\s*",
    re.IGNORECASE | re.DOTALL,
)
# Blocks the Copilot prompt renderer inserts around the typed question.
# They are part of the user message document, not text the user wrote.
_CONTEXT_BLOCK_RE = re.compile(
    r"<(system_reminder|system-reminder|tagged_files|ide_selection|new-activity|attachments|attachment)\b[^>]*>.*?</\1>\s*",
    re.IGNORECASE | re.DOTALL,
)
_IMAGE_PATH_RE = re.compile(r"(?m)^[ \t]*Image file at path [^\n]*(?:\n|$)")
# Next-edit / ghost-text documents. The `<|name|>` tokens are the protocol,
# distinct from a chat user message.
_NES_DOCUMENT_RE = re.compile(
    r"<\|(?:recently_viewed_code_snippets|current_file_content|edit_diff_history|cursor_location|cursor)\|?>"
)
# After a compaction the client replays the earlier turns and the summary it
# just generated as one user item. Those turns were already recorded.
_HISTORY_REPLAY_RE = re.compile(r"<user_message>.*?</user_message>", re.DOTALL)
_SUMMARY_BLOCK_RE = re.compile(r"<summary>|</summary>")
# Items the model and the client produce while a turn is already running.
# One of these after the last role=user entry means the agent is continuing
# that turn, replaying the same question, not asking a new one.
_AGENT_WORK_ITEM_TYPES = frozenset(
    {
        "function_call",
        "function_call_output",
        "custom_tool_call",
        "custom_tool_call_output",
        "local_shell_call",
        "local_shell_call_output",
        "computer_call",
        "computer_call_output",
        "web_search_call",
        "file_search_call",
        "tool_search_call",
        "reasoning",
    }
)
_TOOL_CALL_STREAMS: Dict[str, Tuple[str, ...]] = {}
# Turn the user started -> request_id that logged the prompt, so the answer
# that lands rounds later merges into that row instead of a bare one.
_TURN_PARENT_BY_KEY: "OrderedDict[str, str]" = OrderedDict()
_PARENT_BY_REQUEST: "OrderedDict[str, str]" = OrderedDict()
# Client that still owes the model a tool result: when the round that asked
# for it ended, and the tools it asked for. A tool that runs a sub-agent is
# answered by a second conversation, which is why that conversation exists.
_TOOL_WAIT_BY_CLIENT: "OrderedDict[str, Tuple[float, Tuple[str, ...]]]" = OrderedDict()
_TOOL_WAIT_SECONDS = 180.0
_SUBAGENT_TOOL_RE = re.compile(
    r"(sub_?agent|run_?task|spawn_?agent|delegate|^task$)", re.IGNORECASE
)
_SKIP_RESPONSE_IDS: List[str] = []
_SKIP_RESPONSE_SET = set()
_IP_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\b"
)
_COPILOT_POLICY_PATHS = frozenset(
    {
        "/responses",
        "/v1/responses",
        "/chat/completions",
        "/v1/chat/completions",
    }
)
# GitHub profile and the Copilot token mint. The chat body does not carry the login.
_COPILOT_ACCOUNT_PATHS = frozenset(
    {
        "/user",
        "/user/emails",
        "/copilot_internal/user",
        "/copilot_internal/token",
        "/copilot_internal/v2/token",
    }
)
_ACCOUNT_SERVICE = "github_copilot"
# Copilot session token fingerprint → GitHub token fingerprint from the mint response.
_SESSION_TO_GITHUB: Dict[str, str] = {}
_GITHUB_SESSIONS: Dict[str, List[str]] = {}
_MODEL_BY_CLIENT: Dict[str, str] = {}
_LIVE_CAPTURE_DIR = "/home/AIVAX/research/copilot_live"
_REDACT_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "token",
        "access_token",
        "refresh_token",
        "password",
        "secret",
        "cookie",
        "set-cookie",
    }
)
_RESPONSE_BUFFER: Dict[str, str] = {}
_RESPONSE_STREAM_KEY_ORDER: List[str] = []
_RESPONSE_OVERRIDE_EMITTED: Dict[str, bool] = {}
_FALLBACK_STREAM_KEY = "__fallback__"
_BLOCK_SYSINST_VERTICAL_GAP = "\n" * 1


def _safe_utf8(data: Union[bytes, bytearray, str]) -> str:
    if isinstance(data, (bytes, bytearray)):
        return data.decode("utf-8", errors="replace")
    return str(data)


def _meta(req: Dict[str, Any]) -> Dict[str, Any]:
    meta = req.get("meta")
    return meta if isinstance(meta, dict) else {}


def _path_base(req: Dict[str, Any]) -> str:
    return str(_meta(req).get("path") or "").split("?", 1)[0].rstrip("/") or "/"


def _http_method(req: Dict[str, Any]) -> str:
    return str(_meta(req).get("method") or "").upper()


def _is_json_body(req: Dict[str, Any]) -> bool:
    unit_type = str(req.get("unit_type") or "").lower()
    content_type = str(req.get("content_type") or "").lower()
    return unit_type == "json_body" or "application/json" in content_type


def _is_sse(req: Dict[str, Any]) -> bool:
    unit_type = str(req.get("unit_type") or "").lower()
    content_type = str(req.get("content_type") or "").lower()
    return unit_type == "sse_event" or "text/event-stream" in content_type


def _parse_request_json(payload: bytes) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(_safe_utf8(payload))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _text_from_content_block(block: Any) -> str:
    if isinstance(block, str):
        return block.strip()
    if not isinstance(block, dict):
        return ""
    for key in ("text", "content", "value"):
        val = block.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _last_user_request_match(text: str) -> Optional[re.Match[str]]:
    found: Optional[re.Match[str]] = None
    for m in _USER_REQUEST_RE.finditer(text or ""):
        found = m
    return found


def _extract_user_request_from_text(text: str) -> str:
    m = _last_user_request_match(text)
    if m:
        return m.group(2).strip()
    return (text or "").strip()


def _strip_copilot_context(text: str) -> str:
    raw = _DATETIME_RE.sub("", text or "")
    raw = _CONTEXT_BLOCK_RE.sub("", raw)
    raw = _IMAGE_PATH_RE.sub("", raw)
    return raw.strip()


def _is_nes_document(text: str) -> bool:
    return bool(_NES_DOCUMENT_RE.search(text or ""))


def _is_history_replay(text: str) -> bool:
    raw = text or ""
    return bool(_HISTORY_REPLAY_RE.search(raw)) and bool(_SUMMARY_BLOCK_RE.search(raw))


def _is_scaffold_prompt(text: str) -> bool:
    """Copilot-injected context, not text the user typed.

    The title phrase must open the message. A later chat can quote that
    phrase inside history; a substring match would drop the new question.
    """
    raw = (text or "").strip()
    if not raw:
        return False
    lowered = raw.lower()
    if lowered.startswith("please write a brief title for the following"):
        return True
    if lowered.startswith("<system_reminder") or lowered.startswith("<system-reminder"):
        return True
    if "[PREVIOUS MESSAGE]" in raw and "[CURRENT MESSAGE]" in raw:
        return True
    return False


def _finalize_user_prompt(text: str) -> str:
    # The Responses payload's final role=user entry is the turn candidate.
    # Only unwrap fields that Copilot puts around that entry itself; never
    # derive a prompt by removing text from prior instructions or history.
    raw = text or ""
    if _last_user_request_match(raw):
        raw = _extract_user_request_from_text(raw)
    raw = _strip_copilot_context(raw)
    if not raw or _is_scaffold_prompt(raw) or _is_nes_document(raw):
        return ""
    return raw


def _is_oneshot_generator(obj: Dict[str, Any]) -> bool:
    """Chat Completions call that is not a conversation turn.

    User chat on this path carries `<user_query>` / `<userRequest>`, or it
    continues a thread that already has an assistant message. A one-shot
    generator (activity summary, progress-message list, next-edit) sends
    `max_tokens` or `prediction` and a single instruction with neither.
    """
    if not isinstance(obj, dict):
        return False
    if obj.get("prediction") is not None:
        return True
    if obj.get("input"):
        return False
    messages = obj.get("messages")
    if not isinstance(messages, list) or "max_tokens" not in obj:
        return False
    saw_user = False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").lower()
        if role in ("assistant", "tool"):
            return False
        if role != "user":
            continue
        saw_user = True
        if _last_user_request_match(_user_message_text(msg.get("content"))):
            return False
    return saw_user


def _is_turn_continuation(obj: Dict[str, Any]) -> bool:
    """The agent is still working on a turn the user already sent.

    Every tool round replays the whole thread, so the last role=user entry is
    the same question as the previous round. A new question is the last entry
    in the list; model output or a tool result after it means this HTTP call
    continues the running turn. The answer still arrives on this request, so
    only the prompt side is suppressed.
    """
    if not isinstance(obj, dict):
        return False
    items = _prompt_message_list(obj)
    if not items:
        return False
    last_user = -1
    for idx, item in enumerate(items):
        if isinstance(item, dict) and str(item.get("role") or "").lower() == "user":
            last_user = idx
    if last_user < 0:
        return False
    for item in items[last_user + 1 :]:
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "").lower() in ("assistant", "tool"):
            return True
        if str(item.get("type") or "").lower() in _AGENT_WORK_ITEM_TYPES:
            return True
    return False


def _last_item_role(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("role") or "").lower()


def _is_injected_user_turn(obj: Dict[str, Any]) -> bool:
    """The client wrote this role=user entry while a turn was still running.

    A person can only type after the model closed its turn with a message,
    so a user entry that lands directly on a tool call or its result was
    appended by the editor. Compaction works that way: the token budget
    blows mid tool loop and the client asks for the summary in the user's
    place. Pinning the model to one tool, or to none, says the same thing
    from the other side: chat leaves that choice to the model.
    """
    if not isinstance(obj, dict):
        return False
    choice = obj.get("tool_choice")
    if isinstance(choice, dict):
        return True
    if isinstance(choice, str) and choice.lower() not in ("auto", "required"):
        return True
    items = _prompt_message_list(obj)
    if len(items) < 2:
        return False
    if _last_item_role(items[-1]) != "user":
        return False
    before = items[-2]
    if not isinstance(before, dict):
        return False
    return str(before.get("type") or "").lower() in _AGENT_WORK_ITEM_TYPES


def _is_resumed_history(obj: Dict[str, Any]) -> bool:
    """Post-compaction round: the user entry is the replayed thread itself.

    The client restates the earlier turns in `<user_message>` blocks next to
    the `<summary>` it just generated. Those questions are already recorded,
    so the round carries no new prompt, but the model answers the work it
    resumes and that answer is the user's.
    """
    if not isinstance(obj, dict):
        return False
    return _is_history_replay(_last_user_raw_from_list(_prompt_message_list(obj)))


def _is_thread_start(obj: Dict[str, Any]) -> bool:
    """Nothing in this conversation has been answered yet."""
    items = _prompt_message_list(obj)
    if not items:
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        if _last_item_role(item) == "assistant":
            return False
        if str(item.get("type") or "").lower() in _AGENT_WORK_ITEM_TYPES:
            return False
    return True


def _turn_key(req: Dict[str, Any], obj: Dict[str, Any]) -> str:
    """Identify the running turn: this client plus the question it replays.

    Copilot sends no thread id, but every round of one turn repeats the same
    last role=user message, which is what makes the rounds one turn.
    """
    items = _prompt_message_list(obj)
    question = ""
    for item in items:
        if isinstance(item, dict) and str(item.get("role") or "").lower() == "user":
            question = _user_message_text(item.get("content"))
    if not question:
        return ""
    client = str(_meta(req).get("client_ip") or "")
    digest = hashlib.sha1(question.encode("utf-8", "replace")).hexdigest()[:16]
    return f"{client}|{digest}"


def _client_ip(req: Dict[str, Any]) -> str:
    return str(_meta(req).get("client_ip") or "")


def _note_client_tool_wait(req: Dict[str, Any], names: Tuple[str, ...] = ()) -> None:
    client = _client_ip(req)
    if not client:
        return
    _TOOL_WAIT_BY_CLIENT[client] = (time.time(), names)
    _TOOL_WAIT_BY_CLIENT.move_to_end(client)
    while len(_TOOL_WAIT_BY_CLIENT) > 128:
        _TOOL_WAIT_BY_CLIENT.popitem(last=False)


def _clear_client_tool_wait(req: Dict[str, Any]) -> None:
    client = _client_ip(req)
    if client:
        _TOOL_WAIT_BY_CLIENT.pop(client, None)


def _pending_client_tools(req: Dict[str, Any]) -> Tuple[str, ...]:
    entry = _TOOL_WAIT_BY_CLIENT.get(_client_ip(req))
    if not entry:
        return ()
    started, names = entry
    if time.time() - started > _TOOL_WAIT_SECONDS:
        _clear_client_tool_wait(req)
        return ()
    return names


def _is_spawned_thread(req: Dict[str, Any], obj: Dict[str, Any]) -> bool:
    """A sub-agent the running turn opened, not a new question.

    The editor answers a sub-agent tool by opening a second conversation and
    feeding it the instruction the model wrote. Such a conversation has no
    model output yet and can only exist while that tool call is outstanding,
    which is what separates it from a chat someone started.
    """
    if not _is_thread_start(obj):
        return False
    return any(_SUBAGENT_TOOL_RE.search(name) for name in _pending_client_tools(req))


def _remember_turn_parent(req: Dict[str, Any], key: str) -> None:
    rid = str(req.get("request_id") or "").strip()
    if not key or not rid:
        return
    _TURN_PARENT_BY_KEY[key] = rid
    while len(_TURN_PARENT_BY_KEY) > 256:
        _TURN_PARENT_BY_KEY.popitem(last=False)


def _bind_turn_parent(req: Dict[str, Any], key: str) -> None:
    rid = str(req.get("request_id") or "").strip()
    parent = _TURN_PARENT_BY_KEY.get(key, "")
    if not rid or not parent or parent == rid:
        return
    _PARENT_BY_REQUEST[rid] = parent
    while len(_PARENT_BY_REQUEST) > 512:
        _PARENT_BY_REQUEST.popitem(last=False)


def _attach_turn_parent(req: Dict[str, Any]) -> None:
    rid = str(req.get("request_id") or "").strip()
    parent = _PARENT_BY_REQUEST.get(rid, "") if rid else ""
    if parent:
        req["_github_copilot_ui_message_id"] = parent


def _item_waits_on_client(item: Any) -> bool:
    """True for a tool the editor still has to run.

    ``call_id`` is the protocol marker for "the client must answer this".
    A server-side tool has none, and a returned result (``*_output``) carries
    one but means the result is already in hand.
    """
    if not isinstance(item, dict):
        return False
    if str(item.get("type") or "").lower().endswith("_output"):
        return False
    call_id = item.get("call_id")
    return isinstance(call_id, str) and bool(call_id.strip())


def _completed_waits_on_client(obj: Dict[str, Any]) -> bool:
    """``response.completed`` lists the whole model step.

    A client tool call in it means the turn is paused, so the sentence beside
    it ("파일을 확인하겠습니다.") is narration. The answer is the next round.
    """
    resp = obj.get("response")
    if not isinstance(resp, dict):
        return False
    output = resp.get("output")
    if not isinstance(output, list):
        return False
    return any(_item_waits_on_client(row) for row in output)


def _event_starts_tool_call(event_type: str, obj: Dict[str, Any]) -> bool:
    etype = (event_type or "").lower()
    if etype.startswith(
        (
            "response.function_call_arguments",
            "response.custom_tool_call_input",
            "response.local_shell_call",
            "response.computer_call",
        )
    ):
        return True
    if etype not in ("response.output_item.added", "response.output_item.done"):
        return False
    return _item_waits_on_client(obj.get("item"))


def _tool_name(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    name = item.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    function = item.get("function")
    if isinstance(function, dict):
        inner = function.get("name")
        if isinstance(inner, str):
            return inner.strip()
    return ""


def _completed_tool_names(obj: Dict[str, Any]) -> Tuple[str, ...]:
    resp = obj.get("response")
    output = resp.get("output") if isinstance(resp, dict) else None
    if not isinstance(output, list):
        return ()
    return tuple(
        _tool_name(row) for row in output if _item_waits_on_client(row) and _tool_name(row)
    )


def _note_tool_call_stream(stream_key: str, names: Tuple[str, ...] = ()) -> None:
    known = _TOOL_CALL_STREAMS.get(stream_key) or ()
    _TOOL_CALL_STREAMS[stream_key] = tuple(dict.fromkeys(known + names)) or ("",)
    while len(_TOOL_CALL_STREAMS) > 256:
        _TOOL_CALL_STREAMS.pop(next(iter(_TOOL_CALL_STREAMS)))


def _suppress_tool_step(
    req: Dict[str, Any], stream_key: str, names: Tuple[str, ...] = ()
) -> None:
    """This step ran a tool. Whatever it said alongside is not the answer."""
    _RESPONSE_BUFFER.pop(stream_key, None)
    _set_extract_final(req, False)
    req["_github_copilot_step_suppressed"] = True
    _note_client_tool_wait(req, tuple(name for name in names if name))


def _conversation_lists(obj: Dict[str, Any]) -> List[List[Any]]:
    found: List[List[Any]] = []
    for key in ("input", "messages"):
        value = obj.get(key)
        if isinstance(value, list):
            found.append(value)
    return found


def _last_user_raw_from_list(messages: List[Any]) -> str:
    last = ""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role") or "").lower() != "user":
            continue
        text = _user_message_text(msg.get("content"))
        if text:
            last = text
    return last


def _prompt_message_list(obj: Dict[str, Any]) -> List[Any]:
    """The list extract() will read. input wins, matching the extractor."""
    for messages in _conversation_lists(obj):
        if _last_user_raw_from_list(messages):
            return messages
    return []


def _is_hidden_title_call(obj: Dict[str, Any]) -> bool:
    """System asks for a title and the only user message is the source text.

    A follow-up /responses replays that title line in history and then adds
    the new question. More than one user message means this HTTP call is the
    chat, not the title call.
    """
    messages = _prompt_message_list(obj)
    if not messages:
        return False
    users: List[str] = []
    systems: List[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").lower()
        text = _user_message_text(msg.get("content"))
        if not text:
            continue
        if role == "user":
            users.append(text)
        elif role in ("system", "developer"):
            systems.append(text)
    if len(users) != 1:
        return False
    return any(_is_scaffold_prompt(text) and _TITLE_TURN_RE.search(text) for text in systems)


def _is_internal_copilot_turn(obj: Dict[str, Any]) -> bool:
    """Title, tool-reminder, and sentiment calls are not the user's chat turn."""
    if not isinstance(obj, dict):
        return False
    if _is_hidden_title_call(obj):
        return True
    if _is_oneshot_generator(obj):
        return True
    # Do not inspect `instructions` or earlier input items. They are context
    # carried by a normal /responses request and can contain title/reminder
    # text from an earlier Copilot operation.
    last = _last_user_raw_from_list(_prompt_message_list(obj))
    return bool(last) and not _finalize_user_prompt(last)


def _mark_internal_response(req: Dict[str, Any]) -> None:
    rid = str(req.get("request_id") or "").strip()
    if not rid or rid in _SKIP_RESPONSE_SET:
        return
    _SKIP_RESPONSE_SET.add(rid)
    _SKIP_RESPONSE_IDS.append(rid)
    while len(_SKIP_RESPONSE_IDS) > 256:
        old = _SKIP_RESPONSE_IDS.pop(0)
        _SKIP_RESPONSE_SET.discard(old)


def _response_is_internal(req: Dict[str, Any]) -> bool:
    rid = str(req.get("request_id") or "").strip()
    return bool(rid) and rid in _SKIP_RESPONSE_SET


def _user_message_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for part in content:
        text = _text_from_content_block(part)
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _extract_prompt_from_input(inp: Any) -> str:
    if not isinstance(inp, list):
        return ""
    last_user = ""
    for msg in inp:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role") or "").lower() != "user":
            continue
        text = _user_message_text(msg.get("content"))
        if text:
            last_user = text
    return _finalize_user_prompt(last_user)


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    chunks: List[str] = []
    for part in content:
        text = _text_from_content_block(part)
        if text:
            chunks.append(text)
    return "\n".join(chunks).strip()


def _extract_prompt_from_messages(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    last_user = ""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role") or "").lower() != "user":
            continue
        text = _message_content_text(msg.get("content"))
        if text:
            last_user = text
    if not last_user:
        return ""
    if _last_user_request_match(last_user):
        return _finalize_user_prompt(_extract_user_request_from_text(last_user))
    if _TITLE_REQUEST_MARKER in last_user:
        return _finalize_user_prompt(last_user.split(_TITLE_REQUEST_MARKER, 1)[1].strip())
    return _finalize_user_prompt(last_user)


def _replace_user_request_in_text(text: str, replacement: str) -> Tuple[str, bool]:
    m = _last_user_request_match(text)
    if not m:
        return text, False
    new_text = f"{text[: m.start(2)]}{replacement}{text[m.end(2) :]}"
    return new_text, True


def _derive_masking_token_pairs(source_prompt: str, masked_contents: str) -> List[Tuple[str, str]]:
    src = (source_prompt or "").strip()
    msk = (masked_contents or "").strip()
    pairs: List[Tuple[str, str]] = []
    if not src or not msk or src == msk:
        return pairs
    pairs.append((src, msk))
    if _AIVAX_MASK_TAG not in msk:
        seen_plain: set[str] = set()
        deduped_plain: List[Tuple[str, str]] = []
        for old, new in sorted(pairs, key=lambda p: len(p[0]), reverse=True):
            if not old or old in seen_plain or old == new:
                continue
            seen_plain.add(old)
            deduped_plain.append((old, new))
        return deduped_plain

    parts = msk.split(_AIVAX_MASK_TAG)
    if len(parts) == 2:
        prefix, suffix = parts[0], parts[1]
        if src.startswith(prefix) and src.endswith(suffix):
            token = src[len(prefix) : len(src) - len(suffix)].strip()
            if token and token != _AIVAX_MASK_TAG:
                pairs.append((token, _AIVAX_MASK_TAG))

    src_lines = src.splitlines()
    msk_lines = msk.splitlines()
    if len(src_lines) == len(msk_lines) and len(src_lines) > 1:
        for s_line, m_line in zip(src_lines, msk_lines):
            s_line = s_line.strip()
            m_line = m_line.strip()
            if s_line and s_line != m_line:
                pairs.append((s_line, m_line))
                if _AIVAX_MASK_TAG in m_line:
                    pairs.append((s_line, _AIVAX_MASK_TAG))

    sm = difflib.SequenceMatcher(None, src, msk)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        old_part = src[i1:i2].strip()
        new_part = msk[j1:j2].strip()
        if len(old_part) < 4:
            continue
        if tag == "replace" and old_part and new_part:
            pairs.append((old_part, new_part))
            if _AIVAX_MASK_TAG in new_part:
                pairs.append((old_part, _AIVAX_MASK_TAG))
        elif tag == "delete" and old_part:
            pairs.append((old_part, _AIVAX_MASK_TAG))

    for ip in _IP_RE.findall(src):
        if ip not in msk:
            pairs.append((ip, _AIVAX_MASK_TAG))

    seen: set[str] = set()
    deduped: List[Tuple[str, str]] = []
    for old, new in sorted(pairs, key=lambda p: len(p[0]), reverse=True):
        if not old or old in seen or old == new:
            continue
        seen.add(old)
        deduped.append((old, new))
    return deduped


def _apply_masking_to_text(
    text: str,
    source_prompt: str,
    masked_contents: str,
    token_pairs: List[Tuple[str, str]],
) -> Tuple[str, bool]:
    raw = text or ""
    if not raw:
        return raw, False
    m = _USER_REQUEST_RE.search(raw)
    if m:
        inner = m.group(2)
        new_inner = inner
        changed = False
        if inner.strip() == source_prompt:
            new_inner = masked_contents
            changed = True
        else:
            for old, new in token_pairs:
                if old and old in new_inner:
                    new_inner = new_inner.replace(old, new)
                    changed = True
        if changed:
            return f"{m.group(1)}{new_inner}{m.group(3)}", True
    new_text = raw
    changed = False
    for old, new in token_pairs:
        if old and old in new_text:
            new_text = new_text.replace(old, new)
            changed = True
    return new_text, changed


def _mutate_message_list_for_masking(
    messages: List[Any],
    source: str,
    masked: str,
    token_pairs: List[Tuple[str, str]],
) -> int:
    changed = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").lower()
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            new_c, ok = _apply_masking_to_text(content, source, masked, token_pairs)
            if ok:
                msg["content"] = new_c
                changed += 1
            continue
        if not isinstance(content, list):
            continue
        part_changed = False
        for idx, part in enumerate(content):
            if not isinstance(part, dict):
                continue
            if str(part.get("type") or "") not in ("input_text", "text", "output_text"):
                continue
            raw = str(part.get("text") or "")
            new_t, ok = _apply_masking_to_text(raw, source, masked, token_pairs)
            if ok:
                part["text"] = new_t
                content[idx] = part
                part_changed = True
                changed += 1
        if part_changed:
            msg["content"] = content
    return changed


def _collect_request_text_blobs(obj: Dict[str, Any]) -> List[str]:
    blobs: List[str] = []
    for key in ("input", "messages"):
        items = obj.get(key)
        if not isinstance(items, list):
            continue
        for msg in items:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str) and content:
                blobs.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        text = str(part.get("text") or "")
                        if text:
                            blobs.append(text)
    return blobs


def _extend_token_pairs_from_payload(
    obj: Dict[str, Any],
    token_pairs: List[Tuple[str, str]],
    masked: str,
) -> List[Tuple[str, str]]:
    if _AIVAX_MASK_TAG not in masked:
        return token_pairs
    pairs = list(token_pairs)
    seen = {old for old, _ in pairs}
    for blob in _collect_request_text_blobs(obj):
        for ip in _IP_RE.findall(blob):
            if ip and ip not in seen and ip not in masked:
                seen.add(ip)
                pairs.append((ip, _AIVAX_MASK_TAG))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def _mutate_all_user_content_for_masking(
    obj: Dict[str, Any],
    source_prompt: str,
    masked_contents: str,
) -> int:
    """MASKING: replace sensitive tokens in user/assistant turns (multi-turn history)."""
    source = (source_prompt or "").strip()
    masked = (masked_contents or "").strip()
    if not source or not masked:
        return 0
    token_pairs = _derive_masking_token_pairs(source, masked)
    token_pairs = _extend_token_pairs_from_payload(obj, token_pairs, masked)
    changed = 0
    inp = obj.get("input")
    if isinstance(inp, list):
        changed += _mutate_message_list_for_masking(inp, source, masked, token_pairs)
    msgs = obj.get("messages")
    if isinstance(msgs, list):
        changed += _mutate_message_list_for_masking(msgs, source, masked, token_pairs)
    return changed


_AT_FILE_REF_RE = re.compile(
    r"@file:\s*([^\n<]+?\.(?:png|jpe?g|gif|webp|bmp|heic|heif|pdf|tiff?))",
    re.IGNORECASE,
)


def _user_turn_has_attachments(content: Any) -> bool:
    if not isinstance(content, list):
        return False
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = str(part.get("type") or "").lower()
        if ptype in ("input_file", "input_image", "file", "image"):
            return True
        if ptype == "input_text":
            text = part.get("text")
            if isinstance(text, str) and user_text_has_image_attachment(text):
                return True
    return False


def _mutate_image_only_user_content(obj: Dict[str, Any], replacement: str) -> int:
    """Image/file-only user turn: replace content with a single input_text block."""
    changed = 0
    for key in ("input", "messages"):
        items = obj.get(key)
        if not isinstance(items, list):
            continue
        for msg in reversed(items):
            if not isinstance(msg, dict):
                continue
            if str(msg.get("role") or "").lower() != "user":
                continue
            content = msg.get("content")
            if isinstance(content, list) and _user_turn_has_attachments(content):
                msg["content"] = [{"type": "input_text", "text": replacement}]
                changed += 1
            break
    return changed


def _mutate_last_user_content(obj: Dict[str, Any], replacement: str) -> int:
    changed = 0
    for key in ("input", "messages"):
        inp = obj.get(key)
        if not isinstance(inp, list):
            continue
        for msg in reversed(inp):
            if not isinstance(msg, dict):
                continue
            if str(msg.get("role") or "").lower() != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                new_c, ok = _replace_user_request_in_text(content, replacement)
                if not ok and content.strip() != replacement:
                    new_c = replacement
                    ok = True
                if ok:
                    msg["content"] = new_c
                    changed += 1
                return changed
            if not isinstance(content, list):
                return changed
            for idx, part in enumerate(content):
                if not isinstance(part, dict):
                    continue
                if str(part.get("type") or "") not in ("input_text", "text"):
                    continue
                raw = str(part.get("text") or "")
                new_t, ok = _replace_user_request_in_text(raw, replacement)
                if not ok and raw.strip() != replacement:
                    new_t = replacement
                    ok = True
                if ok:
                    part["text"] = new_t
                    content[idx] = part
                    changed += 1
            if changed:
                msg["content"] = content
            return changed
    return changed


def _redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in _REDACT_KEYS:
                out[str(key)] = "[redacted]"
            else:
                out[str(key)] = _redact_json(item)
        return out
    if isinstance(value, list):
        return [_redact_json(item) for item in value[:40]]
    if isinstance(value, str) and len(value) > 4000:
        return value[:4000] + "…<truncated>"
    return value


def observe_copilot_request(req: Dict[str, Any], payload: bytes) -> None:
    """Record VS Code Copilot chat POSTs so an unseen path can be added."""
    if str(req.get("direction") or "").lower() != "request":
        return
    if _http_method(req) != "POST":
        return
    path = _path_base(req)
    host = str(_meta(req).get("host") or "")
    client = str(_meta(req).get("client_ip") or "")
    obj = _parse_request_json(payload) if _is_json_body(req) else None
    keys = ",".join(list(obj)[:12]) if isinstance(obj, dict) else ""
    logger.info(
        "github_copilot request host=%s path=%s bytes=%d client=%s keys=%s",
        host,
        path,
        len(payload or b""),
        client,
        keys,
    )
    if path in _COPILOT_POLICY_PATHS or not isinstance(obj, dict):
        return
    if "/engines/" in path or path.rstrip("/").endswith("/completions"):
        return
    try:
        os.makedirs(_LIVE_CAPTURE_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", path).strip("_") or "root"
        target = os.path.join(_LIVE_CAPTURE_DIR, f"{stamp}_{safe}.json")
        record = {
            "host": host,
            "path": path,
            "client_ip": client,
            "bytes": len(payload or b""),
            "body": _redact_json(obj),
        }
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("github_copilot live capture failed path=%s", path)


def _log_turn_shape(req: Dict[str, Any], obj: Dict[str, Any], decision: str) -> None:
    """Shape of a chat POST: knobs and item order, never message text.

    The turn rules key off request shape, so the shape is what has to be
    reviewable after the fact.
    """
    try:
        items = _prompt_message_list(obj)
        trail: List[str] = []
        for item in items[-6:]:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "").lower()
            role = str(item.get("role") or "").lower()
            if kind == "message" and role:
                trail.append(f"{kind}:{role}")
            else:
                trail.append(kind or role or "?")
        knobs = {
            k: obj.get(k)
            for k in ("tool_choice", "truncation", "store", "stream", "parallel_tool_calls")
            if obj.get(k) is not None and isinstance(obj.get(k), (str, bool, int))
        }
        logger.info(
            "github_copilot turn_shape path=%s client=%s decision=%s items=%d tail=%s "
            "instructions=%s tools=%d awaits=%s knobs=%s",
            _path_base(req),
            _client_ip(req),
            decision,
            len(items),
            "|".join(trail),
            bool(obj.get("instructions")),
            len(obj.get("tools") or []) if isinstance(obj.get("tools"), list) else 0,
            ",".join(_pending_client_tools(req)) or "-",
            knobs,
        )
    except Exception:
        logger.exception("github_copilot turn_shape failed")


def github_copilot_should_skip_policy(req: Dict[str, Any], payload: bytes = b"") -> bool:
    """POST /responses and /chat/completions chat bodies run input_filter."""
    observe_copilot_request(req, payload)
    if str(req.get("direction") or "").lower() != "request":
        return False
    if is_github_copilot_skip_upload_path(req):
        return True
    if _http_method(req) != "POST":
        return True
    path = _path_base(req)
    if path not in _COPILOT_POLICY_PATHS:
        return True
    if not _is_json_body(req):
        return True
    obj = _parse_request_json(payload)
    if not isinstance(obj, dict):
        return True
    if path in ("/responses", "/v1/responses") and not obj.get("input"):
        return True
    if path in ("/chat/completions", "/v1/chat/completions") and not obj.get("messages"):
        return True
    if _is_internal_copilot_turn(obj):
        _mark_internal_response(req)
        _log_turn_shape(req, obj, "internal")
        return True
    if _is_injected_user_turn(obj) or _is_spawned_thread(req, obj):
        _mark_internal_response(req)
        _log_turn_shape(req, obj, "generated")
        return True
    # A continuation carries no new prompt, but the finished answer of the
    # whole turn arrives on one of these rounds. Never mark it internal:
    # bind it to the round that logged the question instead.
    if _is_turn_continuation(obj) or _is_resumed_history(obj):
        _bind_turn_parent(req, _turn_key(req, obj))
        _log_turn_shape(req, obj, "continuation")
        return True
    _remember_turn_parent(req, _turn_key(req, obj))
    _log_turn_shape(req, obj, "user_turn")
    return False


def _chat_completion_message_text(obj: Dict[str, Any]) -> str:
    choices = obj.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, dict):
        return ""
    message = choice.get("message")
    if isinstance(message, dict):
        return _message_content_text(message.get("content"))
    return ""


def _note_turn(req: Dict[str, Any], kind: str, text: str) -> None:
    """Log a chat prompt or finished answer. Other Copilot POSTs stay quiet."""
    if _path_base(req) not in _COPILOT_POLICY_PATHS:
        return
    body = (text or "").strip()
    if not body:
        return
    client = str(_meta(req).get("client_ip") or "")
    preview = body.replace("\r", " ").replace("\n", "\\n")
    if len(preview) > 1500:
        preview = preview[:1500] + "…"
    logger.info(
        "github_copilot %s client=%s path=%s chars=%d text=%s",
        kind,
        client,
        _path_base(req),
        len(body),
        preview,
    )
    try:
        os.makedirs(_LIVE_CAPTURE_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        target = os.path.join(_LIVE_CAPTURE_DIR, f"{stamp}_{kind}.txt")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(body)
    except Exception:
        logger.exception("github_copilot %s capture failed", kind)


def extract(req: Dict[str, Any], payload: bytes) -> Optional[str]:
    direction = str(req.get("direction") or "").lower()
    if direction == "response" and _response_is_internal(req):
        req["_extract_final"] = False
        return ""
    if direction == "response":
        _attach_turn_parent(req)
    if is_github_copilot_account_path(_path_base(req)):
        if direction == "response":
            _note_account_response(req, payload)
        req["_extract_final"] = False
        return ""
    if _is_json_body(req) and direction == "request":
        obj = _parse_request_json(payload)
        if isinstance(obj, dict):
            _remember_model(req, str(obj.get("model") or ""))
            if _is_internal_copilot_turn(obj):
                _mark_internal_response(req)
                return ""
            if _is_injected_user_turn(obj) or _is_spawned_thread(req, obj):
                _mark_internal_response(req)
                return ""
            if _is_turn_continuation(obj) or _is_resumed_history(obj):
                _bind_turn_parent(req, _turn_key(req, obj))
                return ""
            _remember_turn_parent(req, _turn_key(req, obj))
            text = _extract_prompt_from_input(obj.get("input"))
            if not text:
                text = _extract_prompt_from_messages(obj.get("messages"))
            if text:
                _note_turn(req, "prompt", text)
                return text
        return ""

    if _is_sse(req) and direction == "response":
        text = _extract_sse_assistant_text(req, payload)
        if req.get("_extract_final") and text:
            # The turn spoke to the user, so no tool result is outstanding.
            _clear_client_tool_wait(req)
            _note_turn(req, "answer", text)
        return text

    if _is_json_body(req) and direction == "response":
        obj = _parse_request_json(payload)
        if isinstance(obj, dict):
            text = _chat_completion_message_text(obj)
            if text:
                _set_extract_final(req, True)
                _clear_client_tool_wait(req)
                _note_turn(req, "answer", text)
                return text
        return ""

    return _safe_utf8(payload) if payload else ""


def _set_extract_final(req: Dict[str, Any], is_final: bool) -> None:
    req["_extract_final"] = bool(is_final)


def _parse_sse_payload(text: str) -> Tuple[str, str]:
    """SSLproxy C layer passes SSE data JSON only (no event: line)."""
    raw = (text or "").strip()
    if not raw:
        return "", ""
    if raw == "[DONE]":
        return "", "[DONE]"
    if raw.startswith("{") or raw.startswith("["):
        return "", raw

    event_name = ""
    data_lines: List[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
            continue

    if data_lines:
        return event_name, "\n".join(data_lines)
    return event_name, raw


def _resolve_stream_key(req: Dict[str, Any], payload: bytes) -> str:
    """Stable per-response stream key.

    Copilot SSE assigns a unique item_id on every output_text.delta chunk, so item_id
    must not be used for override-once state.
    """
    rid = str(req.get("request_id") or "").strip()
    if rid:
        return rid
    cached = str(req.get("_github_copilot_stream_key") or "").strip()
    if cached and cached != _FALLBACK_STREAM_KEY:
        return cached
    _, data = _parse_sse_payload(_safe_utf8(payload))
    if data and data != "[DONE]":
        try:
            obj = json.loads(data)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            resp = obj.get("response")
            if isinstance(resp, dict):
                response_id = str(resp.get("id") or "").strip()
                if response_id:
                    req["_github_copilot_stream_key"] = response_id
                    return response_id
    return _FALLBACK_STREAM_KEY


def _split_sse_events(text: str) -> List[Tuple[str, str]]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    events: List[Tuple[str, str]] = []
    for block in blocks:
        event_name, data = _parse_sse_payload(block)
        if data:
            events.append((event_name, data))
    return events


def _extract_sse_assistant_text(req: Dict[str, Any], payload: bytes) -> Optional[str]:
    text = _safe_utf8(payload)
    # The inline filter flushes the whole SSE body at end_stream. Walk every
    # event so the answer is not just the first delta.
    events = _split_sse_events(text)
    if len(events) > 1:
        # A finished answer is often followed by [DONE], which carries no text
        # and clears `_extract_final`. Keep the flag with the text. A frame
        # that resolves the step to a tool call does the opposite: the text it
        # was narrating is dropped.
        last = ""
        saw_final = False
        for _, data in events:
            req.pop("_github_copilot_step_suppressed", None)
            piece = _extract_sse_assistant_text(req, data.encode("utf-8"))
            if req.get("_github_copilot_step_suppressed"):
                last = ""
                saw_final = False
                continue
            if piece:
                last = piece
                saw_final = bool(req.get("_extract_final"))
        req.pop("_github_copilot_step_suppressed", None)
        _set_extract_final(req, bool(last and saw_final))
        return last
    stream_key = _resolve_stream_key(req, payload)
    req["_github_copilot_stream_key"] = stream_key

    event_name, data = _parse_sse_payload(text)

    if not data:
        _set_extract_final(req, False)
        return _RESPONSE_BUFFER.get(stream_key) or ""

    if data == "[DONE]":
        merged = _RESPONSE_BUFFER.pop(stream_key, "")
        _set_extract_final(req, bool(merged))
        return merged

    try:
        obj = json.loads(data)
    except Exception:
        _set_extract_final(req, False)
        return _RESPONSE_BUFFER.get(stream_key) or ""

    if not isinstance(obj, dict):
        _set_extract_final(req, False)
        return ""

    event_type = str(obj.get("type") or event_name or "")

    if event_type == "response.output_text.delta":
        delta = obj.get("delta")
        if isinstance(delta, str) and delta:
            merged = _RESPONSE_BUFFER.get(stream_key, "") + delta
            _RESPONSE_BUFFER[stream_key] = merged
            _set_extract_final(req, False)
            return merged
        _set_extract_final(req, False)
        return _RESPONSE_BUFFER.get(stream_key) or ""

    if _event_starts_tool_call(event_type, obj):
        _note_tool_call_stream(stream_key, (_tool_name(obj.get("item")),))
        _set_extract_final(req, False)
        return ""

    if event_type == "response.output_text.done":
        done_text = obj.get("text")
        if isinstance(done_text, str) and done_text:
            # Hold the snapshot. Whether this step speaks to the user or only
            # narrates a tool call is decided by the completed frame.
            _RESPONSE_BUFFER[stream_key] = done_text
            _set_extract_final(req, False)
            return done_text

    if event_type == "response.completed":
        seen = _TOOL_CALL_STREAMS.pop(stream_key, ())
        names = tuple(dict.fromkeys(seen + _completed_tool_names(obj)))
        if seen or _completed_waits_on_client(obj):
            _suppress_tool_step(req, stream_key, names)
            return ""
        resp = obj.get("response")
        if isinstance(resp, dict):
            out = resp.get("output")
            if isinstance(out, list):
                merged = _extract_prompt_from_output_items(out)
                if merged:
                    _RESPONSE_BUFFER.pop(stream_key, None)
                    _set_extract_final(req, True)
                    return merged
        merged = _RESPONSE_BUFFER.pop(stream_key, "")
        _set_extract_final(req, bool(merged))
        return merged

    choices = obj.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        choice = choices[0]
        delta = choice.get("delta")
        if isinstance(delta, dict):
            if delta.get("tool_calls") or delta.get("function_call"):
                calls = delta.get("tool_calls")
                if not isinstance(calls, list):
                    calls = [delta.get("function_call")]
                _note_tool_call_stream(
                    stream_key, tuple(_tool_name(call) for call in calls if _tool_name(call))
                )
            piece = delta.get("content")
            if isinstance(piece, str) and piece:
                merged = _RESPONSE_BUFFER.get(stream_key, "") + piece
                _RESPONSE_BUFFER[stream_key] = merged
                _set_extract_final(req, False)
                return merged
        message_text = _chat_completion_message_text(obj)
        if message_text:
            _RESPONSE_BUFFER.pop(stream_key, None)
            _set_extract_final(req, True)
            return message_text
        finish_reason = str(choice.get("finish_reason") or "").lower()
        if finish_reason:
            names = _TOOL_CALL_STREAMS.pop(stream_key, ())
            if names or finish_reason in ("tool_calls", "function_call"):
                _suppress_tool_step(req, stream_key, names)
                return ""
            merged = _RESPONSE_BUFFER.pop(stream_key, "")
            _set_extract_final(req, bool(merged))
            return merged

    _set_extract_final(req, False)
    return _RESPONSE_BUFFER.get(stream_key) or ""


def _extract_prompt_from_output_items(output: List[Any]) -> str:
    parts: List[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, list):
            for block in content:
                t = _text_from_content_block(block)
                if t:
                    parts.append(t)
        elif isinstance(content, str) and content.strip():
            parts.append(content.strip())
    return "\n".join(parts).strip()


def is_github_copilot_account_path(path: str) -> bool:
    base = str(path or "").split("?", 1)[0].rstrip("/").lower() or "/"
    return base in _COPILOT_ACCOUNT_PATHS


def _bearer(req: Dict[str, Any]) -> str:
    auth = str(_meta(req).get("authorization") or "").strip()
    lowered = auth.lower()
    if lowered.startswith("bearer "):
        return auth[7:].strip()
    if lowered.startswith("token "):
        return auth[6:].strip()
    return ""


def _client_ip(req: Dict[str, Any]) -> str:
    return str(_meta(req).get("client_ip") or "").strip()


def _token_fingerprint(token: str) -> str:
    token = (token or "").strip()
    if not token or _account_lookup is None:
        return ""
    try:
        return _account_lookup.fingerprint_account_secret(token)
    except Exception:
        return ""


def _person_name(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text or len(text) > 80 or "@" in text:
        return ""
    lowered = text.lower()
    if "/" in text or "\\" in text or lowered.startswith("http"):
        return ""
    return text


def _login_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 80 or any(ch.isspace() for ch in text) or "@" in text:
        return ""
    if text.lower() in ("unknown", "none", "null"):
        return ""
    return text


def _email_address(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 120 or "@" not in text or any(ch.isspace() for ch in text):
        return ""
    return text


def _model_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 80:
        return ""
    if text.lower() in ("user", "assistant", "system", "model", "human", "bot", "tool", "none", "null"):
        return ""
    if any(ord(ch) < 32 for ch in text):
        return ""
    return text


def _remember_model(req: Dict[str, Any], model: str) -> None:
    model = _model_name(model)
    if not model:
        return
    req["_llm_model"] = model
    req["_copilot_model"] = model
    client = _client_ip(req)
    if not client:
        return
    if client in _MODEL_BY_CLIENT:
        _MODEL_BY_CLIENT.pop(client, None)
    _MODEL_BY_CLIENT[client] = model
    while len(_MODEL_BY_CLIENT) > 256:
        _MODEL_BY_CLIENT.pop(next(iter(_MODEL_BY_CLIENT)))


def _lookup_model(req: Dict[str, Any], payload: bytes) -> str:
    for key in ("_llm_model", "_copilot_model"):
        found = _model_name(req.get(key))
        if found:
            return found
    obj = _parse_request_json(payload)
    if isinstance(obj, dict):
        found = _model_name(obj.get("model"))
        if found:
            _remember_model(req, found)
            return found
    client = _client_ip(req)
    return _model_name(_MODEL_BY_CLIENT.get(client)) if client else ""


def _account_keys(req: Dict[str, Any]) -> List[str]:
    keys: List[str] = []
    seen = set()

    def add(key: str) -> None:
        key = (key or "").strip()
        if not key or key in seen:
            return
        seen.add(key)
        keys.append(key)

    token_key = _token_fingerprint(_bearer(req))
    if token_key:
        add(token_key)
        add(_SESSION_TO_GITHUB.get(token_key, ""))
        for session_key in _GITHUB_SESSIONS.get(token_key, []):
            add(session_key)
    client = _client_ip(req)
    if client:
        add(f"ip:{client}")
    return keys


def _store_account(req: Dict[str, Any], user_id: str, email: str, name: str) -> None:
    user_id = (user_id or "").strip()
    email = _email_address(email)
    name = _person_name(name)
    if not _account_lookup or not (user_id or email or name):
        return
    client = _client_ip(req)
    for key in _account_keys(req):
        _account_lookup.set_account(
            _ACCOUNT_SERVICE,
            key,
            user_id,
            email,
            client_ip=client,
            display_name=name,
            source="explicit",
        )
    logger.info(
        "[Account Info] github_copilot: user_id=%s email=%s name=%s client=%s",
        user_id or "-",
        email or "-",
        name or "-",
        client or "-",
    )


def _lookup_account(req: Dict[str, Any]) -> Tuple[str, str, str]:
    if not _account_lookup or not hasattr(_account_lookup, "get_result"):
        return "", "", ""
    for key in _account_keys(req):
        try:
            info = _account_lookup.get_result(_ACCOUNT_SERVICE, key)
        except Exception:
            info = None
        if info is None:
            continue
        user_id = str(getattr(info, "user_id", "") or "").strip()
        email = _email_address(getattr(info, "email", ""))
        name = _person_name(getattr(info, "display_name", ""))
        if user_id or email or name:
            return user_id, email, name
    return "", "", ""


def _link_session_token(req: Dict[str, Any], session_token: str) -> None:
    github_key = _token_fingerprint(_bearer(req))
    session_key = _token_fingerprint(session_token)
    if not github_key or not session_key or github_key == session_key:
        return
    _SESSION_TO_GITHUB[session_key] = github_key
    sessions = _GITHUB_SESSIONS.setdefault(github_key, [])
    if session_key not in sessions:
        sessions.append(session_key)
    while len(_SESSION_TO_GITHUB) > 512:
        old_session, old_github = next(iter(_SESSION_TO_GITHUB.items()))
        _SESSION_TO_GITHUB.pop(old_session, None)
        linked = _GITHUB_SESSIONS.get(old_github) or []
        _GITHUB_SESSIONS[old_github] = [item for item in linked if item != old_session]
    user_id, email, name = _lookup_account(req)
    if user_id or email or name:
        _store_account(req, user_id, email, name)


def _email_from_list(value: Any) -> str:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return ""
    ranked: List[Tuple[int, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        email = _email_address(item.get("email"))
        if not email:
            continue
        score = 0
        if item.get("primary") is True:
            score += 2
        if item.get("verified") is True:
            score += 1
        ranked.append((score, email))
    if not ranked:
        return ""
    ranked.sort(key=lambda row: row[0], reverse=True)
    return ranked[0][1]


def _profile_from_obj(obj: Dict[str, Any]) -> Tuple[str, str, str]:
    login = _login_name(obj.get("login") or obj.get("username"))
    email = _email_address(obj.get("email")) or _email_from_list(obj.get("emails"))
    name = _person_name(obj.get("name") or obj.get("display_name"))
    user_id = login
    if not user_id:
        raw_id = obj.get("id")
        if isinstance(raw_id, int) or (isinstance(raw_id, str) and raw_id.strip().isdigit()):
            user_id = str(raw_id).strip()
    return user_id, email, name


def _note_account_response(req: Dict[str, Any], payload: bytes) -> None:
    obj = _parse_request_json(payload)
    path = _path_base(req)
    if path in ("/copilot_internal/v2/token", "/copilot_internal/token"):
        if isinstance(obj, dict):
            token = obj.get("token")
            if isinstance(token, str):
                _link_session_token(req, token)
        return
    if path == "/user/emails":
        try:
            parsed = json.loads(_safe_utf8(payload))
        except Exception:
            parsed = None
        email = _email_from_list(parsed)
        if email:
            user_id, _old_email, name = _lookup_account(req)
            _store_account(req, user_id, email, name)
        return
    if not isinstance(obj, dict):
        return
    user_id, email, name = _profile_from_obj(obj)
    if not email:
        email = _email_from_list(obj.get("emails"))
    if user_id or email or name:
        _store_account(req, user_id, email, name)


def _attach_identity(payload_obj: Dict[str, Any], req: Dict[str, Any], body: bytes) -> Dict[str, Any]:
    user_id, email, user_name = _lookup_account(req)
    model = _lookup_model(req, body)
    payload_obj["user_id"] = user_id
    payload_obj["email"] = email
    if user_name:
        payload_obj["user_name"] = user_name
    if model:
        payload_obj["model"] = model
    return payload_obj


def build_policy_api_payload(
    req: Dict[str, Any], payload: bytes, extracted_text: str
) -> Optional[Dict[str, Any]]:
    direction = str(req.get("direction") or "").lower()
    meta = _meta(req)
    client_host = str(meta.get("client_ip") or meta.get("host") or "")

    if direction == "response":
        session_id = str(
            req.get("_github_copilot_stream_key")
            or extract_session_id(req, payload)
            or ""
        )
        return _attach_identity(
            {
                "llm_output": extracted_text,
                "user_id": "",
                "email": "",
                "ai_service": AI_SERVICE_GITHUB_COPILOT,
                "client_host": client_host,
                "session_id": session_id,
                "message_id": str(req.get("request_id") or ""),
            },
            req,
            payload,
        )

    obj = _parse_request_json(payload)
    if not isinstance(obj, dict):
        return None
    if isinstance(obj, dict):
        _remember_model(req, str(obj.get("model") or ""))
    attachments = build_policy_attachments(obj, payload, req)

    return _attach_identity(
        {
            "filter_list": ["input_filter", "file_block_filter", "secret_filter", "slm_filter"],
            "prompt": extracted_text,
            "encoding": False,
            "user_id": "",
            "email": "",
            "ai_service": AI_SERVICE_GITHUB_COPILOT,
            "client_host": client_host,
            "session_id": "",
            "attachments": attachments,
            "message_id": "",
        },
        req,
        payload,
    )


def _rewrite_request_payload(
    payload: bytes,
    replacement: str,
    *,
    allow_empty: bool = False,
    source_prompt: str = "",
    masking: bool = False,
) -> Optional[Tuple[bytes, int]]:
    if not allow_empty and not (replacement or "").strip():
        return None
    obj = _parse_request_json(payload)
    if not isinstance(obj, dict):
        return None
    if masking and (source_prompt or "").strip():
        changed = _mutate_all_user_content_for_masking(
            obj, (source_prompt or "").strip(), (replacement or "").strip()
        )
    else:
        changed = _mutate_last_user_content(obj, replacement)
        if changed <= 0:
            changed = _mutate_image_only_user_content(obj, replacement)
    if changed <= 0:
        return None
    removed = strip_request_attachments(obj)
    
    encoded = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return encoded, changed


_COPILOT_FILE_PART_TYPES = ("input_file", "input_image", "file", "image")


def _consent_copy(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _is_copilot_file_part(part: Any) -> bool:
    return (
        isinstance(part, dict)
        and str(part.get("type") or "").lower() in _COPILOT_FILE_PART_TYPES
    )


def _copilot_last_user_item(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for key in ("input", "messages"):
        items = obj.get(key)
        if not isinstance(items, list):
            continue
        for msg in reversed(items):
            if isinstance(msg, dict) and str(msg.get("role") or "").lower() == "user":
                return msg
    return None


def consent_attachment_state(req: Dict[str, Any], payload: bytes) -> Optional[Dict[str, Any]]:
    """File refs of the blocked turn. The upload itself already reached Copilot."""
    if str(req.get("direction") or "").lower() != "request" or not _is_json_body(req):
        return None
    obj = _parse_request_json(payload)
    if not isinstance(obj, dict):
        return None
    top = (
        _consent_copy(obj["attachments"])
        if isinstance(obj.get("attachments"), list) and obj["attachments"]
        else None
    )
    item_attachments = None
    parts: List[Any] = []
    msg = _copilot_last_user_item(obj)
    if isinstance(msg, dict):
        att = msg.get("attachments")
        if isinstance(att, list) and att:
            item_attachments = _consent_copy(att)
        content = msg.get("content")
        if isinstance(content, list):
            parts = [_consent_copy(p) for p in content if _is_copilot_file_part(p)]
    if top is None and item_attachments is None and not parts:
        return None
    return {"top": top, "item_attachments": item_attachments, "parts": parts}


def restore_consent_attachments(
    req: Dict[str, Any],
    payload: bytes,
    state: Any,
    prompt: str = "",
) -> Optional[bytes]:
    """Re-add the blocked turn's file refs that the rewrite stripped."""
    del prompt
    if not isinstance(state, dict) or not _is_json_body(req):
        return None
    obj = _parse_request_json(payload)
    if not isinstance(obj, dict):
        return None
    restored = 0
    top = state.get("top")
    if isinstance(top, list):
        obj["attachments"] = _consent_copy(top)
        restored += 1
    item_attachments = state.get("item_attachments")
    parts = state.get("parts") or []
    if isinstance(item_attachments, list) or parts:
        msg = _copilot_last_user_item(obj)
        if isinstance(msg, dict):
            if isinstance(item_attachments, list):
                msg["attachments"] = _consent_copy(item_attachments)
                restored += 1
            content = msg.get("content")
            if parts and isinstance(content, list):
                kept = [p for p in content if not _is_copilot_file_part(p)]
                msg["content"] = [_consent_copy(p) for p in parts] + kept
                restored += 1
    if restored <= 0:
        return None
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def rewrite_prompt(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
    policy_code: str = "",
) -> Optional[Tuple[bytes, str]]:
    if str(req.get("direction") or "").lower() != "request" or not _is_json_body(req):
        return None
    out = _rewrite_request_payload(payload, replacement_text)
    if not out:
        return None
    reason = "adapter:github_copilot_policy_rewrite"
    if str(policy_code or "").strip().upper() == "BLOCK":
        reason = "adapter:github_copilot_policy_block"
    return out[0], reason


def rewrite_prompt_masking(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
    source_prompt: str = "",
) -> Optional[Tuple[bytes, str]]:
    masked = (replacement_text or "").strip()
    source = (source_prompt or "").strip()
    text = masked or source
    if not text:
        return None
    out = _rewrite_request_payload(
        payload,
        text,
        source_prompt=source,
        masking=bool(source and masked),
    )
    if not out:
        return None
    return out[0], "adapter:github_copilot_policy_masking"


def rewrite_prompt_for_block(
    req: Dict[str, Any],
    payload: bytes,
    user_prompt: str,
    system_instruction: str,
) -> Optional[Tuple[bytes, str]]:
    if str(req.get("direction") or "").lower() != "request" or not _is_json_body(req):
        return None
    sys_t = (system_instruction or "").strip()
    user_t = (user_prompt or "").strip()
    if sys_t and user_t:
        combined = f"{user_t}\n{_BLOCK_SYSINST_VERTICAL_GAP}{sys_t}"
    else:
        combined = user_t or sys_t
    if not combined:
        return None
    out = _rewrite_request_payload(payload, combined)
    if not out:
        return None
    return out[0], "adapter:github_copilot_policy_block_sysinstruct"


def rewrite(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
    policy_code: str = "",
) -> Optional[Tuple[bytes, str]]:
    return rewrite_prompt(req, payload, replacement_text, policy_code)


def extract_session_id(req: Dict[str, Any], payload: bytes) -> str:
    if _is_sse(req) and str(req.get("direction") or "").lower() == "response":
        key = _resolve_stream_key(req, payload)
        if key != _FALLBACK_STREAM_KEY:
            return key
    obj = _parse_request_json(payload)
    if isinstance(obj, dict):
        for key in ("previous_response_id", "response_id", "id"):
            val = str(obj.get(key) or "").strip()
            if val:
                return val
    return str(req.get("request_id") or "")


def response_stream_done(req: Dict[str, Any], payload: bytes) -> bool:
    if not _is_sse(req):
        return False
    text = _safe_utf8(payload)
    event_name, data = _parse_sse_payload(text)
    if data == "[DONE]":
        return True
    if not data:
        return False
    try:
        obj = json.loads(data)
    except Exception:
        return False
    if not isinstance(obj, dict):
        return False
    event_type = str(obj.get("type") or event_name or "")
    if event_type in ("response.completed", "response.output_text.done"):
        return True
    choices = obj.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return bool(choices[0].get("finish_reason"))
    return False


def _rewrite_chat_completion_chunk(obj: Dict[str, Any], replacement: str) -> bool:
    choices = obj.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return False
    choice = choices[0]
    delta = choice.get("delta")
    if isinstance(delta, dict) and isinstance(delta.get("content"), str):
        if delta["content"] == replacement:
            return False
        delta["content"] = replacement
        return True
    message = choice.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        if message["content"] == replacement:
            return False
        message["content"] = replacement
        return True
    return False


def _rewrite_sse_data_obj(obj: Dict[str, Any], event_name: str, replacement: str, state_key: str) -> int:
    emitted = bool(_RESPONSE_OVERRIDE_EMITTED.get(state_key, False))
    changed = 0
    target = replacement if not emitted else ""
    if event_name == "response.output_text.delta":
        if isinstance(obj.get("delta"), str) and obj["delta"] != target:
            obj["delta"] = target
            changed += 1
            emitted = True
    elif _rewrite_chat_completion_chunk(obj, target):
        changed += 1
        emitted = True
    elif event_name == "response.output_text.done":
        if isinstance(obj.get("text"), str) and obj["text"] != target:
            obj["text"] = target
            changed += 1
            emitted = True
    elif event_name == "response.completed":
        resp = obj.get("response")
        if isinstance(resp, dict):
            out = resp.get("output")
            if isinstance(out, list):
                for item in out:
                    if not isinstance(item, dict):
                        continue
                    content = item.get("content")
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and isinstance(block.get("text"), str):
                                if block["text"] != target:
                                    block["text"] = target
                                    changed += 1
                                    emitted = True
    _RESPONSE_OVERRIDE_EMITTED[state_key] = emitted
    if event_name == "response.completed":
        _RESPONSE_OVERRIDE_EMITTED.pop(state_key, None)
    return changed


def rewrite_response(
    req: Dict[str, Any],
    payload: bytes,
    replacement_text: str,
) -> Optional[Tuple[bytes, str]]:
    if not _is_sse(req) or str(req.get("direction") or "").lower() != "response":
        return None
    text = _safe_utf8(payload).replace("\r\n", "\n").replace("\r", "\n")
    state_key = _resolve_stream_key(req, payload)
    req["_github_copilot_stream_key"] = state_key

    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
        except Exception:
            return None
        if isinstance(obj, dict):
            event_name = str(obj.get("type") or "")
            changed = _rewrite_sse_data_obj(obj, event_name, replacement_text, state_key)
            if changed > 0:
                new_data = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
                return new_data.encode("utf-8"), "adapter:github_copilot_response_override"
        return None

    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    if not blocks:
        return None

    changed_total = 0
    new_blocks: List[str] = []
    for block in blocks:
        event_name = ""
        data_raw = ""
        lines = block.split("\n")
        for line in lines:
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_raw = line[5:].strip()
        if not data_raw:
            new_blocks.append(block)
            continue
        if data_raw == "[DONE]":
            new_blocks.append(block)
            continue
        try:
            obj = json.loads(data_raw)
        except Exception:
            new_blocks.append(block)
            continue
        if isinstance(obj, dict):
            changed_total += _rewrite_sse_data_obj(obj, event_name, replacement_text, state_key)
            new_data = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            rebuilt = []
            for line in lines:
                if line.startswith("data:"):
                    rebuilt.append(f"data: {new_data}")
                else:
                    rebuilt.append(line)
            new_blocks.append("\n".join(rebuilt))
        else:
            new_blocks.append(block)

    if changed_total <= 0:
        return None
    new_text = "\n\n".join(new_blocks) + ("\n\n" if text.endswith("\n\n") else "")
    return new_text.encode("utf-8"), "adapter:github_copilot_response_override"


def clear_response_override_state(session_id: str) -> None:
    if not session_id:
        return
    _RESPONSE_BUFFER.pop(session_id, None)
    _RESPONSE_OVERRIDE_EMITTED.pop(session_id, None)
