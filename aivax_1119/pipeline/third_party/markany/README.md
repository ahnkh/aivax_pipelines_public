# MarkAny N2SF 판독 참조 구현

`n2sf_read.py`는 MarkAny가 2026-11-03 제공한 Python 참조 구현이다.
`DSP_N2SFTAG` 26.10.x 판독 규칙을 기준으로 한다.
원본은 작업 자료 `00_docs/06_markany_n2sf/n2sf_read.py`이며 수정 없이 포함했다.

원본 SHA-256은 `7825179963c1cddccd5ea2ad1a34e07da2a9fd069e9d6c29032dbdd08d1ff3e0`이다.
배포 후에는 이 모듈을 사용하므로 `00_docs`에 접근하지 않는다.

Pipeline의 [`markany_grade_adapter.py`](../../src/extensions/cso/markany_grade_adapter.py)는
`read_tags()`만 호출한다. 파일을 수정하거나 태그를 기록하지 않으며,
`N2SF_Sig`를 검증하지 않는다. 판독 상태와 스킵 정책의 변환은 상위 어댑터와 가드가 담당한다.
