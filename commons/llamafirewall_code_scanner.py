import re
import asyncio
from typing import List, Tuple, Dict, Any


def extract_code_blocks(text: str) -> List[Tuple[str, str]]:
    code_blocks = []
    pattern = re.compile(r"```(\w+)?\n(.*?)```", re.DOTALL)
    matches = pattern.findall(text)
    for lang, code in matches:
        code_blocks.append((lang or "plaintext", code.strip()))
    return code_blocks


class CodeShieldScanner:

    def __init__(self, client=None):
        self.client = client

    async def scan_user_blocks(self, blocks: List[Tuple[str, str]]) -> Tuple[List[Dict[str, Any]], bool]:
        results = []
        for lang, code in blocks:
            severity = self._evaluate_risk(code)
            results.append({
                "source": "user",
                "language": lang,
                "severity": severity,
            })
        return results, True

    async def scan_assistant_blocks(self, blocks: List[Tuple[str, str]]) -> Tuple[List[Dict[str, Any]], bool]:
        results = []
        for lang, code in blocks:
            severity = self._evaluate_risk(code)
            results.append({
                "source": "assistant",
                "language": lang,
                "severity": severity,
            })
        return results, True

    def has_issue(self, results: List[Dict[str, Any]], threshold: float = 0.5) -> bool:
        return any(r["severity"] >= threshold for r in results)

    def _evaluate_risk(self, code: str) -> float:
        risky_patterns = [r"exec\(", r"eval\(", r"os\.system", r"subprocess", r"socket", r"requests", r"open\("]
        score = sum(bool(re.search(p, code)) for p in risky_patterns)
        return min(score / len(risky_patterns), 1.0)



class ReportFormatter:
    def render_report(self, results: List[Dict[str, Any]]) -> str:
        if not results:
            return "\nCodeShield 검사 통과 (분석된 코드 없음)"
        summary = "\n<details><summary>CodeShield 결과 보기</summary>\n\n"
        for r in results:
            summary += f"- 소스 : {r['source']} | 언어: {r['language']} | 결과: {r['message']}\n"
        summary += "\n</details>"
        return summary

    def render_user_warning(self, results: List[Dict[str, Any]]) -> str:
        return (
            "\nCodeShield 경고: 위험한 코드가 감지되었습니다. 실행을 중단하세요.\n"
            + self.render_report(results)
        )

    def render_block_message(self, results: List[Dict[str, Any]]) -> str:
        return (
            "\nCodeShield 차단: 모델 응답에 보안상 위험 코드가 포함되어 있습니다.\n"
            + self.render_report(results)
        )
