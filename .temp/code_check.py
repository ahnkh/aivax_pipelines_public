import re
import json
import logging
import asyncio
from logging.handlers import RotatingFileHandler
from typing import Optional, List, Tuple
from pydantic import BaseModel, Field

from llamafirewall import (
    LlamaFirewall,
    Role,
    ScannerType,
    AssistantMessage,
)

CODE_BLOCK_RE = re.compile(
    r"```(?P<lang>[a-zA-Z0-9_\-\+\.#]*)\n(?P<code>.*?)(?:```)", re.DOTALL
)


def extract_code_blocks(text: str) -> List[Tuple[str, str]]:
    """응답에서 (언어, 코드) 튜플 리스트 추출"""
    blocks = []
    for m in CODE_BLOCK_RE.finditer(text or ""):
        lang = (m.group("lang") or "").strip().lower()
        code = m.group("code").strip()
        if code:
            blocks.append((lang, code))
    return blocks


class Pipeline:
    class Valves(BaseModel):
        pipelines: List[str] = Field(
            default_factory=lambda: ["*"],
            description="이 필터를 적용할 파이프라인 이름 리스트. 기본은 전체('*').",
        )
        priority: int = Field(
            default=0,
            description="필터 실행 우선순위",
        )

        enabled: bool = Field(
            default=True,
            description="필터 전역 ON/OFF",
        )
        block_on_findings: bool = Field(
            default=False,
            description="취약점 발견 시 전체 응답 차단(기본 False: 경고/리포트만 첨부)",
        )
        min_severity_to_block: float = Field(
            default=0.5,
            description="block_on_findings=True일 때 차단 임계 점수(0~1)",
        )
        annotate_on_safe: bool = Field(
            default=False,
            description="취약점이 없어도 '검사 통과' 배지 첨부",
        )

        # Logging options
        logging_enabled: bool = Field(
            default=True,
            description="스캔 결과를 로그로 남길지 여부",
        )
        log_path: str = Field(
            default="codeshield_scan.log",
            description="로그 파일 경로",
        )
        log_max_bytes: int = Field(
            default=10 * 1024 * 1024,
            description="로그 파일 롤링 최대 바이트 (기본 10MB)",
        )
        log_backup_count: int = Field(
            default=5,
            description="롤링시 보관할 백업 파일 수",
        )
        log_level: str = Field(
            default="INFO",
            description="로그 레벨 (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
        )
        log_to_console: bool = Field(
            default=True,
            description="콘솔에도 로그 출력 (개발/디버그용)",
        )

    def __init__(self):
        # OpenWebUI 파이프라인 메타데이터
        self.type = "filter"
        self.id = "code_check_filter"
        self.name = "code_check_filter"

        self.valves = self.Valves()
        self.toggle = True
        self.icon = (
            "data:image/svg+xml;base64,"
            "PHN2ZyB3aWR0aD0iMjQiIGhlaWdodD0iMjQiIHZpZXdCb3g9IjAgMCAyNCAyNCIg"
            "ZmlsbD0ibm9uZSIgc3Ryb2tlPSJjdXJyZW50Q29sb3IiIHN0cm9rZS13aWR0aD0i"
            "MS41Ij48cGF0aCBkPSJNMTIgM0wzIDEybDkgOSA5LTkiLz48L3N2Zz4="
        )

        # CodeShield 스캐너 (Assistant 출력 대상)
        self.firewall = LlamaFirewall(
            scanners={Role.ASSISTANT: [ScannerType.CODE_SHIELD]}
        )

        # logger 설정
        self.logger = self._setup_logger()

    # ===== Logger =====
    def _setup_logger(self) -> logging.Logger:
        """로거 초기화 (RotatingFileHandler + optional console)"""
        logger = logging.getLogger("codeshield_filter")

        # 여러 번 초기화되는 환경에서 핸들러 중복 방지
        if not logger.handlers:
            level = getattr(logging, self.valves.log_level.upper(), logging.INFO)
            logger.setLevel(level)
            fmt = logging.Formatter(
                "%(asctime)s %(levelname)s [%(name)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )

            if self.valves.logging_enabled:
                try:
                    fh = RotatingFileHandler(
                        filename=self.valves.log_path,
                        maxBytes=self.valves.log_max_bytes,
                        backupCount=self.valves.log_backup_count,
                        encoding="utf-8",
                    )
                    fh.setFormatter(fmt)
                    fh.setLevel(level)
                    logger.addHandler(fh)
                except Exception as e:
                    # 파일 핸들러 생성 실패 시 콘솔로 대체
                    sh = logging.StreamHandler()
                    sh.setFormatter(fmt)
                    logger.addHandler(sh)
                    logger.error("로그 파일 핸들러 생성 실패, 콘솔로 대체합니다: %s", str(e))

            if self.valves.log_to_console:
                ch = logging.StreamHandler()
                ch.setFormatter(fmt)
                ch.setLevel(level)
                logger.addHandler(ch)

            # 상위 로거로 전파 방지(중복 출력 방지)
            logger.propagate = False
            logger.info(
                "CodeShield filter logger initialized. log_path=%s, level=%s",
                self.valves.log_path,
                self.valves.log_level.upper(),
            )
        else:
            # 레벨만 최신 설정으로 맞춤
            try:
                logger.setLevel(getattr(logging, self.valves.log_level.upper(), logging.INFO))
            except Exception:
                logger.setLevel(logging.INFO)
            logger.propagate = False

        return logger

    def _jlog(self, level: str, payload: dict):
        """JSON 로그 유틸리티(ensure_ascii=False + 예외 안전)"""
        if not self.valves.logging_enabled:
            return
        try:
            msg = json.dumps(payload, ensure_ascii=False)
            getattr(self.logger, level.lower(), self.logger.info)(msg)
        except Exception:
            self.logger.exception("JSON 로그 기록 중 예외: %s", repr(payload))

    def stream(self, event: dict) -> dict:
        return event

    # --- 스레드에서 실행될 동기 스캔 함수 ---
    def _scan_once(self, lang: str, code: str) -> dict:
        try:
            scan_result = self.firewall.scan(AssistantMessage(content=code))
            return {
                "lang": lang or "unknown",
                "decision": getattr(scan_result, "decision", None),
                "reason": getattr(scan_result, "reason", ""),
                "score": float(getattr(scan_result, "score", 2)),
            }
        except Exception as e:
            return {
                "lang": lang or "unknown",
                "decision": "scan_error",
                "reason": f"scan failed: {str(e)}",
                "score": 0.0,
            }

    async def outlet(
        self,
        body: dict,
        __event_emitter__=None,
        __user__: Optional[dict] = None,
    ) -> dict:
        if not self.valves.enabled:
            return body

        messages = body.get("messages", [])
        if not messages:
            return body

        # 마지막 assistant 메시지 대상으로만 검사
        last = next((m for m in reversed(messages) if m.get("role") == "assistant"), None)
        if not last:
            return body

        # --- 중복 검사 방지: 스캔 플래그가 있으면 스킵 ---
        if last.get("_codeshield_scanned", False):
            self._jlog("info", {"event": "skip_already_flagged"})
            return body

        content = last.get("content", "") or ""

        # --- 중복 검사 방지: 이미 리포트 마커가 본문에 포함되어 있으면 스킵 ---
        if (
            "<summary>CodeShield 결과 보기</summary>" in content
            or "CodeShield 검사 통과" in content
            or "CodeShield 경고" in content
        ):
            # 그래도 플래그를 남겨 다음 호출에서 안전하게 스킵하게 함
            last["_codeshield_scanned"] = True
            self._jlog("info", {"event": "skip_already_report", "message_excerpt": (content[:200] + "...") if len(content) > 200 else content})
            return body

        code_blocks = extract_code_blocks(content)

        # 코드 블록이 없을 경우: annotate_on_safe 옵션으로 통과 배지 추가하고 플래그 설정
        if not code_blocks:
            if self.valves.annotate_on_safe:
                last["content"] = f"{content}\n\n> CodeShield: 코드 블록 없음 — 검사 통과"
            last["_codeshield_scanned"] = True
            self._jlog("info", {
                "event": "no_code_blocks",
                "message_excerpt": (content[:200] + "...") if len(content) > 200 else content,
            })
            return body

        # --- 스캔을 스레드로 넘김 (예외를 결과로 수집) ---
        tasks = [asyncio.to_thread(self._scan_once, lang, code) for (lang, code) in code_blocks]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        # 결과 정리: 스레드 예외도 `scan_error` 형태로 변환
        results: List[dict] = []
        worst_score = 0.0
        for idx, rr in enumerate(raw_results):
            lang, code = code_blocks[idx]
            if isinstance(rr, Exception):
                res = {
                    "lang": lang or "unknown",
                    "decision": "scan_error",
                    "reason": f"scan failed with exception: {repr(rr)}",
                    "score": 0.0,
                }
            else:
                # rr는 dict 형태(_scan_once 반환)
                res = rr
            results.append(res)
            worst_score = max(worst_score, float(res.get("score", 0.0)))

            # 블록 단위 로그
            self._jlog("info", {
                "event": "block_scan",
                "lang": res.get("lang", lang or "unknown"),
                "decision": res.get("decision"),
                "score": res.get("score"),
                "reason": res.get("reason"),
                "code_excerpt": (code[:500] + "...") if len(code) > 500 else code,
            })

        # 이슈 판단 (태그용)
        has_issue = any(
            (str(r.get("decision", "")).lower().endswith("block") or float(r.get("score", 0.0)) >= 0.5)
            for r in results
        )

        # 요약 로그
        self._jlog("info", {
            "event": "scan_summary",
            "num_blocks": len(results),
            "worst_score": worst_score,
            "has_issue": bool(has_issue),
            "block_on_findings": bool(self.valves.block_on_findings),
            "min_severity_to_block": self.valves.min_severity_to_block,
        })

        # (옵션) 차단 모드 - 기본 False. 필요 없으면 이 블록을 통째로 제거하세요.
        if has_issue and self.valves.block_on_findings and worst_score >= self.valves.min_severity_to_block:
            report_md = self._render_report(results)
            last["content"] = (
                "CodeShield가 잠재적으로 위험한 코드 출력을 감지하여 응답을 차단했습니다.\n\n"
                + report_md
            )
            last["_codeshield_scanned"] = True
            self._jlog("warning", {
                "event": "response_blocked",
                "worst_score": worst_score,
                "findings": results,
            })
            return body

        # 경고/리포트 주석 덧붙임 (태그 포함 — 중복 생성 피하기 위해 플래그 설정)
        tag = "CodeShield 경고" if has_issue else "CodeShield 검사 통과"
        report_md = self._render_report(results)

        # 안전하게 기존 content를 그대로 유지하고 태그+리포트 붙임
        last["content"] = f"{content}\n\n> {tag}\n\n{report_md}"

        # 플래그 기록 — 같은 메시지에 다시 리포트가 붙지 않게 함
        last["_codeshield_scanned"] = True

        # 최종 상태 로그
        self._jlog("info", {
            "event": "response_emitted",
            "has_issue": bool(has_issue),
            "worst_score": worst_score,
            "findings_count": len(results),
        })

        return body


    def _render_report(self, findings: List[dict]) -> str:
        """간단한 마크다운 리포트 생성"""
        if not findings:
            return "> 검사 결과 없음"

        lines = ["<details>\n<summary>CodeShield 결과 보기</summary>\n\n"]
        lines.append("| # | 언어 | 결정 | 점수 | 이유 |")
        lines.append("|---:|:----|:-----|:----:|:-----|")
        for i, f in enumerate(findings, 1):
            lines.append(
                f"| {i} | `{f.get('lang','unknown')}` | `{f.get('decision')}` | {float(f.get('score',0.0)):.2f} | {f.get('reason','')} |"
            )
        lines.append("\n</details>")
        return "\n".join(lines)
