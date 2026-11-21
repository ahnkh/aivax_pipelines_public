"""단일 로컬 멀티모달 검사 adapter 계약 테스트."""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import fitz
import pytest
from PIL import Image

import src.core.file.local_document_text_extractor as extractor_module
from src.adapters.inspection_multimodal import LocalMultimodalInspectionAdapter
from src.domain.inspection_multimodal.models import (
    DocumentEvidence,
    DocumentEvidenceResult,
    DocumentSource,
    FileMetadata,
    MultimodalFileInspectionResult,
    MultimodalInspectionOptions,
    MultimodalInspectionRequest,
    MultimodalInspectionTarget,
    WatermarkCriteria,
)
from src.domain.policy_multimodal.models import (
    MultimodalPolicyRule,
    MultimodalPolicySnapshot,
)


def _criteria() -> WatermarkCriteria:
    return WatermarkCriteria(
        drm_enabled=True,
        drm_pattern="DRMONE",
        drm_pattern_flags=0,
        drm_header_limit=1024,
        ocr_enabled=True,
        ocr_patterns=("confidential", "internal"),
        ocr_pattern_flags=0,
        ocr_hit_threshold=2,
        ocr_pages=(0,),
    )


def test_adapter_detects_drm_from_file_header(tmp_path: Path) -> None:
    path = tmp_path / "document"
    path.write_bytes(b"prefix-DRMONE-suffix")

    result = asyncio.run(
        LocalMultimodalInspectionAdapter().inspect(
            MultimodalInspectionRequest(
                targets=(
                    MultimodalInspectionTarget(
                        DocumentSource.from_path(path),
                        "document.pdf",
                    ),
                ),
                policy=MultimodalPolicySnapshot(),
                options=MultimodalInspectionOptions(
                    bypass=False,
                    read_timeout_seconds=30,
                    unsupported_file_handling="block",
                    oversized_file_handling="block",
                    content_chunk_size=0,
                    detail_reason_directory="/tmp",
                    watermark_criteria=_criteria(),
                ),
            ),
        )
    )

    assert result.action == "block"
    assert result.selected_policy is not None
    assert result.selected_policy.name == "Watermark 파일 차단"


def test_adapter_collects_pdf_evidence(monkeypatch) -> None:
    expected = DocumentEvidenceResult(
        (
            DocumentEvidence(
                sequence=1,
                page_number=2,
                line_number=4,
                context="contains secret value",
                matched_text="secret",
            ),
        )
    )
    adapter = LocalMultimodalInspectionAdapter()
    monkeypatch.setattr(adapter, "_collect_pdf_evidence", lambda *_: expected)

    result = adapter._collect_evidence(
        DocumentSource.from_path("/tmp/document.pdf"),
        FileMetadata("application/pdf", "pdf", 20),
        re.compile("secret"),
        "/tmp",
        30,
    )

    assert result == expected


def test_adapter_owns_local_content_analysis(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "document.txt"
    path.write_text("contains secret")
    adapter = LocalMultimodalInspectionAdapter()
    monkeypatch.setattr(
        adapter._metadata_reader,
        "read",
        lambda _: FileMetadata("text/plain", "txt", path.stat().st_size),
    )
    monkeypatch.setattr(
        adapter._text_extractor,
        "extract",
        lambda *_: "contains secret",
    )
    expected_evidence = DocumentEvidenceResult(
        (
            DocumentEvidence(
                sequence=1,
                page_number=0,
                line_number=0,
                context="contains secret",
                matched_text="secret",
            ),
        )
    )
    monkeypatch.setattr(adapter, "_collect_evidence", lambda *_: expected_evidence)

    result = asyncio.run(
        adapter.inspect(
            MultimodalInspectionRequest(
                targets=(
                    MultimodalInspectionTarget(
                        DocumentSource.from_path(path),
                        "document.txt",
                    ),
                ),
                policy=MultimodalPolicySnapshot(
                    rules=(
                        MultimodalPolicyRule(
                            id="rule-1",
                            name="Secret",
                            expression="secret",
                            action="block",
                            target="file",
                        ),
                    )
                ),
                options=MultimodalInspectionOptions(
                    bypass=False,
                    read_timeout_seconds=30,
                    unsupported_file_handling="block",
                    oversized_file_handling="block",
                    content_chunk_size=20,
                    detail_reason_directory="/tmp",
                    watermark_criteria=WatermarkCriteria(
                        drm_enabled=False,
                        drm_pattern="",
                        drm_pattern_flags=0,
                        drm_header_limit=0,
                        ocr_enabled=False,
                        ocr_patterns=(),
                        ocr_pattern_flags=0,
                        ocr_hit_threshold=0,
                        ocr_pages=(),
                    ),
                ),
            )
        )
    )

    assert result.action == "block"
    assert result.selected_policy is not None
    assert result.selected_policy.id == "rule-1"
    assert result.files[0].content_chunk == "contains secret"
    assert result.files[0].evidence == expected_evidence.evidence


def _inspect_policy_document(
    tmp_path, rules, *, group_members=(), texts=("alpha beta alpha",)
):
    targets = []
    for index, text in enumerate(texts):
        path = tmp_path / f"policies-{index}.pdf"
        with fitz.open() as document:
            document.new_page().insert_text((72, 72), text)
            document.save(path)
        targets.append(
            MultimodalInspectionTarget(DocumentSource.from_path(path), path.name)
        )
    return asyncio.run(
        LocalMultimodalInspectionAdapter(pdf_evidence_engine="pymupdf").inspect(
            MultimodalInspectionRequest(
                targets=tuple(targets),
                policy=MultimodalPolicySnapshot(
                    rules=tuple(rules), group_members=group_members
                ),
                options=MultimodalInspectionOptions(
                    bypass=False,
                    read_timeout_seconds=30,
                    unsupported_file_handling="block",
                    oversized_file_handling="block",
                    content_chunk_size=100,
                    detail_reason_directory=str(tmp_path),
                    watermark_criteria=replace(
                        _criteria(), drm_enabled=False, ocr_enabled=False
                    ),
                ),
                email="user@example.com",
                ai_service_id=7,
            )
        )
    )


@pytest.mark.parametrize(
    "actions,expected_action,expected_ids",
    [
        (("accept", "accept"), "accept", ["alpha", "beta"]),
        (("accept", "block"), "block", ["alpha", "beta"]),
        (("block", "accept"), "block", ["alpha"]),
    ],
)
def test_content_policies_accumulate_until_block(
    tmp_path, actions, expected_action, expected_ids
):
    result = _inspect_policy_document(
        tmp_path,
        [
            MultimodalPolicyRule(id=text, name=text, expression=text, action=action)
            for text, action in zip(("alpha", "beta"), actions, strict=True)
        ],
    )

    assert result.action == expected_action
    matches = result.files[0].matches
    assert [item.policy.id for item in matches] == expected_ids
    assert [item.matched_text for item in matches[0].evidence] == ["alpha", "alpha"]
    if len(matches) > 1:
        assert [item.matched_text for item in matches[1].evidence] == ["beta"]


@pytest.mark.parametrize("matched_scope", ["user", "service", "group", "default"])
def test_content_scope_falls_back_only_until_a_scope_matches(tmp_path, matched_scope):
    subjects = {
        "user": "user@example.com",
        "service": "7",
        "group": "team",
        "default": "",
    }
    scopes = tuple(subjects)
    selected_index = scopes.index(matched_scope)
    rules = [
        MultimodalPolicyRule(
            id=scope,
            name=scope,
            expression="missing" if index < selected_index else "alpha",
            action="accept" if index == selected_index else "block",
            scope=scope,
            subject_id=subjects[scope],
        )
        for index, scope in enumerate(scopes)
    ]
    rules.append(replace(rules[selected_index], id="second", expression="beta"))

    result = _inspect_policy_document(
        tmp_path, rules, group_members=(("team", ("user@example.com",)),)
    )

    assert result.action == "accept"
    assert [item.policy.id for item in result.files[0].matches] == [
        matched_scope,
        "second",
    ]


def test_content_group_matches_do_not_duplicate_shared_policy(tmp_path):
    shared = MultimodalPolicyRule(
        id="shared",
        name="shared",
        expression="alpha",
        action="accept",
        scope="group",
        subject_id="first",
    )
    result = _inspect_policy_document(
        tmp_path,
        (
            shared,
            replace(shared, subject_id="second"),
            replace(shared, id="other", expression="beta"),
        ),
        group_members=(
            ("first", ("user@example.com",)),
            ("second", ("user@example.com",)),
        ),
    )

    assert [item.policy.id for item in result.files[0].matches] == ["shared", "other"]


@pytest.mark.parametrize("scope", ["user", "service", "group"])
def test_content_ignores_policies_for_other_subjects(tmp_path, scope):
    result = _inspect_policy_document(
        tmp_path,
        (
            MultimodalPolicyRule(
                id="other",
                name="other",
                expression="alpha",
                action="block",
                scope=scope,
                subject_id="not-this-subject",
            ),
            MultimodalPolicyRule(
                id="default", name="default", expression="alpha", action="accept"
            ),
        ),
    )

    assert result.action == "accept"
    assert [item.policy.id for item in result.files[0].matches] == ["default"]


def test_files_select_scope_independently_and_continue_after_block(tmp_path):
    result = _inspect_policy_document(
        tmp_path,
        (
            MultimodalPolicyRule(
                id="user",
                name="user",
                expression="alpha",
                action="accept",
                scope="user",
                subject_id="user@example.com",
            ),
            MultimodalPolicyRule(
                id="default", name="default", expression="beta", action="block"
            ),
        ),
        texts=("beta", "alpha"),
    )

    assert result.action == "block"
    assert [item.action for item in result.files] == ["block", "accept"]
    assert [item.policy.id for item in result.files] == ["default", "user"]


@pytest.mark.parametrize(
    "allowed_extensions,max_size_bytes,expected_policy,expected_ocr_calls",
    [
        (("webp",), 0, "Secret", 1),
        (("png",), 0, "파일 확장자 제한 (webp)", 0),
        (("webp",), 1, "파일 사이즈 제한", 0),
    ],
)
def test_webp_content_inspection_respects_file_limits(
    tmp_path: Path,
    monkeypatch,
    allowed_extensions: tuple[str, ...],
    max_size_bytes: int,
    expected_policy: str,
    expected_ocr_calls: int,
) -> None:
    path = tmp_path / "upload"
    with Image.new("RGB", (24, 16), color="white") as image:
        image.save(path, format="WEBP")
    ocr = Mock(return_value="contains secret")
    monkeypatch.setattr(extractor_module.pytesseract, "image_to_string", ocr)

    result = asyncio.run(
        LocalMultimodalInspectionAdapter().inspect(
            MultimodalInspectionRequest(
                targets=(
                    MultimodalInspectionTarget(
                        DocumentSource.from_path(path), "attachment.webp"
                    ),
                ),
                policy=MultimodalPolicySnapshot(
                    rules=(
                        MultimodalPolicyRule(
                            id="secret-rule",
                            name="Secret",
                            expression="secret",
                            action="block",
                        ),
                    ),
                    allowed_extensions=allowed_extensions,
                    max_size_bytes=max_size_bytes,
                ),
                options=MultimodalInspectionOptions(
                    bypass=False,
                    read_timeout_seconds=30,
                    unsupported_file_handling="block",
                    oversized_file_handling="block",
                    content_chunk_size=100,
                    detail_reason_directory=str(tmp_path),
                    watermark_criteria=_criteria(),
                ),
            )
        )
    )

    assert result.action == "block"
    assert result.selected_policy is not None
    assert result.selected_policy.name.startswith(expected_policy)
    assert ocr.call_count == expected_ocr_calls
    if expected_ocr_calls:
        assert result.selected_policy.id == "secret-rule"
        assert result.files[0].content_chunk == "contains secret"
        assert result.files[0].content_inspected
        ocr.call_args.args[0].close()


@pytest.mark.parametrize(
    ("allowed", "blocked"),
    [
        (("jpg",), False),
        (("jpeg",), False),
        (("JPG",), False),
        (("png",), True),
        ((), False),
    ],
)
def test_jpeg_image_allowlist_accepts_jpg_or_jpeg(
    tmp_path: Path,
    monkeypatch,
    allowed: tuple[str, ...],
    blocked: bool,
) -> None:
    path = tmp_path / "upload.jpg"
    with Image.new("RGB", (8, 8), color="white") as image:
        image.save(path, format="JPEG")
    monkeypatch.setattr(
        LocalMultimodalInspectionAdapter,
        "_watermark_policy",
        lambda self, source, criteria: None,
    )
    monkeypatch.setattr(
        LocalMultimodalInspectionAdapter,
        "_inspect_contents",
        lambda self, source, file_name, metadata, request, rules: (
            MultimodalFileInspectionResult(
                source=source,
                file_name=file_name,
                metadata=metadata,
            )
        ),
    )

    result = asyncio.run(
        LocalMultimodalInspectionAdapter().inspect(
            MultimodalInspectionRequest(
                targets=(
                    MultimodalInspectionTarget(
                        DocumentSource.from_path(path),
                        "계좌번호 - 신한은행.jpg",
                    ),
                ),
                policy=MultimodalPolicySnapshot(allowed_extensions=allowed),
                options=MultimodalInspectionOptions(
                    bypass=False,
                    read_timeout_seconds=30,
                    unsupported_file_handling="block",
                    oversized_file_handling="block",
                    content_chunk_size=0,
                    detail_reason_directory=str(tmp_path),
                    watermark_criteria=WatermarkCriteria(
                        drm_enabled=False,
                        drm_pattern="",
                        drm_pattern_flags=0,
                        drm_header_limit=0,
                        ocr_enabled=False,
                        ocr_patterns=(),
                        ocr_pattern_flags=0,
                        ocr_hit_threshold=0,
                        ocr_pages=(),
                    ),
                ),
            )
        )
    )

    assert result.files[0].metadata.extension == "jpeg"
    policy_name = "" if result.selected_policy is None else result.selected_policy.name
    if blocked:
        assert policy_name.startswith("파일 확장자 제한 (jpeg)")
    else:
        assert not policy_name.startswith("파일 확장자 제한")
