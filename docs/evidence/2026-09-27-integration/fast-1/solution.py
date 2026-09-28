import re

_PATTERN = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def parse_duration(s):
    if not isinstance(s, str):
        raise TypeError("parse_duration expects a string")
    if s == "":
        raise ValueError("empty string is not a valid duration")

    match = _PATTERN.match(s)
    if not match or not any(match.groups()):
        raise ValueError(f"invalid duration string: {s!r}")

    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds
