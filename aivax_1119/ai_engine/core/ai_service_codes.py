"""Policy API ai_service numeric codes (aligned with src/ai_service.h enum)."""

AI_SERVICE_GPT = 1
AI_SERVICE_GEMINI = 2
AI_SERVICE_CLAUDE = 3
AI_SERVICE_GROK = 4
AI_SERVICE_PERPLEXITY = 5
AI_SERVICE_NOTION = 6
AI_SERVICE_WRKS = 7
AI_SERVICE_CHAT_VERSION = 8
AI_SERVICE_GITHUB_COPILOT = 9
AI_SERVICE_TIMELY = 12
AI_SERVICE_GENSPARK = 13

SERVICE_ID_TO_CODE = {
    "gpt": AI_SERVICE_GPT,
    "chatgpt": AI_SERVICE_GPT,
    "gemini": AI_SERVICE_GEMINI,
    "claude": AI_SERVICE_CLAUDE,
    "grok": AI_SERVICE_GROK,
    "perplexity": AI_SERVICE_PERPLEXITY,
    "notion": AI_SERVICE_NOTION,
    "wrks": AI_SERVICE_WRKS,
    "chat_version": AI_SERVICE_CHAT_VERSION,
    "github_copilot": AI_SERVICE_GITHUB_COPILOT,
    "timely": AI_SERVICE_TIMELY,
    "genspark": AI_SERVICE_GENSPARK,
}


def resolve_ai_service_code(adapter_service_id: str) -> int:
    sid = (adapter_service_id or "").lower().strip()
    return int(SERVICE_ID_TO_CODE.get(sid, 0))
