"""Theater-Projektor: wireless stage video projector (Raspberry Pi + GStreamer)."""

__version__ = "0.1.0"


class ProjektorError(Exception):
    """Bad input to a command (unknown cue/video, invalid arguments, invalid show...).

    Raised by Controller methods and ShowStore; reported to clients as /proj/error.
    """
