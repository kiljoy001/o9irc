#!/usr/bin/env python3
"""Static task-lifetime guard for o9irc.

`Task.await()` keeps the task handle (and its reply channel) alive.  Unless a
matching o9_task_close() follows, long-running code leaks one task per call.
`Task.take()` awaits and releases in one step.  This check keeps the hot paths
honest without needing a native UI boot.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Files that must not leave a task handle open.
TAKE_ONLY = ("irc.o9", "ircfs.o9", "protocol.o9", "history.o9", "uiio.o9")
# GUI awaits are allowed only when the same task is closed shortly after.
GUI_WINDOW = 24


def main():
    bad = []
    for name in TAKE_ONLY:
        path = ROOT / name
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if ".await()" in line:
                bad.append(f"{name}:{lineno}: use .take(): {line.strip()}")

    gui = (ROOT / "gui.o9").read_text().splitlines()
    for i, line in enumerate(gui):
        if ".await()" not in line:
            continue
        m = re.search(r"(\w+)\.await\(\)", line)
        if not m:
            bad.append(f"gui.o9:{i+1}: cannot identify awaited task: {line.strip()}")
            continue
        task = m.group(1)
        tail = "\n".join(gui[i:i + GUI_WINDOW])
        if f"o9_task_close({task})" not in tail:
            bad.append(
                f"gui.o9:{i+1}: {task}.await() is not followed by "
                f"o9_task_close({task}) within {GUI_WINDOW} lines"
            )

    if bad:
        print("task-lifetime check FAILED", file=sys.stderr)
        for item in bad:
            print("  " + item, file=sys.stderr)
        return 1
    print("task-lifetime check PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
