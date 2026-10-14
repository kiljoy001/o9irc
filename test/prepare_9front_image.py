#!/usr/bin/env python3
"""Build a throwaway 9front VM image with the current local o9c toolchain."""

import importlib.util
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OBJECTIVE = Path(os.environ.get("OBJECTIVE_9C_REPO", ROOT.parent / "objective-9c"))
HALATHA = Path(os.environ.get("HALATHA_REPO", ROOT.parent / "halatha"))
BUILDER = HALATHA / "extras/9front/kernel/build-kernel.py"


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: prepare_9front_image.py base.img output.img")
    base, output = map(lambda p: Path(p).resolve(), sys.argv[1:])
    arch = os.environ.get("O9IRC_VM_ARCH", "386")
    if arch not in ("386", "amd64"):
        sys.exit("O9IRC_VM_ARCH must be 386 or amd64")
    if output.exists():
        sys.exit(f"refusing to overwrite {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("builder", BUILDER)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    files = [
        "mkfile", "o9.h", "o9_runtime.c", "o9_filetree.c", "o9_view.c",
        "o9_tab_discard.c", "o9_crypto.c", "monocypher.c", "monocypher.h",
        f"o9_dispatch_{arch}.s", "o9build", "o9proj", "o9plumb", "o9plumbbuild",
        "plumb/o9",
        "o9c/o9_type.c", "o9c/o9_type.h",
    ]
    files += [str(p.relative_to(OBJECTIVE)) for pattern in
              ("o9c/grammar.d/*.y", "libtab/*.c", "libtab/*.h", "stdlib/*.o9")
              for p in sorted(OBJECTIVE.glob(pattern))]
    with tempfile.TemporaryDirectory(prefix="o9irc-toolchain-") as temp:
        tarpath = Path(temp) / "o9src.tar"
        with tarfile.open(tarpath, "w", format=tarfile.USTAR_FORMAT) as archive:
            for dirname in ("o9src", "o9src/o9c", "o9src/o9c/grammar.d",
                            "o9src/libtab", "o9src/stdlib", "o9src/plumb"):
                info = tarfile.TarInfo(dirname)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
            for name in files:
                archive.add(OBJECTIVE / name, arcname="o9src/" + name, recursive=False)
        subprocess.run(["cp", "--sparse=always", base, output], check=True)
        fat = f"{output}@@{builder.ninefat_offset(output)}"
        subprocess.run(["mcopy", "-i", fat, tarpath, "::o9src.tar"], check=True)
    qemu = [f"qemu-system-{'x86_64' if arch == 'amd64' else 'i386'}",
            "-m", "1024", "-display", "none",
            "-monitor", "none", "-serial", "stdio", "-no-reboot", "-net", "none",
            "-drive", f"file={output},format=raw,if=ide,index=0"]
    if os.access("/dev/kvm", os.R_OK | os.W_OK):
        qemu.insert(1, "-enable-kvm")
    logpath = output.with_suffix(".build.log")
    with logpath.open("w") as log:
        proc = subprocess.Popen(qemu, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        console = builder.Console(proc, log)
        try:
            if console.expect(["init: starting /bin/rc"], 180) is None:
                raise RuntimeError("9front VM did not boot")
            if console.expect(["term% "], 60) is None:
                raise RuntimeError("9front serial prompt unavailable")

            def guest(command, timeout=60):
                for char in command + "\n":
                    proc.stdin.write(char.encode())
                    proc.stdin.flush()
                    time.sleep(0.006)
                if console.expect(["term% "], timeout) is None:
                    raise RuntimeError(f"guest command timed out: {command}")

            guest("9fs 9fat", 30)
            guest("ramfs", 30)
            guest("cd /tmp; tar xf /n/9fat/o9src.tar", 90)
            guest("cd /tmp/o9src; mk install >/tmp/o9-install.log >[2=1]; echo O9INSTALL:$status", 900)
            guest("tail -25 /tmp/o9-install.log", 30)
            log.flush()
            output_text = logpath.read_text(errors="replace")
            if "O9INSTALL:\r\n" not in output_text and "O9INSTALL:\n" not in output_text:
                raise RuntimeError("o9c install failed; see " + str(logpath))
            for char in "fshalt\n":
                proc.stdin.write(char.encode())
                proc.stdin.flush()
                time.sleep(0.006)
            console.expect(["done halting", "safe to turn off"], 90)
        except Exception:
            log.flush()
            print(logpath.read_text(errors="replace")[-8000:], file=sys.stderr)
            output.unlink(missing_ok=True)
            raise
        finally:
            proc.kill()
            proc.wait()
    print(f"prepared {output}")


if __name__ == "__main__":
    main()
