"""Safe, structured public errors. Internal exception details stay private."""


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def invalid(message: str = "Please check your input.") -> AppError:
    return AppError(400, "invalid_input", message)


def conflict() -> AppError:
    return AppError(409, "revision_conflict", "The forecast has changed. Refresh the page and try again.")
