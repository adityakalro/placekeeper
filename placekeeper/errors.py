"""Safe errors shared by service adapters and sync jobs."""

class ReadFailure(Exception):
    """An error safe to show without request headers, tokens or response bodies."""


class MissingProgress(ReadFailure):
    """A service confirmed the book has no saved progress marker."""


class UnsupportedMapping(ReadFailure):
    """A verified service position cannot be mapped for this edition."""


class NeedsRecovery(ReadFailure):
    """An uncertain earlier write requires explicit recovery, not an automatic retry."""
