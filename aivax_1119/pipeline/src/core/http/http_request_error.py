"""공통 HTTP client가 외부 호출 실패를 표현하는 오류."""


class HttpRequestError(RuntimeError):
    def __init__(
        self,
        kind: str,
        method: str,
        url: str,
        *,
        status_code: int | None = None,
        response_body: str | None = None,
    ) -> None:
        self.kind = kind
        self.method = method
        self.url = url
        self.status_code = status_code
        self.response_body = response_body
        suffix = f" status={status_code}" if status_code is not None else ""
        super().__init__(f"HTTP {kind}: {method} {url}{suffix}")
