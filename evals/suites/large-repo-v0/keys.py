"""Load ~/.amplifier/keys.env (KEY=VALUE / export KEY=VALUE lines) into
os.environ without printing anything -- the same effect as
`set -a; . ~/.amplifier/keys.env; set +a` for simple files."""
from __future__ import annotations

import os
import shlex
from pathlib import Path


def load_keys(path: str = "~/.amplifier/keys.env") -> int:
    p = Path(path).expanduser()
    if not p.exists():
        return 0
    n = 0
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, sep, value = line.partition("=")
        if not sep or not key.strip().isidentifier():
            continue
        try:
            parts = shlex.split(value)
            value = parts[0] if parts else ""
        except ValueError:
            value = value.strip().strip("'\"")
        os.environ.setdefault(key.strip(), value)
        n += 1
    return n
