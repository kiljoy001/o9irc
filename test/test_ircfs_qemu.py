#!/usr/bin/env python3
"""Mount the o9c IRC view in 9front and exercise its network actor."""

import importlib.util
import hashlib
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OBJECTIVE = Path(os.environ.get("OBJECTIVE_9C_REPO", ROOT.parent / "objective-9c"))
HALATHA = Path(os.environ.get("HALATHA_REPO", ROOT.parent / "halatha"))
BUILDER = HALATHA / "extras/9front/kernel/build-kernel.py"
if len(sys.argv) != 2:
    sys.exit("usage: test_ircfs_qemu.py /path/to/9front-with-o9c.img")
IMAGE = Path(sys.argv[1])
ARCH = os.environ.get("O9IRC_VM_ARCH", "386")
if ARCH not in ("386", "amd64"):
    sys.exit("O9IRC_VM_ARCH must be 386 or amd64")
NIC = os.environ.get("O9IRC_VM_NIC", "e1000" if ARCH == "amd64" else "ne2k_pci")
TEST_SOURCE = Path(os.environ.get("O9IRCFS_TEST_SOURCE", ROOT / "ircfs.o9"))


def main():
    export_dir = os.environ.get("O9IRCFS_EXPORT_BINARIES")
    stress_count = int(os.environ.get("O9IRC_STRESS_COUNT", "0"))
    stress_batches = int(os.environ.get("O9IRC_STRESS_BATCHES", "1"))
    history_disabled = os.environ.get("O9IRC_NO_HISTORY") == "1"
    spawn_probe = os.environ.get("O9IRC_SPAWN_PROBE") == "1"
    string_probe = os.environ.get("O9IRC_STRING_PROBE") == "1"
    nick_conflict = os.environ.get("O9IRC_TEST_NICK_CONFLICT") == "1"
    setup_mode = os.environ.get("O9IRC_TEST_SETUP") == "1"
    tls_cli = os.environ.get("O9IRC_TEST_TLS_CLI") == "1"
    tls_mode = os.environ.get("O9IRC_TEST_TLS") == "1" or tls_cli
    if tls_mode and not (setup_mode or tls_cli):
        raise RuntimeError("O9IRC_TEST_TLS requires setup or TLS CLI mode")
    tls_context = None
    accepted_nick = b"testnick_" if nick_conflict else b"testnick"
    complete = False
    spec = importlib.util.spec_from_file_location("builder", BUILDER)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    failure = []
    received = []
    accepted = threading.Event()
    mismatch_seen = threading.Event()
    welcome_sent = threading.Event()
    guest_message = threading.Event()
    burst_processed = threading.Event()
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(2)
        port = server.getsockname()[1]

        def serve():
            try:
                server.settimeout(90)
                if tls_mode and setup_mode:
                    rejected_conn, _ = server.accept()
                    with tls_context.wrap_socket(rejected_conn, server_side=True) as rejected_tls:
                        rejected_tls.settimeout(20)
                        if rejected_tls.recv(1):
                            raise AssertionError("IRC bytes sent to untrusted TLS server")
                    mismatch_seen.set()
                conn, _ = server.accept()
                accepted.set()
                if tls_mode:
                    conn = tls_context.wrap_socket(conn, server_side=True)
                with conn:
                    conn.settimeout(30)
                    pending = b""

                    def get_line():
                        nonlocal pending
                        while b"\r\n" not in pending:
                            data = conn.recv(4096)
                            if not data:
                                raise AssertionError("IRC view closed connection early")
                            pending += data
                        line, pending = pending.split(b"\r\n", 1)
                        received.append(line.decode())
                        return line

                    assert get_line() == b"NICK testnick"
                    real_name = b"Setup Tester" if setup_mode else b"testnick"
                    assert get_line() == b"USER testnick 0 * :" + real_name
                    if nick_conflict:
                        conn.sendall(b":vm.test 433 * testnick :Nickname is already in use\r\n")
                        assert get_line() == b"NICK testnick_"
                    conn.sendall(b"@time=now :vm.test PING :view-token\r\n")
                    conn.sendall(b":vm.test 001 " + accepted_nick + b" :welcome\r\n")
                    assert get_line() == b"PONG :view-token"
                    assert get_line() == b"JOIN #test"
                    conn.sendall(b":vm.test 332 " + accepted_nick + b" #test :A topic from 9P\r\n")
                    conn.sendall(b":vm.test 353 " + accepted_nick + b" = #test :@alice bob " + accepted_nick + b"\r\n")
                    conn.sendall(b":vm.test 366 " + accepted_nick + b" #test :End of names\r\n")
                    conn.sendall(b":alice!u@h PRIVMSG #test :view hello\r\n")
                    conn.sendall(b":alice!u@h PRIVMSG " + accepted_nick + b" :private hello\r\n")
                    welcome_sent.set()
                    assert get_line() == b"JOIN #other"
                    conn.sendall(b":vm.test 332 " + accepted_nick + b" #other :Other topic\r\n")
                    conn.sendall(b":vm.test 353 " + accepted_nick + b" = #other :@carol " + accepted_nick + b"\r\n")
                    conn.sendall(b":vm.test 366 " + accepted_nick + b" #other :End of names\r\n")
                    conn.sendall(b":carol!u@h PRIVMSG #other :other hello\r\n")
                    conn.sendall(b":bob!u@h PRIVMSG " + accepted_nick + b" :bob private\r\n")
                    assert get_line() == b"PRIVMSG #other :from view"
                    assert get_line() == b"PRIVMSG bob :reply"
                    assert get_line() == b"PRIVMSG #test :back to first"
                    guest_message.set()
                    for batch in range(stress_batches):
                        for i in range(stress_count):
                            number = batch * stress_count + i
                            conn.sendall(f":alice!u@h PRIVMSG #test :stress {number:04d}\r\n".encode())
                        if stress_count and not burst_processed.wait(120):
                            raise AssertionError("guest did not process IRC burst before disconnect")
                        burst_processed.clear()
            except Exception as exc:
                failure.append(exc)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        with tempfile.TemporaryDirectory(prefix="o9ircfs-qemu-") as temp:
            work = Path(temp)
            if tls_mode:
                cert = work / "server.crt"
                key = work / "server.key"
                subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048",
                                "-sha256", "-nodes", "-subj", "/CN=10.0.2.2",
                                "-days", "1", "-keyout", str(key), "-out", str(cert)],
                               check=True, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
                tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                tls_context.maximum_version = ssl.TLSVersion.TLSv1_2
                tls_context.load_cert_chain(cert, key)
                der = ssl.PEM_cert_to_DER_cert(cert.read_text())
                thumb = work / "trustedkeys"
                thumb.write_text("x509 sha256=" + hashlib.sha256(der).hexdigest() + "\n")
                bad_thumb = work / "badkeys"
                bad_thumb.write_text("x509 sha256=" + "0" * 64 + "\n")
            disk = work / "test.img"
            subprocess.run(["cp", "--sparse=always", IMAGE, disk], check=True)
            fat = f"{disk}@@{builder.ninefat_offset(disk)}"
            subprocess.run(["mmd", "-i", fat, "::o9irc"], check=True)
            sources = ("ircfs.o9", "protocol.o9", "history.o9", "gui.o9",
                       "uiio.o9", "draw.o9", "ui_input_probe.o9", "profile_probe.o9",
                       "history_probe.o9")
            if spawn_probe:
                sources += ("spawn_memory_probe.o9",)
            if string_probe:
                sources += ("string_memory_probe.o9",)
            for name in sources:
                source = (OBJECTIVE / "stdlib" / name if name == "draw.o9" else
                          TEST_SOURCE if name == "ircfs.o9" else
                          ROOT / "test" / name if name in ("ui_input_probe.o9", "profile_probe.o9", "history_probe.o9", "spawn_memory_probe.o9", "string_memory_probe.o9") else
                          ROOT / name)
                subprocess.run(["mcopy", "-i", fat, source, f"::o9irc/{name}"], check=True)
            if tls_mode:
                subprocess.run(["mcopy", "-i", fat, thumb,
                                "::o9irc/trustedkeys"], check=True)
            if setup_mode:
                profile = f"10.0.2.2\t{port}\ttestnick\t#test\tSetup Tester"
                if tls_mode:
                    profile += "\ttls\t/tmp/trustedkeys"
                    subprocess.run(["mcopy", "-i", fat, bad_thumb,
                                    "::o9irc/badkeys"], check=True)
                commands = [("savecmd", "profile\t" + profile + "\n"),
                            ("connectcmd", "connect\t" + profile + "\n")]
                if tls_mode:
                    commands.append(("badcmd", "connect\t" +
                                     profile.replace("/tmp/trustedkeys", "/tmp/missingkeys") + "\n"))
                    commands.append(("mismatchcmd", "connect\t" +
                                     profile.replace("/tmp/trustedkeys", "/tmp/badkeys") + "\n"))
                for name, command in commands:
                    prepared = work / name
                    prepared.write_text(command)
                    subprocess.run(["mcopy", "-i", fat, prepared,
                                    f"::o9irc/{name}"], check=True)
            qemu = [f"qemu-system-{'x86_64' if ARCH == 'amd64' else 'i386'}", "-m", "1024", "-display", "none",
                    "-monitor", "none", "-serial", "stdio", "-no-reboot",
                    "-netdev", "user,id=n0",
                    "-device", f"{NIC},netdev=n0,mac=52:54:00:44:19:02",
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
                    if spawn_probe or string_probe:
                        stem = "spawn_memory_probe" if spawn_probe else "string_memory_probe"
                        tag = "SPAWN" if spawn_probe else "STRING"
                        result = guest(f"o9build /tmp/{stem}.o9", 120)
                        if f"built /tmp/{stem}" not in result:
                            raise RuntimeError(f"{tag} probe build failed:\n" + result)
                        guest(f"/tmp/{stem} probe >/tmp/memory-probe.log >[2=1] &", 30)
                        probe_sizes = []
                        for marker in (f"{tag}_PROBE_START", f"{tag}_PROBE_DONE"):
                            deadline = time.monotonic() + 60
                            while time.monotonic() < deadline:
                                output = guest("cat /tmp/memory-probe.log", 30)
                                if marker in output:
                                    break
                                time.sleep(0.2)
                            else:
                                raise RuntimeError(f"spawn probe did not reach {marker}: {output!r}")
                            sizes = re.findall(r"\b(\d+)K\b", guest(f"ps | grep {stem}", 30))
                            print(f"O9IRC {tag} {marker} {sizes!r}", flush=True)
                            if not sizes:
                                raise RuntimeError(f"spawn probe process missing at {marker}")
                            probe_sizes.append(int(sizes[0]))
                        if probe_sizes[1] - probe_sizes[0] > 128:
                            raise RuntimeError(f"{tag.lower()} probe memory grew: {probe_sizes!r} KiB")
                        complete = True
                        return
                    build = guest("o9build /tmp/ircfs.o9", 120)
                    if "built /tmp/ircfs" not in build:
                        raise RuntimeError("o9build failed:\n" + build)
                    gui_build = guest("o9build /tmp/gui.o9", 120)
                    if "built /tmp/gui" not in gui_build:
                        raise RuntimeError("GUI build failed:\n" + gui_build)
                    probe_build = guest("o9build /tmp/ui_input_probe.o9", 120)
                    if "built /tmp/ui_input_probe" not in probe_build:
                        raise RuntimeError("GUI input probe build failed:\n" + probe_build)
                    if setup_mode:
                        profile_build = guest("o9build /tmp/profile_probe.o9", 120)
                        if "built /tmp/profile_probe" not in profile_build:
                            raise RuntimeError("profile probe build failed:\n" + profile_build)
                    history_build = guest("o9build /tmp/history_probe.o9", 120)
                    if "built /tmp/history_probe" not in history_build:
                        raise RuntimeError("history probe build failed:\n" + history_build)
                    history_probe = guest("/tmp/history_probe history-probe", 120)
                    if ("HISTORY_LIMIT_OK" not in history_probe or
                            "HISTORY_RAW_LIMIT_OK" not in history_probe or
                            "_FAIL" in history_probe):
                        raise RuntimeError("compressed history probe failed:\n" + history_probe)
                    if export_dir:
                        guest(f"cp /tmp/ircfs /n/9fat/o9irc/ircfs.{ARCH}", 30)
                        guest(f"cp /tmp/gui /n/9fat/o9irc/gui.{ARCH}", 30)
                    if os.environ.get("O9IRCFS_COMPILE_ONLY"):
                        complete = True
                        print("O9IRCFS VM COMPILE PASS")
                        return
                    guest("ip/ipconfig ether /net/ether0 10.0.2.15 255.255.255.0 10.0.2.2", 30)
                    history_args = " /tmp/o9irc-history 0" if history_disabled else ""
                    if tls_mode:
                        guest("cp /n/9fat/o9irc/trustedkeys /tmp/trustedkeys", 30)
                    if setup_mode:
                        if tls_mode:
                            guest("cp /n/9fat/o9irc/badkeys /tmp/badkeys", 30)
                        guest("/tmp/ircfs ircfs '' '' '' /tmp/o9irc-profile 262144 >/tmp/ircfs.log >[2=1] &", 30)
                    elif tls_cli:
                        guest(f"/tmp/ircfs ircfs tls!10.0.2.2!{port} testnick '#test' /tmp/o9irc-history 262144 /tmp/trustedkeys >/tmp/ircfs.log >[2=1] &", 30)
                    else:
                        guest(f"/tmp/ircfs ircfs tcp!10.0.2.2!{port} testnick '#test'{history_args} >/tmp/ircfs.log >[2=1] &", 30)
                    guest("mkdir /mnt/o9irc", 30)
                    mount = guest("mount -c /srv/ircfs /mnt/o9irc", 30)
                    if "mount:" in mount:
                        raise RuntimeError("9P mount failed:\n" + mount)
                    if setup_mode:
                        root = "/mnt/o9irc/view/networks/default"
                        idle = guest(f"cat {root}/status", 30)
                        if "disconnected" not in idle or accepted.is_set():
                            raise RuntimeError(f"service did not start disconnected: {idle!r}")
                        saved = guest(f"/tmp/profile_probe probe {root}/input /n/9fat/o9irc/savecmd", 30)
                        if "o9irc-profile-write: " not in saved or "-1" in saved:
                            raise RuntimeError(f"profile save failed: {saved!r}")
                        profile_view = guest(f"cat {root}/profile", 30)
                        idle = guest(f"cat {root}/status", 30)
                        if "Setup Tester" not in profile_view or "disconnected" not in idle:
                            raise RuntimeError(f"profile did not save without connecting: {profile_view!r} {idle!r}")
                        if tls_mode:
                            rejected = guest(f"/tmp/profile_probe probe {root}/input /n/9fat/o9irc/badcmd", 30)
                            bad_status = guest(f"cat {root}/statuslog", 30)
                            if ("o9irc-profile-write: " not in rejected or
                                    "TLS trusted-keys file is missing" not in bad_status or
                                    accepted.is_set()):
                                raise RuntimeError(f"TLS missing-key policy failed: {rejected!r} {bad_status!r}")
                            mismatch = guest(f"/tmp/profile_probe probe {root}/input /n/9fat/o9irc/mismatchcmd", 30)
                            mismatch_status = guest(f"cat {root}/statuslog", 30)
                            if ("o9irc-profile-write: " not in mismatch or
                                    "TLS certificate does not match trusted keys" not in mismatch_status or
                                    not mismatch_seen.wait(5) or accepted.is_set()):
                                raise RuntimeError(f"TLS mismatch policy failed: {mismatch!r} {mismatch_status!r} {failure!r}")
                        started = guest(f"/tmp/profile_probe probe {root}/input /n/9fat/o9irc/connectcmd", 30)
                        if "o9irc-profile-write: " not in started or "-1" in started:
                            raise RuntimeError(f"profile connect failed: {started!r}")
                    if not welcome_sent.wait(10):
                        runtime = guest("cat /tmp/ircfs.log", 30)
                        view_status = guest("cat /mnt/o9irc/view/networks/default/status", 30)
                        raise RuntimeError(f"server handshake failed: accepted={accepted.is_set()} {failure or received}; runtime: {runtime}; view: {view_status}")
                    deadline = time.monotonic() + 10
                    status = ""
                    while time.monotonic() < deadline:
                        status = guest("cat /mnt/o9irc/view/networks/default/status", 30)
                        if "connected" in status:
                            break
                        if "view unavailable" in status:
                            guest("unmount /mnt/o9irc", 30)
                            guest("mount -c /srv/ircfs /mnt/o9irc", 30)
                        time.sleep(0.1)
                    if "connected" not in status:
                        runtime = guest("cat /tmp/ircfs.log", 30)
                        root_listing = guest("ls /mnt/o9irc", 30)
                        raise RuntimeError(f"9P status view missing:\n{status}\nruntime:\n{runtime}\nroot:\n{root_listing}")
                    deadline = time.monotonic() + 10
                    events = ""
                    while time.monotonic() < deadline:
                        events = guest("cat /mnt/o9irc/view/networks/default/events", 30)
                        if "view hello" in events and "private hello" in events:
                            break
                        time.sleep(0.1)
                    if "view hello" not in events or "private hello" not in events:
                        raise RuntimeError("9P events view missing:\n" + events)
                    topic = guest("cat /mnt/o9irc/view/networks/default/topic", 30)
                    nicks = guest("cat /mnt/o9irc/view/networks/default/nicks", 30)
                    transcript = guest("cat /mnt/o9irc/view/networks/default/transcript", 30)
                    statuslog = guest("cat /mnt/o9irc/view/networks/default/statuslog", 30)
                    if "A topic from 9P" not in topic or "@alice" not in nicks or "bob" not in nicks:
                        raise RuntimeError(f"9P topic/nicks missing: {topic!r} {nicks!r}")
                    if "<alice> view hello" not in transcript:
                        raise RuntimeError("9P transcript missing:\n" + transcript)
                    if f"332 {accepted_nick.decode()} #test" not in statuslog or "view hello" in statuslog:
                        raise RuntimeError("9P status stream is not separated from chat:\n" + statuslog)
                    if nick_conflict and "nickname in use; trying testnick_" not in statuslog:
                        raise RuntimeError("nickname fallback was not shown in status:\n" + statuslog)
                    if "private hello" in statuslog or "private hello" in transcript:
                        raise RuntimeError("private message was mixed into another view")
                    if f"332 {accepted_nick.decode()} #test" in transcript:
                        raise RuntimeError("server replies leaked into chat transcript:\n" + transcript)
                    index = guest("cat /mnt/o9irc/view/networks/default/conversations", 30)
                    first = "channels/2374657374"
                    alice = "users/616c696365"
                    if first not in index or alice not in index:
                        raise RuntimeError(f"conversation index missing: {index!r}")
                    private = guest(f"cat /mnt/o9irc/view/networks/default/{alice}/transcript", 30)
                    if "<alice> private hello" not in private:
                        raise RuntimeError(f"private conversation missing: {private!r}")
                    history_status = guest("cat /mnt/o9irc/view/networks/default/historystatus", 30)
                    history_path = guest("cat /mnt/o9irc/view/networks/default/historypath", 30)
                    if not history_disabled and ("saved" not in history_status or ".o9h" not in history_path):
                        raise RuntimeError(f"history was not saved: {history_status!r} {history_path!r}")
                    baseline_ps = guest("ps | grep ircfs", 30)
                    baseline_sizes = [int(x) for x in re.findall(r"\b(\d+)K\b", baseline_ps)]
                    write = guest("/tmp/ui_input_probe probe /mnt/o9irc/view/networks/default/input 'join #other'", 30)
                    if "o9irc-ui-write: 12" not in write:
                        raise RuntimeError("GUI join write failed:\n" + write)
                    other = "channels/236f74686572"
                    bob = "users/626f62"
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        index = guest("cat /mnt/o9irc/view/networks/default/conversations", 30)
                        if other in index and bob in index:
                            break
                        time.sleep(0.1)
                    if other not in index or bob not in index:
                        raise RuntimeError(f"multiple conversations missing: {index!r}")
                    first_text = guest(f"cat /mnt/o9irc/view/networks/default/{first}/transcript", 30)
                    other_text = guest(f"cat /mnt/o9irc/view/networks/default/{other}/transcript", 30)
                    bob_text = guest(f"cat /mnt/o9irc/view/networks/default/{bob}/transcript", 30)
                    if "view hello" not in first_text or "other hello" in first_text:
                        raise RuntimeError(f"first channel was overwritten: {first_text!r}")
                    if "other hello" not in other_text or "view hello" in other_text:
                        raise RuntimeError(f"second channel routed incorrectly: {other_text!r}")
                    if "bob private" not in bob_text or "bob private" in other_text:
                        raise RuntimeError(f"private message routed incorrectly: {bob_text!r}")
                    if not history_disabled:
                        for conversation in (first, other, alice, bob):
                            saved = guest(f"cat /mnt/o9irc/view/networks/default/{conversation}/historystatus", 30)
                            path = guest(f"cat /mnt/o9irc/view/networks/default/{conversation}/historypath", 30)
                            if "saved" not in saved or ".o9h" not in path:
                                raise RuntimeError(f"compressed history missing for {conversation}: {saved!r} {path!r}")
                    for command in ("say from view", "msg bob reply", "msg #test back to first"):
                        write = guest(f"/tmp/ui_input_probe probe /mnt/o9irc/view/networks/default/input '{command}'", 30)
                        if "o9irc-ui-write:" not in write:
                            raise RuntimeError(f"GUI command write failed: {write!r}")
                    if not guest_message.wait(10):
                        raise RuntimeError(f"9P command not sent: {failure or received}")
                    if stress_count:
                        previous_size = None
                        for batch in range(stress_batches):
                            deadline = time.monotonic() + 120
                            needle = f"stress {(batch + 1) * stress_count - 1:04d}"
                            while time.monotonic() < deadline:
                                transcript = guest(f"cat /mnt/o9irc/view/networks/default/{first}/transcript", 30)
                                if needle in transcript:
                                    break
                                time.sleep(0.2)
                            if needle not in transcript:
                                raise RuntimeError(f"IRC burst was not fully processed: {transcript[-300:]!r}")
                            current_ps = guest("ps | grep ircfs", 30)
                            current_sizes = [int(x) for x in re.findall(r"\b(\d+)K\b", current_ps)]
                            print(f"O9IRCFS MEMORY {baseline_sizes[0]} -> {current_sizes[0]} KiB after {(batch + 1) * stress_count} lines", flush=True)
                            if (previous_size is not None and stress_count >= 100 and
                                    current_sizes[0] - previous_size > max(1024, 4 * stress_count)):
                                raise RuntimeError("IRC memory grew across repeated bursts")
                            previous_size = current_sizes[0]
                            burst_processed.set()
                    thread.join(10)
                    if thread.is_alive() or failure:
                        raise RuntimeError(f"IRC server failed: {failure or 'timed out'}; received {received}")
                    if history_disabled:
                        complete = True
                        return
                    if setup_mode:
                        guest("/tmp/ircfs replay '' '' '' /tmp/o9irc-profile 262144 >/tmp/ircfs-replay.log >[2=1] &", 30)
                    elif tls_cli:
                        guest(f"/tmp/ircfs replay tls!10.0.2.2!{port} testnick '#test' /tmp/o9irc-history 262144 /tmp/trustedkeys >/tmp/ircfs-replay.log >[2=1] &", 30)
                    else:
                        guest(f"/tmp/ircfs replay tcp!10.0.2.2!{port} testnick '#test' >/tmp/ircfs-replay.log >[2=1] &", 30)
                    guest("mkdir /mnt/o9irc-replay", 30)
                    if setup_mode:
                        guest("mount -c /srv/replay /mnt/o9irc-replay", 30)
                        restarted = guest("/tmp/profile_probe probe /mnt/o9irc-replay/view/networks/default/input /n/9fat/o9irc/connectcmd", 30)
                        if "o9irc-profile-write: " not in restarted or "-1" in restarted:
                            raise RuntimeError(f"restarted profile connect failed: {restarted!r}")
                        guest("unmount /mnt/o9irc-replay", 30)
                    replay = ""
                    deadline = time.monotonic() + 10
                    replay_path = "channels/2374657374/transcript" if setup_mode else "transcript"
                    while time.monotonic() < deadline:
                        guest("mount -c /srv/replay /mnt/o9irc-replay", 30)
                        replay = guest(f"cat /mnt/o9irc-replay/view/networks/default/{replay_path}", 30)
                        if "<alice> view hello" in replay:
                            break
                        guest("unmount /mnt/o9irc-replay", 30)
                        time.sleep(0.2)
                    if "<alice> view hello" not in replay:
                        raise RuntimeError("history did not survive service restart:\n" + replay)
                    if setup_mode:
                        restored_profile = guest("cat /mnt/o9irc-replay/view/networks/default/profile", 30)
                        if "Setup Tester" not in restored_profile:
                            raise RuntimeError("connection profile did not survive service restart:\n" + restored_profile)
                    for conversation, expected in ((other, "other hello"),
                                                   (alice, "private hello"),
                                                   (bob, "bob private")):
                        restored = guest(f"cat /mnt/o9irc-replay/view/networks/default/{conversation}/transcript", 30)
                        if expected not in restored:
                            raise RuntimeError(f"compressed history did not restore {conversation}: {restored!r}")
                    complete = True
                except Exception:
                    log.flush()
                    print(logpath.read_text(errors="replace")[-2500:], file=sys.stderr)
                    raise
                finally:
                    proc.kill()
                    proc.wait()
                    if complete and export_dir:
                        destination = Path(export_dir)
                        destination.mkdir(parents=True, exist_ok=True)
                        for name in ("ircfs", "gui"):
                            subprocess.run(["mcopy", "-o", "-i", fat,
                                            f"::o9irc/{name}.{ARCH}",
                                            str(destination / f"{name}.{ARCH}")], check=True)
    mode = "TLS command line" if tls_cli else "TLS profile and pin rejection" if tls_mode else "plain TCP"
    print(f"O9IRCFS VM PASS ({mode}): multiple channels, private messages, 9P routing, compressed history")


if __name__ == "__main__":
    main()
