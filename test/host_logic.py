#!/usr/bin/env python3
"""Run o9irc's host-side logic checks without booting 9front.

The test transpiles the real o9irc GUI/protocol/history code with o9c
--no-facade, compiles the generated C against objective-9c's host runtime,
and checks the pure helpers plus the transcript layout/hit-test path.

The host runtime artifacts are produced by objective-9c's host_test.sh:

    O9_HOST_BUILD=/tmp/o9host sh ../objective-9c/tools/host_test.sh

Set OBJECTIVE_9C_REPO, O9_HOST_BUILD, or O9C to override the defaults.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OBJECTIVE = Path(os.environ.get("OBJECTIVE_9C_REPO", ROOT.parent / "objective-9c"))
HOST_BUILD = Path(os.environ.get("O9_HOST_BUILD", "/tmp/o9host"))
O9C = Path(os.environ.get("O9C", OBJECTIVE / "o9c/o9c"))
PLAN9 = Path(os.environ.get("PLAN9", "/usr/local/plan9"))

HOST_FILES = ("libo9host.a", "host_ui_main.o", "o9_view.o")


def need(path, hint):
    if not path.exists():
        sys.exit(f"host logic: missing {path}\n{hint}")


def run(args, cwd, stdout=None, stderr=subprocess.PIPE):
    return subprocess.run(args, cwd=cwd, stdout=stdout,
                          stderr=stderr, text=stdout is not None)


def main():
    need(O9C, "set O9C to a built o9c binary")
    for name in HOST_FILES:
        need(HOST_BUILD / name,
             "build objective-9c's host runtime first:\n"
             "  O9_HOST_BUILD=/tmp/o9host sh "
             f"{OBJECTIVE}/tools/host_test.sh")
    for tool in ("9c", "9l"):
        if shutil.which(tool) is None:
            sys.exit(f"host logic: {tool} is not on PATH (install plan9port)")

    # The compiler resolves imports relative to the entry file.  Stage the
    # app and stdlib sources together, as o9build's binds do on 9front.
    with tempfile.TemporaryDirectory(prefix="o9irc-host-logic-") as tmp:
        work = Path(tmp)
        for src in ROOT.glob("*.o9"):
            shutil.copy(src, work / src.name)
        for src in (OBJECTIVE / "stdlib").glob("*.o9"):
            shutil.copy(src, work / src.name)
        shutil.copy(ROOT / "test/host_logic.o9", work / "host_logic.o9")

        gen = work / "host_logic.c"
        with gen.open("w") as out:
            cp = run([str(O9C), "--no-facade", "host_logic.o9"],
                     work, stdout=out, stderr=subprocess.PIPE)
        if cp.returncode != 0:
            sys.exit("host logic: transpile failed\n" + cp.stderr)

        cflags = ["9c", "-include",
                  str(OBJECTIVE / "o9c/test/host_compat.h"),
                  "-DO9_PLAN9PORT", f"-I{OBJECTIVE}",
                  f"-I{OBJECTIVE / 'libtab'}", "-o", "host_logic.o",
                  "host_logic.c"]
        cp = run(cflags, work)
        if cp.returncode != 0:
            sys.exit("host logic: compile failed\n" + cp.stderr.decode(errors="replace"))

        lflags = ["9l", "-o", "host_logic.bin",
                  str(HOST_BUILD / "host_ui_main.o"), "host_logic.o",
                  str(HOST_BUILD / "o9_view.o"),
                  str(HOST_BUILD / "libo9host.a"),
                  f"-L{PLAN9 / 'lib'}", "-l9", "-lthread", "-lbio",
                  "-ldraw", "-lmemdraw", "-lflate", "-lm"]
        cp = run(lflags, work)
        if cp.returncode != 0:
            sys.exit("host logic: link failed\n" + cp.stderr.decode(errors="replace"))

        cp = run([str(work / "host_logic.bin")], work,
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if cp.returncode != 0:
            sys.exit("host logic: run failed\n" + cp.stderr)

        expected = (ROOT / "test/host_logic.expected").read_text()
        if cp.stdout != expected:
            print("host logic: output mismatch", file=sys.stderr)
            print("--- expected ---", file=sys.stderr)
            print(expected, file=sys.stderr)
            print("--- actual ---", file=sys.stderr)
            print(cp.stdout, file=sys.stderr)
            return 1

    cp = subprocess.run([sys.executable, str(ROOT / "test/check_task_lifetime.py")])
    if cp.returncode != 0:
        return cp.returncode
    print("o9irc HOST LOGIC PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
