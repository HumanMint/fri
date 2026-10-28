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


class AuthorErased(Exception):
    """The author erased after their message came in, so nothing more answers it."""


class GateClosed(Exception):
    """A gate the message passed coming in closed before the model saw it, e.g. its DM author left.
    The reason is a gate name for the log, never content."""


class Continued(Exception):
    """A short line from the author joined the turn before it committed: the burst it reopened answers instead
    (see fri_conversation.Continuation). Nothing went out, and nothing is missed."""
