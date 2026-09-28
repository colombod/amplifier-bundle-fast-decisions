"""Total provider-reported spend of every Amplifier session this eval ever ran (scored, discarded,
killed, smoke), from the native session files of projects whose path contains the eval root.

    python3 evals/continuous/total_spend.py [/tmp/ampup/continuous-eval]
"""
import json
import sys
from pathlib import Path

root = (sys.argv[1] if len(sys.argv) > 1 else "/tmp/ampup/continuous-eval").replace("/", "-")
total, sessions = 0.0, 0
for proj in (Path.home() / ".amplifier/projects").iterdir():
    if root.strip("-") not in proj.name:
        continue
    for ev in proj.glob("sessions*/*/events.jsonl"):
        sessions += 1
        for line in ev.read_text(errors="replace").splitlines():
            if '"llm:response"' not in line:
                continue
            try:
                total += float(((json.loads(line).get("data") or {}).get("usage") or {}).get("cost_usd") or 0)
            except (ValueError, TypeError):
                pass
print(f"{total:.3f} usd over {sessions} sessions")
