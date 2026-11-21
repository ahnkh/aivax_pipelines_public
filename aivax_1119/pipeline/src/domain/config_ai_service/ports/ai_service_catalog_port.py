"""Management가 배포한 AI 서비스 설정 조회·갱신 계약."""

from typing import Protocol


class AiServiceCatalogPort(Protocol):
    def name_for(self, service_id: int | None) -> str: ...

    def refresh(self) -> int: ...
