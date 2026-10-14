#!/usr/bin/env python3
"""Exercise the o9c IRC client against a local server from a 9front VM."""

import importlib.util
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HALATHA = Path(os.environ.get("HALATHA_REPO", ROOT.parent / "halatha"))
BUILDER = HALATHA / "extras/9front/kernel/build-kernel.py"
if len(sys.argv) != 2:
    sys.exit("usage: test_irc_qemu.py /path/to/9front.img")
IMAGE = Path(sys.argv[1])


def main():
    nick_conflict = os.environ.get("O9IRC_TEST_NICK_CONFLICT") == "1"
    accepted_nick = b"testnick_" if nick_conflict else b"testnick"
    spec = importlib.util.spec_from_file_location("builder", BUILDER)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    received = []
    failure = []
    guest_message = threading.Event()
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def serve():
            try:
                server.settimeout(90)
                conn, _ = server.accept()
                with conn:
                    conn.settimeout(30)
                    pending = b""

                    def get_line():
                        nonlocal pending
                        while b"\r\n" not in pending:
                            data = conn.recv(4096)
                            if not data:
                                raise AssertionError("client disconnected before expected IRC line")
                            pending += data
                        line, pending = pending.split(b"\r\n", 1)
                        received.append(line.decode())
                        return line

                    assert get_line() == b"NICK testnick"
                    assert get_line() == b"USER testnick 0 * :testnick"
                    if nick_conflict:
                        conn.sendall(b":vm.test 433 * testnick :Nickname is already in use\r\n")
                        assert get_line() == b"NICK testnick_"
                    conn.sendall(b"PING :split-")
                    conn.sendall(b"token\r\n:vm.test 001 " + accepted_nick + b" :welcome\r\n")
                    assert get_line() == b"PONG :split-token"
                    assert get_line() == b"JOIN #test"
                    conn.sendall(b":alice!u@h PRIVMSG #test :hello from server\r\n")
                    assert get_line() == b"PRIVMSG #test :hello from guest"
                    guest_message.set()
                    assert get_line() == b"QUIT :leaving"
            except Exception as exc:
                failure.append(exc)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        with tempfile.TemporaryDirectory(prefix="o9irc-qemu-") as temp:
            work = Path(temp)
            disk = work / "test.img"
            subprocess.run(["cp", "--sparse=always", IMAGE, disk], check=True)
            fat = f"{disk}@@{builder.ninefat_offset(disk)}"
            subprocess.run(["mmd", "-i", fat, "::o9irc"], check=True)
            subprocess.run(["mcopy", "-i", fat, ROOT / "irc.o9", "::o9irc/irc.o9"], check=True)
            subprocess.run(["mcopy", "-i", fat, ROOT / "protocol.o9", "::o9irc/protocol.o9"], check=True)
            qemu = ["qemu-system-i386", "-m", "1024", "-display", "none",
                    "-monitor", "none", "-serial", "stdio", "-no-reboot",
                    "-netdev", "user,id=n0",
                    "-device", "ne2k_pci,netdev=n0,mac=52:54:00:44:19:01",
                    "-drive", f"file={disk},format=raw,if=ide,index=0"]
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
                        for char in command + "\n":
                            proc.stdin.write(char.encode())
                            proc.stdin.flush()
                            time.sleep(0.006)
                        if console.expect(["term% "], timeout) is None:
                            raise RuntimeError(f"guest command timed out: {command}")
                        return logpath.read_text(errors="replace")

                    guest("9fs 9fat", 30)
                    guest("ramfs", 30)
                    guest("cp /n/9fat/o9irc/irc.o9 /tmp/irc.o9", 30)
                    guest("cp /n/9fat/o9irc/protocol.o9 /tmp/protocol.o9", 30)
                    guest("bind -b /usr/glenda/bin/386 /bin", 30)
                    guest("bind -b /usr/glenda/include /sys/include", 30)
                    guest("bind -b /usr/glenda/lib/o9 /386/lib", 30)
                    build = guest("o9build /tmp/irc.o9", 120)
                    if "built /tmp/irc" not in build:
                        raise RuntimeError("o9build failed")
                    guest("ip/ipconfig ether /net/ether0 10.0.2.15 255.255.255.0 10.0.2.2", 30)
                    guest_command = f"/tmp/irc irc tcp!10.0.2.2!{port} testnick '#test'"
                    for char in guest_command + "\n":
                        proc.stdin.write(char.encode())
                        proc.stdin.flush()
                        time.sleep(0.006)
                    if console.expect(["irc: connected;"], 40) is None:
                        raise RuntimeError("IRC connection did not start")
                    if console.expect(["hello from server"], 30) is None:
                        raise RuntimeError("server PRIVMSG was not displayed")
                    for char in "hello from guest\n":
                        proc.stdin.write(char.encode())
                        proc.stdin.flush()
                        time.sleep(0.006)
                    if not guest_message.wait(10):
                        raise RuntimeError(f"guest PRIVMSG was not sent; received {received}")
                    for char in "/quit\n":
                        proc.stdin.write(char.encode())
                        proc.stdin.flush()
                        time.sleep(0.006)
                    if console.expect(["term% "], 40) is None:
                        raise RuntimeError("IRC client did not quit")
                    thread.join(10)
                    if thread.is_alive() or failure:
                        raise RuntimeError(f"IRC server failed: {failure or 'timed out'}; received {received}")
                    output = logpath.read_text(errors="replace")
                    if "hello from server" not in output:
                        raise RuntimeError("server PRIVMSG was not displayed")
                except Exception:
                    log.flush()
                    print(logpath.read_text(errors="replace")[-8000:], file=sys.stderr)
                    raise
                finally:
                    proc.kill()
                    proc.wait()
    print("O9IRC VM PASS: registration, fragmented PING/PONG, JOIN, receive, send, quit")


if __name__ == "__main__":
    main()
