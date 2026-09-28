import re

_PATTERN = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


def parse_duration(s):
    """Parse a duration like "1h30m15s" into total seconds.

    Components (hours, minutes, seconds) are optional but must appear in
    h -> m -> s order, and at least one must be present. Any other string
    raises ValueError; a non-string input raises TypeError.
    """
    if not isinstance(s, str):
        raise TypeError("duration must be a str")
    if not s:
        raise ValueError("empty duration string")

    # \d in str patterns matches Unicode digits; restrict to ASCII 0-9.
    if not s.isascii():
        raise ValueError(f"invalid duration: {s!r}")

    match = _PATTERN.fullmatch(s)
    if match is None:
        raise ValueError(f"invalid duration: {s!r}")

    hours, minutes, seconds = match.groups()
    if hours is None and minutes is None and seconds is None:
        raise ValueError(f"invalid duration: {s!r}")

    total = 0
    if hours is not None:
        total += int(hours) * 3600
    if minutes is not None:
        total += int(minutes) * 60
    if seconds is not None:
        total += int(seconds)
    return total
