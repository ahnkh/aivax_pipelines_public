"""테스트·부하 측정용 문서를 만들고 Fasoo 라이브러리로 CSO 등급을 부여한다."""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
from functools import cache
from pathlib import Path
from tempfile import TemporaryDirectory

import fitz
from docx import Document

ROOT = Path(__file__).resolve().parents[1]
JAVA_SOURCE = ROOT / "scripts/java/CsoFixtureTagger.java"
ENCRYPTOR_SOURCE = ROOT / "scripts/java/CsoFixtureEncryptor.java"
VENDOR_LIBS = ROOT / "third_party/fasoo/cso/lib/*"
FIXTURE_PASSWORD = "CsoFixtureOnly_2026!"


def _run(command: list[str], *, cwd: Path) -> None:
    result = subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, timeout=30, check=False
    )
    if result.returncode:
        raise RuntimeError(
            f"CSO 테스트 파일 생성 실패 (exit={result.returncode})\n"
            f"{result.stdout}\n{result.stderr}"
        )


@cache
def _tagger_directory() -> Path:
    """자체 테스트 helper만 프로세스당 한 번 컴파일한다."""
    for executable in ("java", "javac"):
        if not shutil.which(executable):
            raise RuntimeError(
                "CSO 테스트 파일 태깅에는 JDK 17 이상(java/javac)이 필요합니다."
            )
    temporary = TemporaryDirectory(prefix="aivax-cso-fixture-")
    atexit.register(temporary.cleanup)
    directory = Path(temporary.name)
    _run(
        [
            "javac",
            "--release",
            "17",
            "-encoding",
            "UTF-8",
            "-sourcepath",
            str(JAVA_SOURCE.parent),
            "-cp",
            str(VENDOR_LIBS),
            "-d",
            str(directory),
            str(JAVA_SOURCE),
            str(ENCRYPTOR_SOURCE),
        ],
        cwd=directory,
    )
    return directory


def tag_cso_document(path: Path, grade: int | str) -> None:
    """Fasoo 태깅 API로 등급을 기록하고 같은 라이브러리로 재조회한다."""
    directory = _tagger_directory()
    _run(
        [
            "java",
            "-Dfile.encoding=UTF-8",
            f"-Djava.io.tmpdir={directory}",
            "-cp",
            os.pathsep.join((str(directory), str(VENDOR_LIBS))),
            "CsoFixtureTagger",
            str(path.resolve()),
            str(grade),
        ],
        cwd=directory,
    )


def write_cso_docx(
    path: Path, grade: int | str | None = None, *, text: str = "CSO test document"
) -> Path:
    """일반 DOCX를 생성하고 grade가 있으면 라이브러리의 setCTProperty로 태깅한다.

    None은 미태깅 파일을 뜻한다. 빈 값·잘못된 등급도 라이브러리로 기록하여
    조회 경계값 테스트에 사용한다. 태깅 API 실패 시 직접 XML을 쓰는 대체 경로는 없다.
    """
    path = path.resolve()
    document = Document()
    document.add_paragraph(text)
    document.save(path)
    if grade is not None:
        tag_cso_document(path, grade)
    return path


def write_password_protected_docx(path: Path, *, mode: str = "standard") -> Path:
    """Fasoo로 O 태깅한 DOCX를 실제 Office 열기 암호로 보호하고 복호화 검증한다."""
    path = path.resolve()
    directory = _tagger_directory()
    with TemporaryDirectory(prefix="aivax-cso-plaintext-") as temporary:
        source = write_cso_docx(Path(temporary) / "source.docx", 3)
        _run(
            [
                "java",
                "-Dfile.encoding=UTF-8",
                f"-Djava.io.tmpdir={directory}",
                "-cp",
                os.pathsep.join((str(directory), str(VENDOR_LIBS))),
                "CsoFixtureEncryptor",
                str(source),
                str(path),
                FIXTURE_PASSWORD,
                mode,
            ],
            cwd=directory,
        )
    return path


def write_password_protected_pdf(path: Path) -> Path:
    """Fasoo로 O 태깅한 PDF를 암호화하고 비밀번호가 필요한지 확인한다."""
    path = path.resolve()
    text = "Password protected CSO integration document"
    with TemporaryDirectory(prefix="aivax-cso-plaintext-") as temporary:
        source = Path(temporary) / "source.pdf"
        with fitz.open() as document:
            document.new_page().insert_text((72, 72), text)
            document.save(source)
        tag_cso_document(source, 3)
        with fitz.open(source) as document:
            document.save(
                path,
                encryption=fitz.PDF_ENCRYPT_AES_256,
                owner_pw=FIXTURE_PASSWORD,
                user_pw=FIXTURE_PASSWORD,
                permissions=0,
            )
    with fitz.open(path) as document:
        if not document.needs_pass or document.authenticate(
            FIXTURE_PASSWORD + "-wrong"
        ):
            raise RuntimeError("암호화 PDF에 올바른 비밀번호가 필요하지 않습니다.")
        if not document.authenticate(FIXTURE_PASSWORD):
            raise RuntimeError("올바른 비밀번호로 PDF를 열 수 없습니다.")
        if document[0].get_text().strip() != text:
            raise RuntimeError("암호화 전후 PDF 본문이 다릅니다.")
    return path
