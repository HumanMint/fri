"""Separate a definite controller refusal from an uncertain transport outcome."""

from . import feedback as commands

UNREACHABLE = 'can’t reach my memory rn, so i’m sitting this one out. try again in a few.'


class ControllerRejected(commands.UserFeedbackCheckFailure):
    """A controller 4xx response confirms this submission was rejected."""

    def __init__(self, detail, status=None):
        super().__init__(detail)
        self.status = status


class ControllerUnavailable(commands.UserFeedbackCheckFailure):
    """The controller didn't answer, or failed on its side (5xx); nothing confirms either way."""

    def __init__(self, detail, answered=False):
        super().__init__(detail)
        self.answered = answered  # Something answered as the controller never would: a proxy's page, a wrong URL.
