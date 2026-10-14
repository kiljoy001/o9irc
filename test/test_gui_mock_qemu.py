#!/usr/bin/env python3
"""Check the Rio client's 9P contract without an IRC connection."""

import importlib.util
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OBJECTIVE = Path(os.environ.get("OBJECTIVE_9C_REPO", ROOT.parent / "objective-9c"))
HALATHA = Path(os.environ.get("HALATHA_REPO", ROOT.parent / "halatha"))
BUILDER = HALATHA / "extras/9front/kernel/build-kernel.py"
if len(sys.argv) != 2:
    sys.exit("usage: test_gui_mock_qemu.py /path/to/9front-with-o9c.img")
IMAGE = Path(sys.argv[1])
ARCH = os.environ.get("O9IRC_VM_ARCH", "386")
if ARCH not in ("386", "amd64"):
    sys.exit("O9IRC_VM_ARCH must be 386 or amd64")


def main():
    spec = importlib.util.spec_from_file_location("builder", BUILDER)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    with tempfile.TemporaryDirectory(prefix="o9irc-ui-mock-") as temp:
        work = Path(temp)
        disk = work / "test.img"
        subprocess.run(["cp", "--sparse=always", IMAGE, disk], check=True)
        fat = f"{disk}@@{builder.ninefat_offset(disk)}"
        subprocess.run(["mmd", "-i", fat, "::o9irc"], check=True)
        sources = {
            "mock_view.o9": ROOT / "test/mock_view.o9",
            "gui.o9": ROOT / "gui.o9",
            "uiio.o9": ROOT / "uiio.o9",
            "draw.o9": OBJECTIVE / "stdlib/draw.o9",
            "rtext.o9": OBJECTIVE / "stdlib/rtext.o9",
            "ui_input_probe.o9": ROOT / "test/ui_input_probe.o9",
            "draw_pack_probe.o9": OBJECTIVE / "stdlib/e2e_draw_pack.o9",
            "draw_panel_probe.o9": OBJECTIVE / "stdlib/e2e_draw_panel.o9",
            "draw_frame_probe.o9": OBJECTIVE / "stdlib/e2e_draw_frame.o9",
            "draw_rtext_probe.o9": OBJECTIVE / "stdlib/e2e_draw_rtext.o9",
        }
        for name, source in sources.items():
            subprocess.run(["mcopy", "-i", fat, source, f"::o9irc/{name}"], check=True)
        qemu = [f"qemu-system-{'x86_64' if ARCH == 'amd64' else 'i386'}",
                "-m", "1024", "-display", "none",
                "-monitor", "none", "-serial", "stdio", "-no-reboot",
                "-net", "none", "-drive", f"file={disk},format=raw,if=ide,index=0"]
        if os.access("/dev/kvm", os.R_OK | os.W_OK):
            qemu.insert(1, "-enable-kvm")
        logpath = work / "serial.log"
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
                    log.flush()
                    start = logpath.stat().st_size
                    for char in command + "\n":
                        proc.stdin.write(char.encode())
                        proc.stdin.flush()
                        time.sleep(0.006)
                    if console.expect(["term% "], timeout) is None:
                        raise RuntimeError(f"guest command timed out: {command}")
                    log.flush()
                    return logpath.read_bytes()[start:].decode(errors="replace")

                guest("9fs 9fat", 30)
                guest("ramfs", 30)
                for name in sources:
                    guest(f"cp /n/9fat/o9irc/{name} /tmp/{name}", 30)
                guest(f"bind -b /usr/glenda/bin/{ARCH} /bin", 30)
                guest("bind -b /usr/glenda/include /sys/include", 30)
                guest(f"bind -b /usr/glenda/lib/o9 /{ARCH}/lib", 30)
                for name in ("mock_view", "gui", "ui_input_probe", "draw_pack_probe",
                             "draw_panel_probe", "draw_frame_probe",
                             "draw_rtext_probe"):
                    output = guest(f"o9build /tmp/{name}.o9", 120)
                    if f"built /tmp/{name}" not in output:
                        raise RuntimeError(f"{name} build failed:\n{output}")
                layout = guest("/tmp/draw_pack_probe", 30)
                for line in OBJECTIVE.joinpath("stdlib/e2e_draw_pack.expected").read_text().splitlines():
                    if line not in layout:
                        raise RuntimeError(f"packer mismatch, missing {line!r}:\n{layout}")
                panel = guest("/tmp/draw_panel_probe", 30)
                for line in OBJECTIVE.joinpath("stdlib/e2e_draw_panel.expected").read_text().splitlines():
                    if line not in panel:
                        raise RuntimeError(f"existing panel behavior changed, missing {line!r}:\n{panel}")
                frame = guest("/tmp/draw_frame_probe", 30)
                for line in OBJECTIVE.joinpath("stdlib/e2e_draw_frame.expected").read_text().splitlines():
                    if line not in frame:
                        raise RuntimeError(f"existing frame behavior changed, missing {line!r}:\n{frame}")
                rich = guest("/tmp/draw_rtext_probe", 30)
                for line in ("layout 54 cells 3",
                             "link https://example.com",
                             "click 1 https://example.com",
                             "selection hello world ",
                             "partial it byte 2",
                             "mixed 72 cells 6 widget 26 78 20 10",
                             "frame 1 commands 7",
                             "reset 18 cells 1 hit draw://new",
                             "styled fallback 18 cells 2"):
                    if line not in rich:
                        raise RuntimeError(f"rich text probe missing {line!r}:\n{rich}")
                guest("/tmp/mock_view mock </dev/null >/tmp/mock_view.log >[2=1] &", 30)
                guest("mkdir /mnt/o9irc", 30)
                deadline = time.monotonic() + 10
                status = ""
                while time.monotonic() < deadline:
                    guest("mount -c /srv/mock /mnt/o9irc", 30)
                    status = guest("cat /mnt/o9irc/view/networks/default/status", 30)
                    if "connected to mock" in status:
                        break
                    guest("unmount /mnt/o9irc", 30)
                    time.sleep(0.1)
                if "connected to mock" not in status:
                    raise RuntimeError("mock 9P view unavailable:\n" + status)
                root = "/mnt/o9irc/view/networks/default/"
                checks = {
                    "statuslog": ":mock 001 test :welcome",
                    "channel": "#fixture",
                    "topic": "Synthetic 9P view",
                    "nicks": "@alice",
                    "transcript": "<alice> fixture hello",
                }
                for name, expected in checks.items():
                    output = guest(f"cat {root}{name}", 30)
                    if expected not in output:
                        raise RuntimeError(f"{name} view missing {expected!r}:\n{output}")
                conversations = guest(f"cat {root}conversations", 30)
                for key, label, expected in (
                    ("channels/2366697874757265", "#fixture", "fixture hello"),
                    ("channels/236f74686572", "#other", "other hello"),
                    ("users/616c696365", "alice", "private hello"),
                ):
                    if f"{key}\t{label}" not in conversations:
                        raise RuntimeError(f"conversation missing from index: {conversations!r}")
                    output = guest(f"cat {root}{key}/transcript", 30)
                    if expected not in output:
                        raise RuntimeError(f"conversation {label} missing {expected!r}:\n{output}")
                write = guest(f"/tmp/ui_input_probe probe {root}input", 30)
                if "o9irc-ui-write: 14" not in write:
                    raise RuntimeError("GUI 9P input adapter failed:\n" + write)
                sent = guest(f"cat {root}sent", 30)
                if "say from view" not in sent:
                    raise RuntimeError("mock did not record GUI command:\n" + sent)
            except Exception:
                log.flush()
                print(logpath.read_text(errors="replace")[-3000:], file=sys.stderr)
                raise
            finally:
                proc.kill()
                proc.wait()
    print("O9IRC UI MOCK PASS: GUI compiled and 9P view/input work without IRC")


if __name__ == "__main__":
    main()
