"""Fixed native stage adapters; no executable request-defined callbacks."""
from .contracts import IntakeError, identifier, require


class StageFailure(Exception):
    def __init__(self, category, code):
        require(category in {"retryable", "blocked", "permanent"}, "invalid_failure")
        identifier(code)
        self.category, self.code = category, code
        super().__init__(code)


def prepare(root, stage):
    raise StageFailure("blocked", "unsupported-stage")


def execute(root, stage, intent, previous=None):
    raise StageFailure("blocked", "unsupported-stage")


def availability(root, stage, evidence):
    return {"available": False, "reason": "not-publication"}
