"""로컬 문서·OCR 도구를 adapter 안에서 사용하는 구현 세부사항."""

from __future__ import annotations

import fcntl
import io
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import fitz
import pdfplumber
import pytesseract
from anyio.to_thread import run_sync
from PIL import Image, ImageEnhance

from src.core.file import (
    LocalDocumentTextExtractor,
    LocalFileMetadataReader,
    LocalFileOperations,
    LocalPdfTextExtractor,
    PdfTextExtraction,
)
from src.core.file.file_metadata import extension_allowed
from src.core.hash import HashGenerator
from src.domain.inspection_multimodal.models import (
    DocumentEvidence,
    DocumentEvidenceResult,
    DocumentSource,
    FileMetadata,
    MultimodalFileInspectionResult,
    MultimodalFileLimitReason,
    MultimodalInspectionRequest,
    MultimodalInspectionResult,
    MultimodalInspectionTarget,
    MultimodalMatchedPolicy,
    MultimodalPolicyMatch,
    WatermarkCriteria,
    WatermarkInspectionResult,
)
from src.domain.inspection_multimodal.ports import MultimodalInspectionPort
from src.domain.policy_multimodal.models import (
    MultimodalPolicyRule,
    MultimodalPolicySnapshot,
)


@dataclass(frozen=True, slots=True)
class _CompiledMultimodalRule:
    rule: MultimodalPolicyRule
    pattern: re.Pattern[str]


class LocalMultimodalInspectionAdapter(MultimodalInspectionPort):
    """로컬 파일·문서 파서·OCR을 사용하는 멀티모달 검사 adapter."""

    _LOGGER = logging.getLogger("khan-python")
    _EVIDENCE_TTL_SECONDS = 30 * 60
    _ACTION_PRIORITY = ("block", "masking", "accept", "")
    _SCOPE_ORDER = ("user", "service", "group", "default")
    _CATEGORY = "멀티 모달 분석"
    _EXTENSION_LIMIT_NAME = "파일 확장자 제한"
    _SIZE_LIMIT_NAME = "파일 사이즈 제한"
    _DRM_POLICY_NAME = "Watermark 파일 차단"
    _OCR_POLICY_NAME = "DRM 반출 불가 문서 차단"
    _EVIDENCE_CONVERTIBLE_MIME_TYPES = {
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.hancom.hwpx",
        "application/x-hwp",
        "application/zip",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }

    def __init__(
        self,
        *,
        ocr_max_inflight: int = 2,
        ocr_timeout_seconds: int = 60,
        pdf_evidence_engine: str = "pdfplumber",
    ) -> None:
        if ocr_max_inflight < 1:
            raise ValueError("ocr_max_inflight는 1 이상이어야 합니다.")
        if ocr_timeout_seconds < 1:
            raise ValueError("ocr_timeout_seconds는 1 이상이어야 합니다.")
        if pdf_evidence_engine not in {"pdfplumber", "pymupdf"}:
            raise ValueError(
                "pdf_evidence_engine는 pdfplumber 또는 pymupdf여야 합니다."
            )
        self._metadata_reader = LocalFileMetadataReader()
        self._text_extractor = LocalDocumentTextExtractor()
        self._pdf_text_extractor = LocalPdfTextExtractor()
        self._pdf_evidence_engine = pdf_evidence_engine
        self._compiled_policy: MultimodalPolicySnapshot | None = None
        self._compiled_rules: tuple[_CompiledMultimodalRule, ...] = ()
        self._compile_lock = threading.Lock()
        self._ocr_semaphore = threading.BoundedSemaphore(ocr_max_inflight)
        self._ocr_timeout_seconds = int(ocr_timeout_seconds)

    async def inspect(
        self,
        request: MultimodalInspectionRequest,
    ) -> MultimodalInspectionResult:
        if request.options.bypass or not request.targets:
            return MultimodalInspectionResult()
        return await run_sync(partial(self._inspect_sync, request))

    # 레거시 FileBlockFilterPattern이 제거될 때 함께 삭제한다. 신규 경로는
    # MultimodalInspectionPort.inspect()만 사용한다.
    def describe(self, source: DocumentSource) -> FileMetadata:
        return self._metadata_reader.read(source.path)

    def extract_text(
        self,
        source: DocumentSource,
        metadata: FileMetadata,
        timeout_seconds: int,
    ) -> str:
        return self._text_extractor.extract(source.path, metadata, timeout_seconds)

    def inspect_drm(
        self,
        source: DocumentSource,
        criteria: WatermarkCriteria,
    ) -> WatermarkInspectionResult:
        return self._inspect_drm(source, criteria)

    def inspect_ocr(
        self,
        source: DocumentSource,
        criteria: WatermarkCriteria,
    ) -> WatermarkInspectionResult:
        return self._inspect_ocr(source, criteria)

    def collect_evidence(
        self,
        source: DocumentSource,
        metadata: FileMetadata,
        pattern: re.Pattern[str],
        temporary_directory: str,
        timeout_seconds: int,
    ) -> DocumentEvidenceResult:
        return self._collect_evidence(
            source,
            metadata,
            pattern,
            temporary_directory,
            timeout_seconds,
        )

    def _inspect_sync(
        self,
        request: MultimodalInspectionRequest,
    ) -> MultimodalInspectionResult:
        rules = self._current_rules(request)
        files = tuple(
            result
            for target in request.targets
            if (result := self._inspect_target(target, request, rules)) is not None
        )
        selected = self._select_file(files)
        return MultimodalInspectionResult(
            action=selected.action if selected is not None else "",
            selected_policy=selected.policy if selected is not None else None,
            files=files,
        )

    def _inspect_target(
        self,
        target: MultimodalInspectionTarget,
        request: MultimodalInspectionRequest,
        rules: tuple[_CompiledMultimodalRule, ...],
    ) -> MultimodalFileInspectionResult | None:
        source = target.source
        if not target.file_name or not source.path.is_file():
            self._LOGGER.error(
                "첨부파일이 존재하지 않아 검사를 건너뜁니다 path=%s name=%s",
                source.path,
                target.file_name,
            )
            return None

        metadata = self._metadata_reader.read(source.path)
        limit = self._file_limit(metadata, request)
        if limit is not None:
            policy_name, handling, limit_reason = limit
            if handling == "skip":
                return MultimodalFileInspectionResult(
                    source=source,
                    file_name=target.file_name,
                    metadata=metadata,
                    inspection_status="skipped",
                    skip_reason=policy_name,
                    limit_reason=limit_reason,
                )
            return self._blocked_file(source, target.file_name, metadata, policy_name)

        watermark_policy = self._watermark_policy(
            source,
            request.options.watermark_criteria,
        )
        if watermark_policy is not None:
            return self._blocked_file(
                source,
                target.file_name,
                metadata,
                watermark_policy,
            )
        return self._inspect_contents(
            source, target.file_name, metadata, request, rules
        )

    def _inspect_contents(
        self,
        source: DocumentSource,
        file_name: str,
        metadata: FileMetadata,
        request: MultimodalInspectionRequest,
        rules: tuple[_CompiledMultimodalRule, ...],
    ) -> MultimodalFileInspectionResult:
        file_hash = HashGenerator.sha256_file(source.path)
        pdf_text: PdfTextExtraction | None = None
        try:
            if (
                metadata.mime_type == "application/pdf"
                and self._pdf_evidence_engine == "pymupdf"
            ):
                pdf_text = self._pdf_text_extractor.extract(source.path)
                extracted_text = pdf_text.text
            else:
                extracted_text = self._text_extractor.extract(
                    source.path,
                    metadata,
                    request.options.read_timeout_seconds,
                )
        except Exception:
            self._LOGGER.exception(
                "fail extract document text: %s name=%s", source.path, file_name
            )
            extracted_text = ""
        content_chunk: str | None = (
            extracted_text[: request.options.content_chunk_size]
            if request.options.content_chunk_size > 0
            else None
        )

        matches: list[MultimodalPolicyMatch] = []
        seen_policy_ids: set[str] = set()
        for compiled in rules:
            if matches and compiled.rule.scope != matches[0].policy.scope:
                break
            if compiled.rule.id and compiled.rule.id in seen_policy_ids:
                continue
            match = compiled.pattern.search(extracted_text)
            if match is None:
                continue
            evidence = (
                self._evidence_from_pdf_text(pdf_text, compiled.pattern)
                if pdf_text is not None
                else self._collect_evidence(
                    source,
                    metadata,
                    compiled.pattern,
                    request.options.detail_reason_directory,
                    request.options.read_timeout_seconds,
                )
            )
            policy = self._matched_policy(compiled.rule, match.group())
            matches.append(MultimodalPolicyMatch(policy, evidence.evidence))
            seen_policy_ids.add(policy.id)
            if policy.action == "block":
                break

        return MultimodalFileInspectionResult(
            source=source,
            file_name=file_name,
            metadata=metadata,
            matches=tuple(matches),
            file_hash=file_hash,
            content_chunk=content_chunk,
            content_inspected=True,
        )

    @classmethod
    def _blocked_file(
        cls,
        source: DocumentSource,
        file_name: str,
        metadata: FileMetadata,
        policy_name: str,
    ) -> MultimodalFileInspectionResult:
        return MultimodalFileInspectionResult(
            source=source,
            file_name=file_name,
            metadata=metadata,
            matches=(
                MultimodalPolicyMatch(
                    MultimodalMatchedPolicy(
                        name=policy_name,
                        action="block",
                        target=cls._CATEGORY,
                        category="",
                    ),
                ),
            ),
        )

    @classmethod
    def _matched_policy(
        cls,
        rule: MultimodalPolicyRule,
        matched_text: str,
    ) -> MultimodalMatchedPolicy:
        return MultimodalMatchedPolicy(
            id=rule.id,
            name=rule.name,
            action=rule.action,
            target=rule.target,
            category=rule.category or "",
            scope=rule.scope,
            matched_text=matched_text,
            expression=rule.expression,
            flags=rule.flags,
        )

    def _current_rules(
        self,
        request: MultimodalInspectionRequest,
    ) -> tuple[_CompiledMultimodalRule, ...]:
        snapshot = request.policy
        if snapshot is not self._compiled_policy:
            with self._compile_lock:
                if snapshot is not self._compiled_policy:
                    self._compiled_rules = tuple(
                        _CompiledMultimodalRule(
                            rule,
                            re.compile(rule.expression, rule.flags),
                        )
                        for rule in snapshot.rules
                    )
                    self._compiled_policy = snapshot
        group_members = {
            group_id: frozenset(emails) for group_id, emails in snapshot.group_members
        }
        applicable_by_scope = {
            scope: tuple(
                compiled
                for compiled in self._compiled_rules
                if compiled.rule.scope == scope
                and self._matches_subject(
                    compiled.rule,
                    request,
                    group_members,
                )
            )
            for scope in self._SCOPE_ORDER
        }
        return tuple(
            compiled
            for scope in self._SCOPE_ORDER
            for compiled in applicable_by_scope[scope]
        )

    @staticmethod
    def _matches_subject(
        rule: MultimodalPolicyRule,
        request: MultimodalInspectionRequest,
        group_members: dict[str, frozenset[str]],
    ) -> bool:
        if rule.scope == "default":
            return True
        if rule.scope == "user":
            return bool(request.email.strip()) and (
                request.email.strip().lower() == rule.subject_id.strip().lower()
            )
        if rule.scope == "service":
            return str(request.ai_service_id).strip() == rule.subject_id.strip()
        if rule.scope == "group":
            return request.email.strip().lower() in group_members.get(
                rule.subject_id,
                frozenset(),
            )
        return False

    @classmethod
    def _file_limit(
        cls,
        metadata: FileMetadata,
        request: MultimodalInspectionRequest,
    ) -> tuple[str, str, MultimodalFileLimitReason] | None:
        snapshot = request.policy
        if snapshot.allowed_extensions and not extension_allowed(
            metadata.extension, snapshot.allowed_extensions
        ):
            return (
                f"{cls._EXTENSION_LIMIT_NAME} ({metadata.extension})",
                cls._handling(request.options.unsupported_file_handling),
                MultimodalFileLimitReason.UNSUPPORTED_EXTENSION,
            )
        if (
            snapshot.max_size_bytes > 0
            and metadata.size_bytes > snapshot.max_size_bytes
        ):
            return (
                f"{cls._SIZE_LIMIT_NAME} ({metadata.size_bytes}byte)",
                cls._handling(request.options.oversized_file_handling),
                MultimodalFileLimitReason.OVERSIZED,
            )
        return None

    @staticmethod
    def _handling(value: str) -> str:
        return value if value in {"block", "skip"} else "block"

    @classmethod
    def _select_file(
        cls,
        files: tuple[MultimodalFileInspectionResult, ...],
    ) -> MultimodalFileInspectionResult | None:
        selected_by_action = {item.action: item for item in files}
        for action in cls._ACTION_PRIORITY:
            if action in selected_by_action:
                return selected_by_action[action]
        return None

    def _watermark_policy(
        self,
        source: DocumentSource,
        criteria: WatermarkCriteria,
    ) -> str | None:
        if self._inspect_drm(source, criteria).detected:
            return self._DRM_POLICY_NAME
        if self._inspect_ocr(source, criteria).detected:
            return self._OCR_POLICY_NAME
        return None

    def _inspect_drm(
        self,
        source: DocumentSource,
        criteria: WatermarkCriteria,
    ) -> WatermarkInspectionResult:
        path = source.path
        if not criteria.drm_enabled:
            return WatermarkInspectionResult()
        try:
            pattern = re.compile(
                criteria.drm_pattern.encode("utf-8"),
                criteria.drm_pattern_flags,
            )
            header = LocalFileOperations.read_header(
                path,
                criteria.drm_header_limit,
            )
            match = pattern.search(header)
        except Exception:
            self._LOGGER.exception("fail inspect DRM watermark: %s", path)
            return WatermarkInspectionResult()
        if match is None:
            return WatermarkInspectionResult()
        return WatermarkInspectionResult(
            detected=True,
            kind="drm",
            offset=match.start(),
            length=match.end() - match.start(),
            matched_text=match.group().decode("ascii", errors="ignore"),
        )

    def _inspect_ocr(
        self,
        source: DocumentSource,
        criteria: WatermarkCriteria,
    ) -> WatermarkInspectionResult:
        path = source.path
        if not criteria.ocr_enabled or not self._has_pdf_header(path):
            return WatermarkInspectionResult()
        try:
            patterns = [
                re.compile(pattern, criteria.ocr_pattern_flags)
                for pattern in criteria.ocr_patterns
            ]
            with fitz.open(path) as document:
                page_numbers = criteria.ocr_pages
                if len(document) <= len(page_numbers):
                    page_numbers = (0,)
                for page_number in page_numbers:
                    page = document[page_number]
                    ocr_text = self._run_ocr(page)
                    combined_text = f"{page.get_text()}\n{ocr_text}"
                    match_count = sum(
                        1 for pattern in patterns if pattern.search(combined_text)
                    )
                    if match_count >= criteria.ocr_hit_threshold:
                        return WatermarkInspectionResult(
                            detected=True,
                            kind="ocr",
                            page_number=page_number,
                            match_count=match_count,
                            match_threshold=criteria.ocr_hit_threshold,
                        )
        except Exception:
            self._LOGGER.exception("fail inspect OCR watermark: %s", path)
        return WatermarkInspectionResult()

    def _collect_evidence(
        self,
        source: DocumentSource,
        metadata: FileMetadata,
        pattern: re.Pattern[str],
        temporary_directory: str,
        timeout_seconds: int,
    ) -> DocumentEvidenceResult:
        path = source.path
        try:
            if metadata.mime_type == "application/pdf":
                return self._collect_pdf_evidence(path, pattern)
            if metadata.mime_type in self._EVIDENCE_CONVERTIBLE_MIME_TYPES:
                with self._temporary_evidence_directory(temporary_directory) as workdir:
                    converted = self._text_extractor.convert_to_pdf(
                        path,
                        workdir,
                        timeout_seconds,
                        extension=metadata.extension,
                    )
                    if converted is not None:
                        return self._collect_pdf_evidence(converted, pattern)
        except Exception:
            self._LOGGER.exception("fail collect document evidence: %s", path)
        return DocumentEvidenceResult()

    @staticmethod
    @contextmanager
    def _temporary_evidence_directory(directory: str) -> Iterator[Path]:
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        # worker별 검사와 cron 모두 같은 lock을 사용한다. 사용 중인 결과는
        # 오래 걸린 검사라도 삭제하지 않으며, 파일명 충돌은 요청별 경로로 막는다.
        with (root / ".cleanup.lock").open("a") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_SH)
            workdir = Path(tempfile.mkdtemp(prefix="evidence-", dir=root))
            try:
                yield workdir
            finally:
                # 정상/오류 처리 종료 후 30분 보관. 삭제는 cleanup cron이 담당한다.
                os.utime(workdir, None)
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def expire_temporary_evidence(self, directory: str) -> int:
        root = Path(directory)
        if not root.is_dir():
            return 0
        deleted = 0
        with (root / ".cleanup.lock").open("a") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            try:
                cutoff = time.time() - self._EVIDENCE_TTL_SECONDS
                for path in root.iterdir():
                    if path.is_symlink():
                        continue
                    try:
                        if path.stat().st_mtime > cutoff:
                            continue
                        if path.is_dir() and path.name.startswith("evidence-"):
                            shutil.rmtree(path)
                        elif path.is_file() and path.suffix.lower() == ".pdf":
                            # 기존 구현이 root에 남긴 변환 PDF도 처리한다.
                            path.unlink()
                        else:
                            continue
                        deleted += 1
                    except FileNotFoundError:
                        continue
                    except OSError:
                        self._LOGGER.exception(
                            "temporary evidence cleanup failed %s", path
                        )
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
        return deleted

    @staticmethod
    def _has_pdf_header(path: Path) -> bool:
        try:
            return LocalFileOperations.read_header(path, 5) == b"%PDF-"
        except OSError:
            return False

    def _ocr_page(self, page) -> str:
        pixmap = page.get_pixmap(dpi=300)
        image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("L")
        image = ImageEnhance.Contrast(image).enhance(3.0)
        image = image.point(
            [0 if value < 180 else 255 for value in range(256)],
            "1",
        )
        return pytesseract.image_to_string(
            image,
            lang="kor",
            config="--psm 11",
            timeout=self._ocr_timeout_seconds,
        )

    def _run_ocr(self, page) -> str:
        with self._ocr_semaphore:
            return self._ocr_page(page)

    def _collect_pdf_evidence(
        self,
        path: Path,
        pattern: re.Pattern[str],
    ) -> DocumentEvidenceResult:
        if self._pdf_evidence_engine == "pymupdf":
            return self._evidence_from_pdf_text(
                self._pdf_text_extractor.extract(path), pattern
            )
        return self._collect_pdfplumber_evidence(path, pattern)

    @staticmethod
    def _evidence_from_pdf_text(
        pdf_text: PdfTextExtraction,
        pattern: re.Pattern[str],
    ) -> DocumentEvidenceResult:
        evidence: list[DocumentEvidence] = []
        for line in pdf_text.lines:
            for match in pattern.finditer(line.text):
                evidence.append(
                    DocumentEvidence(
                        sequence=len(evidence) + 1,
                        page_number=line.page_number,
                        line_number=line.line_number,
                        context=line.text.strip(),
                        matched_text=match.group(),
                    )
                )
        return DocumentEvidenceResult(tuple(evidence))

    @staticmethod
    def _collect_pdfplumber_evidence(
        path: Path,
        pattern: re.Pattern[str],
    ) -> DocumentEvidenceResult:
        evidence: list[DocumentEvidence] = []
        with pdfplumber.open(path) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                lines: dict[float, list[str]] = {}
                for word in page.extract_words(use_text_flow=True):
                    lines.setdefault(round(word["top"], 1), []).append(word["text"])
                for line_number, (_, texts) in enumerate(
                    sorted(lines.items()), start=1
                ):
                    context = " ".join(texts)
                    for match in pattern.finditer(context):
                        evidence.append(
                            DocumentEvidence(
                                sequence=len(evidence) + 1,
                                page_number=page_number,
                                line_number=line_number,
                                context=context.strip(),
                                matched_text=match.group(),
                            )
                        )
        return DocumentEvidenceResult(tuple(evidence))
