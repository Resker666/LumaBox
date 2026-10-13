class LumaBoxError(Exception):
    """Only fixed, non-sensitive messages belong in this exception."""

    def __init__(self, code: str, message: str = "Operation could not be completed", *, diagnostics: dict | None = None):
        self.code = code
        self.message = message
        self.diagnostics = diagnostics or {}
        super().__init__(f"{code}: {message}")


def check_status(status: int) -> None:
    code = {401: "AUTH_REQUIRED", 403: "BLOCKED", 404: "NOT_FOUND", 429: "RATE_LIMITED"}.get(status)
    if code:
        raise LumaBoxError(code, "Remote access requires attention")
