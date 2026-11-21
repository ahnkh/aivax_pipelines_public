"""파일 전달 구현에 대한 도메인 계약."""

from abc import ABC, abstractmethod

from src.domain.delivery_file.models import FileDeliveryRequest, FileDeliveryResult


class FileDeliveryPort(ABC):
    @abstractmethod
    async def deliver(self, request: FileDeliveryRequest) -> FileDeliveryResult:
        """파일을 목적지로 전달하고 결과를 반환한다."""
        raise NotImplementedError
