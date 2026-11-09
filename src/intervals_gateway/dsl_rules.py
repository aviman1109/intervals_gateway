"""Workout rules enforced before anything is written to intervals.icu.

Workouts sync to Zwift (.zwo) and Garmin. These rules come from real sync
failures recorded in athlete-os `integrations/intervals.md` (DSL rules):
- Zwift cannot render CJK titles, and full-width punctuation breaks .zwo sync.
- `N%` power must be %FTP so Zwift scales by its own FTP; absolute watts are
  converted through intervals' FTP and drift when the two FTPs differ.
- `% HR` means percent of MAX heart rate, a common mix-up with `% LTHR`.
- The `3x` repeat block is not parsed; repeats must be written out.
"""
from __future__ import annotations

import re

_PCT_MAX_HR = re.compile(r"%\s*HR\b", re.IGNORECASE)
_WATTS = re.compile(r"\b\d+(?:-\d+)?\s*w\b", re.IGNORECASE)
_REPEAT = re.compile(r"^\s*-?\s*\d+\s*x\s*$", re.IGNORECASE)


def _non_ascii(text: str) -> list[str]:
    return sorted({ch for ch in text if ord(ch) > 127})


def workout_errors(name: str | None, description: str, sport: str) -> list[str]:
    """Return human-readable rule violations; empty list means OK."""
    errors: list[str] = []
    if name is not None and (bad := _non_ascii(name)):
        errors.append(f"name must be ASCII (Zwift cannot show {''.join(bad)}); write it in English")
    if bad := _non_ascii(description):
        errors.append(
            f"description must be ASCII (found {''.join(bad[:10])}); "
            "Chinese notes go in a separate create_note_event, not the workout"
        )
    for line in description.splitlines():
        if _REPEAT.match(line):
            errors.append(f"repeat syntax '{line.strip()}' is not parsed; write each step out")
        if _PCT_MAX_HR.search(line):
            errors.append(f"'{line.strip()}': '% HR' is percent of MAX HR; use '% LTHR' or 'bpm'")
        if sport == "Ride" and line.lstrip().startswith("-") and _WATTS.search(line):
            errors.append(f"'{line.strip()}': use %FTP for ride power, not watts")
    return errors
