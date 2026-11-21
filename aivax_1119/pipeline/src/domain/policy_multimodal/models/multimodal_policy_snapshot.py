"""멀티모달 검사에 배포할 정책 snapshot."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MultimodalPolicyRule:
    id: str
    name: str
    expression: str
    action: str
    scope: str = "default"
    target: str = ""
    category: str = ""
    flags: int = 0
    subject_id: str = ""


@dataclass(frozen=True, slots=True)
class MultimodalPolicySnapshot:
    """파일 내용 규칙과 파일 허용 범위를 한 번에 교체하는 실행 snapshot.

    ``rules.action``에는 원본 저장 형식이 아니라 멀티모달 검사기가 실행할 수 있는
    유효 action이 들어간다. 따라서 원본 masking 정책은 provider가 block으로
    변환한 뒤 snapshot에 넣는다.
    """

    rules: tuple[MultimodalPolicyRule, ...] = ()
    group_members: tuple[tuple[str, tuple[str, ...]], ...] = ()
    allowed_extensions: tuple[str, ...] = ()
    max_size_bytes: int = 0


@dataclass(frozen=True, slots=True)
class MultimodalPolicyRefreshResult:
    active_rule_count: int
