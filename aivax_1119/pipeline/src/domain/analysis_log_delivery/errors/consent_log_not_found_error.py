"""동의 처리에 필요한 원래 차단 로그의 전달 정보가 없을 때 발생한다."""


class ConsentLogNotFoundError(RuntimeError):
    pass
