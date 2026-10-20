"""Typed errors so every entry point (CLI, HTTP, library) can tell "you asked for something invalid"
from "it doesn't exist". Both subclass ValueError, so older `except ValueError` callers keep working."""


class InvalidInput(ValueError):
    """The request itself is malformed (bad range, unknown stream, malformed MBID or track string)."""


class NotFound(ValueError):
    """The request is well-formed but the seed doesn't exist upstream."""
