"""Separate a definite controller refusal from an uncertain transport outcome."""

from . import feedback as commands

UNREACHABLE = 'can’t reach my memory rn, so i’m sitting this one out. try again in a few.'
UNKNOWN_MESSAGE = 10008  # Discord's error code for a message that doesn't exist.


def message_gone(exc):
    """Whether Discord's 404 says the message itself is gone (Unknown Message). Any other 404, Unknown Channel say,
    doesn't show that, so it's an unconfirmed read: left out, never withdrawn (fri #358, Sol round 1 of #362)."""
    return getattr(exc, 'code', None) == UNKNOWN_MESSAGE


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


class TurnUnreachable(ControllerUnavailable):
    """The controller couldn't say whether a turn's fence still holds, asked by a step of the turn (before a tool's
    model request of its own): the turn's outage, which the tool runner leaves to it (see fri_runner)."""


class AuthorErased(Exception):
    """The author erased after their message came in, so nothing more answers it."""


class GateClosed(Exception):
    """A gate the message passed coming in closed before the model saw it, e.g. its DM author left.
    The reason is a gate name for the log, never content."""


class Continued(Exception):
    """A short line from the author joined the turn before it committed: the burst it reopened answers instead
    (see fri_conversation.Continuation). Nothing went out, and nothing is missed."""
