"""Separate a definite controller refusal from an uncertain transport outcome."""

from . import feedback as commands


class ControllerRejected(commands.UserFeedbackCheckFailure):
    """A controller 4xx response confirms this submission was rejected."""

    def __init__(self, detail, status=None):
        super().__init__(detail)
        self.status = status
