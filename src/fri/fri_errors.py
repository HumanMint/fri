"""Separate a definite controller refusal from an uncertain transport outcome."""

from . import feedback as commands


class ControllerRejected(commands.UserFeedbackCheckFailure):
    """A controller 4xx response confirms this submission was rejected."""


class ControllerUnavailable(commands.UserFeedbackCheckFailure):
    """The controller couldn't be reached, so nothing is known about the request's outcome."""
