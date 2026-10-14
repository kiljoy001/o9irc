#!/usr/bin/env python3
"""Test real Rio mouse events and 9P input in a disposable graphical 9front VM."""

import importlib.util
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OBJECTIVE = Path(os.environ.get("OBJECTIVE_9C_REPO", ROOT.parent / "objective-9c"))
HALATHA = Path(os.environ.get("HALATHA_REPO", ROOT.parent / "halatha"))
if len(sys.argv) != 2:
    sys.exit("usage: test_gui_mouse_qemu.py /path/to/graphical-9front-with-o9c.img")
IMAGE = Path(sys.argv[1])
ARCH = os.environ.get("O9IRC_VM_ARCH", "386")
if ARCH not in ("386", "amd64"):
    sys.exit("O9IRC_VM_ARCH must be 386 or amd64")


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def stop(proc):
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def pixel(path, x, y):
    magic, size, maxval, data = path.read_bytes().split(b"\n", 3)
    assert magic == b"P6" and maxval == b"255"
    width, height = map(int, size.split())
    assert 0 <= x < width and 0 <= y < height
    offset = (y * width + x) * 3
    return tuple(data[offset:offset + 3])


def chat_region(path):
    _, size, _, data = path.read_bytes().split(b"\n", 3)
    width, _ = map(int, size.split())
    return b"".join(data[(y * width + 200) * 3:(y * width + 700) * 3]
                    for y in range(180, 500))


def region(path, x0, y0, x1, y1):
    _, size, _, data = path.read_bytes().split(b"\n", 3)
    width, _ = map(int, size.split())
    return b"".join(data[(y * width + x0) * 3:(y * width + x1) * 3]
                    for y in range(y0, y1))


def hmp(sock, command):
    sock.sendall((command + "\n").encode())
    time.sleep(0.4)
    sock.recv(4096)


def shot(sock, work, name):
    path = work / f"{name}.ppm"
    hmp(sock, f"screendump {path}")
    return path


def main():
    setup_mode = os.environ.get("O9IRC_SETUP_ONLY") == "1"
    tls_setup = os.environ.get("O9IRC_SETUP_TLS") == "1"
    spec = importlib.util.spec_from_file_location(
        "builder", HALATHA / "extras/9front/kernel/build-kernel.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    with tempfile.TemporaryDirectory(prefix="o9irc-mouse-") as temp:
        work = Path(temp)
        disk = work / "test.img"
        run("cp", "--sparse=always", IMAGE, disk)
        fat = f"{disk}@@{builder.ninefat_offset(disk)}"
        subprocess.run(["mmd", "-i", fat, "::o9irc"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        mock = (ROOT / "test/mock_view.o9").read_text().replace(
            'files.text("sent", sent, false);',
            'files.text("sent", sent, false);\n        writefile("/n/9fat/o9irc-sent", sent);')
        if setup_mode:
            mock = mock.replace(
                'files.text("status", "connected to mock\\n", false);',
                'files.text("status", "disconnected\\n", false);\n'
                '        files.text("profile", "\\t6667\\t\\t\\t", false);')
        channel_name = "#fixture-very-long-channel-name-for-scrolling"
        topic_name = "Synthetic 9P view with a long channel topic that keeps going " * 3
        mock = mock.replace("#fixture\\n", channel_name + "\\n")
        mock = mock.replace("Synthetic 9P view", topic_name)
        chunks = ["".join(f"<alice> historical message {i:02d}"
                          + (" WRAPBEGIN " + "word " * 35 + "WRAPEND" if i == 59 else "")
                          + "\\n" for i in range(start, start + 10))
                  for start in range(0, 60, 10)]
        transcript = f'"{chunks[0]}"'
        for chunk in chunks[1:]:
            transcript = f'cat({transcript}, "{chunk}")'
        mock = mock.replace(
            'files.text("transcript", "<alice> fixture hello\\n", false);',
            f'files.text("transcript", {transcript}, false);')
        mock = mock.replace(
            'files.text("channels/2366697874757265/transcript", "<alice> fixture hello\\n", false);',
            f'files.text("channels/2366697874757265/transcript", {transcript}, false);')
        sources = {"mock_view.o9": mock, "gui.o9": (ROOT / "gui.o9").read_text(),
                   "uiio.o9": (ROOT / "uiio.o9").read_text(),
                   "draw.o9": (OBJECTIVE / "stdlib/draw.o9").read_text()}
        for name, content in sources.items():
            source = work / name
            source.write_text(content)
            run("mcopy", "-o", "-i", fat, source, f"::o9irc/{name}")
        memory_seed = work / "memory-seed"
        memory_seed.write_text("start\n")
        run("mcopy", "-o", "-i", fat, memory_seed, "::o9irc-memory")
        riostart = work / "riostart"
        riostart.write_text("""#!/bin/rc
window 0,0,161,117 stats -lmisce
9fs 9fat
echo ready >> /n/9fat/o9irc-memory
if(! test -d /mnt/o9irc) mkdir /mnt/o9irc
/usr/glenda/bin/ARCH/o9irc-mock mock </dev/null >/tmp/o9irc-mock.log >[2=1] &
sleep 2
mount -c /srv/mock /mnt/o9irc
echo o9irc-snarf-probe >/dev/snarf
{ for(i in 1 2 3 4 5 6 7 8 9 10 11 12){
    sleep 5
    echo sample >> /n/9fat/o9irc-memory
    ps | grep o9irc-gui >> /n/9fat/o9irc-memory
} } &
{ while(test -e /dev/snarf){
    sleep 1
    cat /dev/snarf >/n/9fat/o9irc-snarf >[2]/dev/null
} } &
window -m -r 30 30 990 730 /usr/glenda/bin/ARCH/o9irc-gui gui /mnt/o9irc >/n/9fat/o9irc-guilog >[2=1] &
window -miny 130
""")
        riostart.write_text(riostart.read_text().replace("/ARCH/", f"/{ARCH}/"))
        run("mcopy", "-o", "-i", fat, riostart, "::o9irc/riostart")
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
                for name in sources:
                    guest(f"cp /n/9fat/o9irc/{name} /tmp/{name}")
                for command in (f"bind -b /usr/glenda/bin/{ARCH} /bin",
                                "bind -b /usr/glenda/include /sys/include",
                                f"bind -b /usr/glenda/lib/o9 /{ARCH}/lib"):
                    guest(command)
                for name in ("mock_view", "gui"):
                    result = guest(f"o9build /tmp/{name}.o9", 120)
                    if f"built /tmp/{name}" not in result:
                        raise RuntimeError(f"{name} build failed:\n{result}")
                guest(f"cp /tmp/mock_view /usr/glenda/bin/{ARCH}/o9irc-mock")
                guest(f"cp /tmp/gui /usr/glenda/bin/{ARCH}/o9irc-gui")
                guest("cp /n/9fat/o9irc/riostart /usr/glenda/bin/rc/riostart")
                guest("chmod +x /usr/glenda/bin/rc/riostart")
                guest("mount /srv/boot /adm adm")
                guest("echo sync > /adm/ctl")
                guest("sleep 5")
            except Exception:
                print(logpath.read_text(errors="replace")[-3000:], file=sys.stderr)
                raise
            finally:
                stop(proc)

        # The clean installer image is configured for a serial console. Rio
        # needs the VGA console for this graphical pass.
        ini.write_bytes(b"\n".join(line for line in original.splitlines()
                                   if not line.startswith(b"console=")) +
                        b"\nmonitor=vesa\nvgasize=1024x768x16\n")
        run("mcopy", "-o", "-i", fat, ini, "::plan9.ini")
        monitor = work / "monitor.sock"
        proc = subprocess.Popen(
            qemu + ["-vnc", "127.0.0.1:49", "-monitor",
                    f"unix:{monitor},server=on,wait=off", "-serial", "none"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 60
            while not monitor.exists() and time.monotonic() < deadline:
                time.sleep(0.2)
            with socket.socket(socket.AF_UNIX) as sock:
                sock.connect(str(monitor))
                sock.recv(4096)
                while time.monotonic() < deadline:
                    boot = shot(sock, work, "boot")
                    # This spot stays blank inside the transcript pane. The
                    # first text row can cover the former probe at 250,160.
                    _, size, _, _ = boot.read_bytes().split(b"\n", 3)
                    screen_width, screen_height = map(int, size.split())
                    if screen_width <= 800 or screen_height <= 400:
                        time.sleep(2)
                        continue
                    ready_pixel = pixel(boot, 500, 155) if setup_mode else pixel(boot, 500, 50)
                    ready = ready_pixel[2] > ready_pixel[0] + 20 and max(ready_pixel) < 150
                    if ready:
                        break
                    time.sleep(2)
                else:
                    failure = Path("/tmp/o9irc-rio-boot-fail.ppm")
                    failure.write_bytes(boot.read_bytes())
                    logs = subprocess.run(["mtype", "-i", fat, "::o9irc-guilog"],
                                          capture_output=True, text=True).stdout
                    samples = subprocess.run(["mtype", "-i", fat, "::o9irc-memory"],
                                             capture_output=True, text=True).stdout
                    raise RuntimeError(f"Rio GUI did not draw; screen {failure}; GUI log {logs!r}; samples {samples[-500:]!r}")
                preview = os.environ.get("O9IRC_PREVIEW_PATH")
                if preview:
                    Path(preview).write_bytes(boot.read_bytes())
                # A long conversation name must stay inside the sidebar.
                gutter = region(boot, 206, 190, 212, 211)
                if any(max(gutter[i:i+3]) < 180 for i in range(0, len(gutter), 3)):
                    raise RuntimeError("conversation label escaped the sidebar")
                if pixel(boot, 981, 160) == pixel(boot, 982, 160) or \
                   pixel(boot, 982, 160) != pixel(boot, 985, 160):
                    raise RuntimeError("nick pane right border or window gutter is missing")
                if os.environ.get("O9IRC_MEMORY_ONLY"):
                    sample_deadline = time.monotonic() + 35
                    while time.monotonic() < sample_deadline:
                        data = subprocess.run(["mtype", "-i", fat, "::o9irc-memory"],
                                              capture_output=True, text=True, check=True).stdout
                        if "sample\n" in data:
                            break
                        time.sleep(1)
                    else:
                        failure = Path("/tmp/o9irc-memory-fail.ppm")
                        failure.write_bytes(shot(sock, work, "memory-fail").read_bytes())
                        raise RuntimeError(f"Rio memory sampler did not start: {data!r}; screen {failure}")
                    time.sleep(50)
                    data = subprocess.run(["mtype", "-i", fat, "::o9irc-memory"],
                                          capture_output=True, text=True, check=True).stdout
                    samples = []
                    for block in data.split("sample\n")[1:]:
                        sizes = [int(size) for size in re.findall(r"\b(\d+)K\b", block)]
                        if sizes:
                            samples.append(max(sizes))
                    if len(samples) < 5 or samples[-1] - samples[0] > 1024:
                        raise RuntimeError(f"GUI memory grew: {samples!r} KiB")
                    print(f"O9IRC GUI MEMORY PASS: {samples[0]} -> {samples[-1]} KiB")
                    return
                hmp(sock, "mouse_move 200 120")
                hmp(sock, "mouse_button 1")
                hmp(sock, "mouse_button 0")
                # The emulated PS/2 mouse moves two guest pixels per HMP unit.
                mouse = [400, 240]

                def move_to(x, y):
                    while abs(mouse[0] - x) > 1 or abs(mouse[1] - y) > 1:
                        dx = max(-60, min(60, (x - mouse[0]) // 2))
                        dy = max(-60, min(60, (y - mouse[1]) // 2))
                        hmp(sock, f"mouse_move {dx} {dy}")
                        mouse[0] += 2 * dx
                        mouse[1] += 2 * dy

                def click_at(x, y):
                    move_to(x, y)
                    hmp(sock, "mouse_button 1")
                    hmp(sock, "mouse_button 0")

                if setup_mode:
                    # Enter settings from the default blank profile, then
                    # send the exact command through the mock 9P input file.
                    time.sleep(2)
                    click_at(500, 219)
                    for letter in "localhost":
                        hmp(sock, f"sendkey {letter}")
                    click_at(500, 313)
                    for letter in "guest":
                        hmp(sock, f"sendkey {letter}")
                    if tls_setup:
                        click_at(500, 466)
                        click_at(500, 506)
                        for letter in "/tmp/trustedkeys":
                            hmp(sock, "sendkey slash" if letter == "/" else f"sendkey {letter}")
                    click_at(500, 592)
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        sent = subprocess.run(["mtype", "-i", fat, "::o9irc-sent"],
                                              capture_output=True, text=True).stdout
                        expected = ("connect\tlocalhost\t6697\tguest\t\t\ttls\t/tmp/trustedkeys\n"
                                    if tls_setup else "connect\tlocalhost\t6667\tguest\t\t\ttcp\t\n")
                        if expected in sent:
                            print("O9IRC GUI SETUP PASS: typed profile sent through 9P")
                            return
                        time.sleep(0.5)
                    failure = Path("/tmp/o9irc-setup-fail.ppm")
                    failure.write_bytes(shot(sock, work, "setup-fail").read_bytes())
                    raise RuntimeError(f"Connection setup did not submit: {sent!r}; screen {failure}")

                boot = shot(sock, work, "channel-tail")
                channel_scrolled = False
                topic_scrolled = False
                for attempt in range(6):
                    time.sleep(1)
                    moved = shot(sock, work, f"scrolling-labels-{attempt}")
                    channel_scrolled |= region(boot, 142, 75, 208, 95) != region(moved, 142, 75, 208, 95)
                    topic_scrolled |= region(boot, 218, 110, 845, 135) != region(moved, 218, 110, 845, 135)
                    if channel_scrolled and topic_scrolled:
                        break
                if not channel_scrolled:
                    raise RuntimeError("long channel label did not scroll")
                if not topic_scrolled:
                    Path("/tmp/o9irc-topic-before.ppm").write_bytes(boot.read_bytes())
                    Path("/tmp/o9irc-topic-after.ppm").write_bytes(moved.read_bytes())
                    raise RuntimeError("long topic did not scroll")
                tail = chat_region(boot)
                move_to(218, 156)
                hmp(sock, "mouse_button 1")
                move_to(730, 156)
                hmp(sock, "mouse_button 0")
                selected = chat_region(shot(sock, work, "selected-chat"))
                if selected == tail:
                    raise RuntimeError("dragging chat text did not highlight it")
                hmp(sock, "mouse_button 4")
                time.sleep(0.35)
                hmp(sock, "mouse_button 0")
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    snarf = subprocess.run(["mtype", "-i", fat, "::o9irc-snarf"],
                                           capture_output=True, text=True).stdout
                    if re.search(r"<alice> historical message \d\d", snarf):
                        break
                    time.sleep(0.5)
                else:
                    raise RuntimeError(f"selected chat text did not reach /dev/snarf: {snarf!r}")
                click_at(218, 156)
                tail = chat_region(shot(sock, work, "channel-tail-cleared"))
                hmp(sock, "sendkey pgup")
                for attempt in range(8):
                    history_image = shot(sock, work, "channel-history")
                    if chat_region(history_image) != tail:
                        break
                    time.sleep(0.5)
                if chat_region(history_image) == tail:
                    raise RuntimeError("PgUp did not show older channel messages")
                hmp(sock, "sendkey end")
                for attempt in range(8):
                    live_image = shot(sock, work, "channel-live")
                    if chat_region(live_image) == tail:
                        break
                    time.sleep(0.5)
                if chat_region(live_image) != tail:
                    raise RuntimeError("End did not return to newest messages")
                move_to(218, 556)
                hmp(sock, "mouse_button 1")
                move_to(820, 594)
                hmp(sock, "mouse_button 0")
                hmp(sock, "mouse_button 4")
                time.sleep(0.35)
                hmp(sock, "mouse_button 0")
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    snarf = subprocess.run(["mtype", "-i", fat, "::o9irc-snarf"],
                                           capture_output=True, text=True).stdout
                    if "WRAPBEGIN" in snarf and "WRAPEND" in snarf:
                        break
                    time.sleep(0.5)
                else:
                    raise RuntimeError(f"copy across wrapped chat lost text: {snarf!r}")
                move_to(180, 140)
                tail = chat_region(shot(sock, work, "tail-without-cursor"))
                for y, expected_change in ((220, True), (244, True), (196, False)):
                    click_at(100, y)
                    for attempt in range(8):
                        chosen_image = shot(sock, work, f"conversation-{y}")
                        changed = chat_region(chosen_image) != tail
                        if changed == expected_change:
                            break
                        time.sleep(0.5)
                    if changed != expected_change:
                        raise RuntimeError(f"conversation selection at y={y} failed")
                    if y == 244:
                        for command in ("sendkey d", "sendkey m", "sendkey ret"):
                            hmp(sock, command)
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline:
                            sent = subprocess.run(["mtype", "-i", fat, "::o9irc-sent"],
                                                  capture_output=True, text=True).stdout
                            if "msg alice dm\n" in sent:
                                break
                            time.sleep(0.5)
                        else:
                            raise RuntimeError(f"direct message was not sent: {sent!r}")
                for command in ("sendkey o", "sendkey k", "sendkey ret"):
                    hmp(sock, command)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    sent = subprocess.run(["mtype", "-i", fat, "::o9irc-sent"],
                                          capture_output=True, text=True).stdout
                    if f"msg {channel_name} ok\n" in sent:
                        break
                    time.sleep(0.5)
                else:
                    raise RuntimeError(f"channel message was not sent: {sent!r}")
                before = pixel(boot, 180, 150)
                click_at(100, 150)
                for attempt in range(8):
                    status_image = shot(sock, work, "status")
                    after = pixel(status_image, 180, 150)
                    if after != before:
                        break
                    time.sleep(0.5)
                if after == before:
                    raise RuntimeError(f"Status click did not change view: {before} -> {after}")
                click_at(100, 196)
                click_at(875, 160)
                for command in ("sendkey h", "sendkey i", "sendkey ret"):
                    hmp(sock, command)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    sent = subprocess.run(["mtype", "-i", fat, "::o9irc-sent"],
                                          capture_output=True, text=True).stdout
                    if "msg alice hi\n" in sent:
                        break
                    time.sleep(0.5)
                else:
                    raise RuntimeError(f"nick click/keyboard did not write to 9P: {sent!r}")
                move_to(500, 300)
                before = shot(sock, work, "before-menu").read_bytes()
                hmp(sock, "mouse_button 2")
                time.sleep(0.35)
                menu = shot(sock, work, "menu").read_bytes()
                if sum(a != b for a, b in zip(before, menu)) < 1000:
                    raise RuntimeError("native Rio menu did not appear")
                hmp(sock, "mouse_button 0")
                hmp(sock, "sendkey esc")
        finally:
            stop(proc)
    print("O9IRC MOUSE PASS: channel and private views, nick input, scrollback, snarf copy, Rio menu")


if __name__ == "__main__":
    main()
