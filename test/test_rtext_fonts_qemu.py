#!/usr/bin/env python3
"""Verify o9 rich text loads and draws real 9front fonts in Rio."""

import importlib.util
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OBJECTIVE = Path(os.environ.get("OBJECTIVE_9C_REPO", ROOT / "objective-9c"))
HALATHA = Path(os.environ.get("HALATHA_REPO", ROOT / "halatha"))
if len(sys.argv) != 2:
    sys.exit("usage: test_rtext_fonts_qemu.py /path/to/graphical-9front-with-o9c.img")
IMAGE = Path(sys.argv[1])
ARCH = os.environ.get("O9IRC_VM_ARCH", "386")
if ARCH not in ("386", "amd64"):
    sys.exit("O9IRC_VM_ARCH must be 386 or amd64")


def run(*args):
    subprocess.run(args, check=True)


def stop(proc):
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def main():
    spec = importlib.util.spec_from_file_location(
        "builder", HALATHA / "extras/9front/kernel/build-kernel.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    with tempfile.TemporaryDirectory(prefix="o9-rtext-font-") as temp:
        work = Path(temp)
        disk = work / "test.img"
        run("cp", "--sparse=always", IMAGE, disk)
        fat = f"{disk}@@{builder.ninefat_offset(disk)}"
        subprocess.run(["mmd", "-i", fat, "::o9font"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for name in ("draw.o9", "rtext.o9", "string.o9",
                     "e2e_draw_rtext_font.o9"):
            run("mcopy", "-o", "-i", fat, OBJECTIVE / "stdlib" / name,
                f"::o9font/{name}")
        result_seed = work / "result-seed"
        result_seed.write_text("start\n")
        run("mcopy", "-o", "-i", fat, result_seed, "::o9font-result")
        riostart = work / "riostart"
        riostart.write_text(f"""#!/bin/rc
9fs 9fat
echo ready >> /n/9fat/o9font-result
window -m -r 40 40 520 200 /usr/glenda/bin/{ARCH}/o9-rtext-font > /n/9fat/o9font-result >[2=1] &
window -miny 130
""")
        run("mcopy", "-o", "-i", fat, riostart, "::o9font/riostart")
        original = subprocess.check_output(["mtype", "-i", fat, "::plan9.ini"])
        ini = work / "plan9.ini"
        ini.write_bytes(original + b"console=0 b115200\n")
        run("mcopy", "-o", "-i", fat, ini, "::plan9.ini")
        qemu = [f"qemu-system-{'x86_64' if ARCH == 'amd64' else 'i386'}",
                "-m", "1024", "-display", "none", "-vga", "std",
                "-no-reboot", "-net", "none", "-drive",
                f"file={disk},format=raw,if=ide,index=0"]
        if os.access("/dev/kvm", os.R_OK | os.W_OK):
            qemu.insert(1, "-enable-kvm")

        logpath = work / "serial.log"
        with logpath.open("w") as log:
            proc = subprocess.Popen(qemu + ["-monitor", "none", "-serial", "stdio"],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT)
            console = builder.Console(proc, log)
            try:
                assert console.expect(["init: starting /bin/rc"], 180)
                assert console.expect(["term% "], 60)

                def guest(command, timeout=60):
                    start = logpath.stat().st_size
                    for char in command + "\n":
                        proc.stdin.write(char.encode())
                        proc.stdin.flush()
                        time.sleep(0.006)
                    if console.expect(["term% "], timeout) is None:
                        raise RuntimeError(f"guest command timed out: {command}")
                    return logpath.read_bytes()[start:].decode(errors="replace")

                guest("9fs 9fat")
                guest("ramfs")
                for name in ("draw.o9", "rtext.o9", "string.o9",
                             "e2e_draw_rtext_font.o9"):
                    guest(f"cp /n/9fat/o9font/{name} /tmp/{name}")
                guest(f"bind -b /usr/glenda/bin/{ARCH} /bin")
                built = guest("o9build /tmp/e2e_draw_rtext_font.o9", 120)
                if "built /tmp/e2e_draw_rtext_font" not in built:
                    raise RuntimeError(f"font probe build failed:\n{built}")
                copied = guest(f"cp /tmp/e2e_draw_rtext_font /usr/glenda/bin/{ARCH}/o9-rtext-font")
                if "can't" in copied or "permission denied" in copied:
                    raise RuntimeError(f"font probe install failed:\n{copied}")
                copied = guest("cp /n/9fat/o9font/riostart /usr/glenda/bin/rc/riostart")
                if "can't" in copied or "permission denied" in copied:
                    raise RuntimeError(f"riostart copy failed:\n{copied}")
                installed = guest("cat /usr/glenda/bin/rc/riostart")
                if "o9-rtext-font" not in installed:
                    raise RuntimeError(f"riostart install failed:\n{installed}")
                guest("mount /srv/boot /adm adm")
                guest("echo sync > /adm/ctl")
                guest("sleep 5")
            except Exception:
                print(logpath.read_text(errors="replace")[-3000:], file=sys.stderr)
                raise
            finally:
                stop(proc)

        ini.write_bytes(b"\n".join(line for line in original.splitlines()
                                   if not line.startswith(b"console=")) +
                        b"\nmonitor=vesa\nvgasize=1024x768x16\n")
        run("mcopy", "-o", "-i", fat, ini, "::plan9.ini")
        monitor = work / "monitor.sock"
        proc = subprocess.Popen(qemu + ["-vnc", "127.0.0.1:49", "-serial", "none",
                                        "-monitor", f"unix:{monitor},server=on,wait=off"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 45
            output = ""
            while time.monotonic() < deadline:
                output = subprocess.run(["mtype", "-i", fat, "::o9font-result"],
                                        capture_output=True, text=True, check=True).stdout
                match = re.search(r"font probe 5 (\d+) (\d+) (\d+) (\d+) ([01]) ([01]) ([01])", output)
                if match:
                    plain, styled, cells, drawn, bold, italic, typewriter = map(int, match.groups())
                    if not (styled > plain and cells == 3 and drawn == 1 and
                            bold == italic == typewriter == 1):
                        raise RuntimeError(f"font probe wrong metrics: {output!r}")
                    image = work / "font-pass.ppm"
                    with socket.socket(socket.AF_UNIX) as sock:
                        sock.connect(str(monitor))
                        sock.recv(4096)
                        sock.sendall(f"screendump {image}\n".encode())
                        time.sleep(0.5)
                    magic, size, maxval, pixels = image.read_bytes().split(b"\n", 3)
                    width, height = map(int, size.split())
                    if magic != b"P6" or maxval != b"255" or width < 500 or height < 160:
                        raise RuntimeError("font probe screenshot has unexpected dimensions")
                    dark = 0
                    for y in range(50, 92):
                        for x in range(50, 450):
                            offset = (y * width + x) * 3
                            if pixels[offset:offset + 3] < b"\x80\x80\x80":
                                dark += 1
                    if dark < 20:
                        shutil.copyfile(image, "/tmp/o9-rtext-font-fail.ppm")
                        raise RuntimeError(f"font probe drew too few text pixels: {dark}")
                    print(f"O9 RTEXT FONT PASS: default {plain}px, styled {styled}px, 3 fonts loaded and drawn")
                    return
                if "font probe -1" in output:
                    raise RuntimeError(output)
                time.sleep(1)
            image = work / "font-fail.ppm"
            with socket.socket(socket.AF_UNIX) as sock:
                sock.connect(str(monitor))
                sock.recv(4096)
                sock.sendall(f"screendump {image}\n".encode())
                time.sleep(1)
            if image.exists():
                shutil.copyfile(image, "/tmp/o9-rtext-font-fail.ppm")
            raise RuntimeError(f"font probe timed out: {output!r}; screen /tmp/o9-rtext-font-fail.ppm")
        finally:
            stop(proc)


if __name__ == "__main__":
    main()
