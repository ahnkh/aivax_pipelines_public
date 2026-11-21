from src.domain.config_ai_service.ports import AiServiceCatalogPort


class RefreshAiServiceCatalogUseCase:
    def __init__(self, catalog: AiServiceCatalogPort) -> None:
        self._catalog = catalog

    def execute(self) -> int:
        return self._catalog.refresh()
