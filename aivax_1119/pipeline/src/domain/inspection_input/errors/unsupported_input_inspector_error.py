"""등록되지 않은 입력 inspector 요청 오류."""


class UnsupportedInputInspectorError(ValueError):
    def __init__(self, inspector_type: object) -> None:
        inspector_name = getattr(inspector_type, "value", inspector_type)
        super().__init__(f"unsupported input inspector: {inspector_name}")
        self.inspector_type = inspector_type
