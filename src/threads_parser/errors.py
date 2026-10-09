class TargetProfileError(RuntimeError):
    def __init__(self, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class ProxyAccessError(RuntimeError):
    pass
