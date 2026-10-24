"""Separate a definite controller refusal from an uncertain transport outcome."""

from . import feedback as commands

UNREACHABLE = 'can’t reach my memory rn, so i’m sitting this one out. try again in a few.'


class ControllerRejected(commands.UserFeedbackCheckFailure):
    """A controller 4xx response confirms this submission was rejected."""


class ControllerUnavailable(commands.UserFeedbackCheckFailure):
    """The controller didn't answer, or failed on its side (5xx); nothing confirms either way."""
