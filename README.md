# o9irc

An IRC client written in o9c for 9front. The project has a terminal client,
a 9P service, and a Rio GUI fed by that service. The terminal client and
service share IRC wire functions in `protocol.o9`.

Build on 9front with an installed o9c toolchain:

```rc
o9build irc.o9
o9build ircfs.o9
o9build gui.o9
```

Connect to a server:

```rc
./irc irc tcp!irc.example.net!6667 nickname '#channel'
```

The first `irc` argument is the o9c service name. The address uses Plan 9's
`dial` syntax. This initial version uses plain TCP; do not send a server
password or other secrets through it. The client handles IRC registration,
server PING/PONG, automatic channel join after welcome, and terminal input.
If the server rejects the chosen nickname during registration, it tries a
short fallback such as `nickname_`, then numbered variants through `_9`.

Type a message to send to the current channel, or use `/join #channel`,
`/msg target text`, `/nick newname`, `/help`, and `/quit`. Server messages are
shown as raw IRC lines so protocol errors remain visible.

The 9P service exposes an actor owned network view. To set up a connection in
Rio, start the launcher without arguments:

```rc
o9irc-start
```

The Connection setup window opens when the service is disconnected. Enter the
server, port, nickname, optional channel, and optional full name, then select
**Connect**. **Save** keeps the profile for later without connecting. The
profile is stored as a private file under `$home/lib/o9irc/connection` and is
reloaded on the next start. Choose **Connection setup** from the toolbar menu
or the button 3 menu to edit it later. Select **Use TLS** for an encrypted
connection, then enter an absolute path to a 9front trusted-keys file. TLS
requires a matching server certificate and TLS 1.2 or newer; an absent or
mismatched file closes the connection. The file contains a verified certificate
thumbprint:

```text
x509 sha256=<64 hexadecimal digits>
```

Verify the thumbprint through a trusted channel before adding it. For example,
on a machine where the server certificate already validates, derive its
fingerprint with `openssl s_client -connect host:6697 -servername host` piped
through `openssl x509 -outform DER` and `sha256sum`. A certificate pin may need
updating when the server changes its certificate. Plain TCP remains available
for servers without TLS; do not send secrets over it. A service handles one server's
history at a time; restart the service before connecting to a different server
after it has collected conversations.

The command line launch remains available. Start the service with a distinct
service name and mount it in another terminal:

```rc
./ircfs ircfs tcp!irc.example.net!6667 nickname '#channel'
mkdir /mnt/o9irc
mount /srv/ircfs /mnt/o9irc
cat /mnt/o9irc/view/networks/default/status
cat /mnt/o9irc/view/networks/default/events
echo 'say hello' >> /mnt/o9irc/view/networks/default/input
./gui gui /mnt/o9irc
```

For a command line TLS connection, give a `tls!host!port` address and pass the
trusted-keys file after the optional history directory and byte limit:

```rc
./ircfs ircfs tls!irc.example.net!6697 nickname '#channel' $home/lib/o9irc 262144 $home/lib/o9irc/irc.example.net.keys
```

The view also accepts `join #channel`, `msg target text`, `nick newname`, and
`quit` in `input`. `profile<TAB>host<TAB>port<TAB>nick<TAB>channel<TAB>full name<TAB>tls<TAB>/absolute/path/to/trustedkeys`
saves a TLS profile; use `tcp` and an empty final field for plain TCP.
`connect<TAB>...` saves it and starts the connection. Existing five-field
profiles remain valid and use plain TCP. The Rio
GUI reads the conversation index and selected
conversation's topic, transcript, and nick names from this mount, and writes
commands to `input`; it does not own a socket. Type plain messages in its
input line, or use `/join`, `/msg`, `/nick`, and `/quit`. Press Esc to close the
GUI. `events` remains a bounded snapshot of raw IRC lines. The GUI has a
classic mIRC-like tree, switchbar, conversation, and nick list. Click Status,
a channel, or a person in the tree to switch views. A plain message goes to
the selected channel or person. Click the Menu label or press mouse button 3
to open a native Rio menu. Its Join, Nick, and Message options fill the command
line; clicking a nick also starts a private message.
Drag over chat text with mouse button 1 to select it. Mouse button 2 copies the
selection to Rio's `/dev/snarf`; the native menu also has Copy selection.
Use the mouse wheel, PgUp/PgDn, or Home/End over the conversation to read older
messages. Each 9P `transcript` remains plain text, while the service saves a
compressed archive for each server and conversation under `$home/lib/o9irc`.
The transcript wraps at pixel measured word boundaries; selecting across a
wrapped line still copies the original plain text. Long channel labels and
topics scroll within their bars while the GUI is open.
The archive has a default limit of 262144 bytes **after compression**. On
overflow, the service removes the oldest complete lines until the compressed
archive fits. Each conversation's `historypath` and `historystatus` files show
the archive location and whether saving succeeded. A private index lets the
service reload channel and private message archives after a restart.

Set `o9irchistorydir` and `o9irchistorybytes` before starting `o9irc-start`
to change the directory and compressed byte limit. The byte limit accepts
4096 through 1048576; set it to `0` to disable disk history. For example:

```rc
o9irchistorybytes=131072
o9irc-start 'tcp!irc.example.net!6667' nickname '#channel'
```

These settings take effect when a new service starts; an already running
service keeps its current settings. History files are private to the 9front
user and the GUI never reads the compressed format directly.
It currently shows one network and up to 64 channel or private conversations. See
[DESIGN.md](DESIGN.md) for the view layout and next steps.

Next steps: stable event cursors, native TLS, and richer IRC state.

To run the protocol test from Linux, use a local 9front disk image with o9c
installed:

```sh
python3 test/test_irc_qemu.py /path/to/9front.img
python3 test/test_ircfs_qemu.py /path/to/9front.img
O9IRC_TEST_SETUP=1 O9IRC_VM_ARCH=amd64 python3 test/test_ircfs_qemu.py /path/to/9front-with-current-o9c.img
O9IRC_TEST_SETUP=1 O9IRC_TEST_TLS=1 O9IRC_VM_ARCH=amd64 python3 test/test_ircfs_qemu.py /path/to/9front-with-current-o9c.img
O9IRC_TEST_TLS_CLI=1 O9IRC_VM_ARCH=amd64 python3 test/test_ircfs_qemu.py /path/to/9front-with-current-o9c.img
O9IRC_TEST_NICK_CONFLICT=1 python3 test/test_irc_qemu.py /path/to/9front.img
O9IRC_TEST_NICK_CONFLICT=1 O9IRC_NO_HISTORY=1 python3 test/test_ircfs_qemu.py /path/to/9front.img
python3 test/test_gui_mock_qemu.py /path/to/9front-with-current-o9c.img
python3 test/test_gui_mouse_qemu.py /path/to/graphical-9front-with-current-o9c.img
O9IRC_SETUP_ONLY=1 O9IRC_VM_ARCH=amd64 python3 test/test_gui_mouse_qemu.py /path/to/graphical-9front-with-current-o9c.img
O9IRC_SETUP_ONLY=1 O9IRC_SETUP_TLS=1 O9IRC_VM_ARCH=amd64 python3 test/test_gui_mouse_qemu.py /path/to/graphical-9front-with-current-o9c.img
O9IRC_MEMORY_ONLY=1 python3 test/test_gui_mouse_qemu.py /path/to/graphical-9front-with-current-o9c.img
O9IRC_STRESS_COUNT=200 python3 test/test_ircfs_qemu.py /path/to/9front-with-current-o9c.img
```

Host-side logic checks run without QEMU. They need plan9port and a host build
of objective-9c's runtime:

```sh
O9_HOST_BUILD=/tmp/o9host sh ../objective-9c/tools/host_test.sh
O9_HOST_BUILD=/tmp/o9host python3 test/host_logic.py
```

The second command transpiles the real GUI, protocol, and history modules with
`o9c --no-facade`, links objective-9c's host runtime, and checks input/profile
helpers, the zlib history roundtrip, and transcript wrapping and hit testing.
It also runs `test/check_task_lifetime.py`, which rejects a `Task.await()` left
without a matching close in long-running code.

The IRC tests boot a temporary copy of the image, build the client inside
9front, and talk to a small IRC server on the host. They leave the source
image alone.
The mock UI test uses no IRC server. It compiles the GUI, mounts a fixed 9P
view, and checks that the GUI's input adapter writes commands to it.
The setup mode checks typing into the Rio form and confirms its
Connect button writes the expected command. The mouse test uses a disposable graphical VM to check conversation switching,
scrollback, text selection, and copying into `/dev/snarf`. It removes its disk
and screenshots when it exits.
The memory mode samples GUI process size in the guest for 55 seconds and fails
if it grows by more than 1 MiB, catching timer loop leaks.

To inspect the GUI itself in a Rio terminal with synthetic data:

```rc
o9build test/mock_view.o9
o9build gui.o9
test/mock_view o9irc-mock </dev/null >/tmp/o9irc-mock.log >[2=1] &
mkdir /mnt/o9irc-mock
mount -c /srv/o9irc-mock /mnt/o9irc-mock
window -m gui gui /mnt/o9irc-mock
cat /mnt/o9irc-mock/view/networks/default/sent
```

The mock displays two channels and a private conversation, and records input writes in `sent`, so menu and
keyboard behavior can be checked without opening an IRC connection.

## dev9p install

The amd64 client, service, and GUI are installed under `/usr/scott/bin/amd64`
on dev9p. In a Rio terminal there, start the GUI and fill in Connection setup:

```rc
o9irc-start
```

The command line connection form is also available:

```rc
o9irc-start 'tcp!irc.example.net!6667' nickname '#channel'
```

The launcher posts `/srv/o9irc`, mounts it at `/mnt/o9irc` for the GUI, and
leaves the service running when the GUI closes. Running the launcher again
reopens the existing service. Replace the example address, nickname, and
channel with your IRC server details. The currently installed system `o9c`
on dev9p predates this app's 9P views; the app was built with a private current
toolchain. Its versioned compiler can be queried with `o9c-0.2.0-dev --version`.

Use `o9irc-start` for the normal session. It reopens the GUI for an existing
`/srv/o9irc` connection, so closing the window does not disconnect IRC.
An existing service keeps running its previous binary until it is restarted.
The connection setup form requires a service that exposes
`/mnt/o9irc/view/networks/default/profile`; if that file is absent, restart
the service when you are ready to drop the current IRC connection. Reopening
the GUI alone does not replace the service.
