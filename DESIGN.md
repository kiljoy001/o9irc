# Views and actors

The GUI will be a client of the application's 9P view. It reads presentation
data and writes commands as files. The network subsystem owns IRC parsing and
socket I/O; its actor owns the connection state and published view. The GUI can
restart without reconnecting the network actor.

The current vertical slice has two actors:

- `IrcNetwork` owns the IRC connection state and a `FileTree` registered as
  `o9irc.network.default`. It handles incoming lines and command requests.
- `IrcSupervise` owns dial, registration, the read loop, the ping watchdog,
  and teardown. It never exits while the service runs; `connect`,
  `disconnect`, and `reconnect` are flags it observes between attempts.
- `IrcViews` is the `ViewController`. It mounts the network tree into the
  application view and handles writes to the live input file.

The app posts one `/srv/ircfs` 9P service. The actors compose their trees
inside that service. No internal actor opens a separate listener.

A failed dial or a dropped socket moves the service to `reconnecting` and
schedules a new attempt with exponential backoff from one second to one
minute plus jitter. `o9ircmaxattempts` caps consecutive failures; zero means
unlimited. Reaching `001` clears the attempt count and rejoins channels whose
`joined` flag is still set. A watchdog sends `PING` after 60 seconds of
silence and closes the socket after 120 seconds without any server line. The
supervisor owns exactly one socket at a time and closes it before a redial.

```text
/srv/ircfs
  /view/network       write: add | remove | rename | select
  /view/networks.list read: id<TAB>name<TAB>address<TAB>status
  /view/selected      read/write: selected network id
  /view/networks/default/
    status    read: disconnected | connecting | connected | reconnecting | closed
    lasterror read: the most recent connection failure
    nextattempt read: Unix time of the next dial, or 0
    profile   read: host<TAB>port<TAB>nick<TAB>channel<TAB>full name
    events    read: recent raw IRC lines, oldest first
    statuslog read: recent server replies and events, oldest first
    conversations read: key<TAB>display name, one conversation per line
    knownusers read: nick<TAB>user<TAB>host<TAB>account<TAB>away, one per line
    queue     read: number of outbound frames waiting for the pacer
    channels/<casefolded-target-hex>/
      name, kind, topic, topicsetter, nicks, modes, transcript,
      historypath, historystatus
    users/<casefolded-target-hex>/
      name, kind, topic, topicsetter, nicks, modes, transcript,
      user, host, account, away, historypath, historystatus
    channel   read: selected channel
    topic, topicsetter, nicks, modes, transcript, historypath, historystatus
              legacy selected-channel aliases
    input     write: one command per write
    connect, disconnect write: control this network
```

`/view/networks/` also exposes `slot1` through `slot7` as fixed registry
slots. Unused slots are mounted but do not appear in `networks.list`; adding a
network configures the next free slot. This avoids changing a mounted
FileTree's directory list at runtime. `networks.list` and `selected` are live
files, so clients see adds, removes, and selection changes without remounting.
`/view/networks/default` is a permanent alias for the legacy network and is
never removable.

`nicks` is rendered from the live member set, with membership prefixes
attached (`@alice`, `+bob`, `@+carol`). JOIN, PART, QUIT, KICK, NICK, and
channel MODE updates rewrite it without requiring a rejoin. `modes` contains
the current channel mode letters, for example `+nt`. The internal member list
keeps the prefix set separate from the nick so mode changes and nick renames
do not rewrite the whole list. `topicsetter` records the last setter seen in
`TOPIC` or `333`. System events are also written to `transcript` as
`* alice joined` style lines; raw protocol lines remain in `events` and
`statuslog`. `knownusers` is updated from every `nick!user@host` prefix,
WHOIS replies, `AWAY`, `ACCOUNT`, and `CHGHOST`; existing query conversations
also expose the matching `user`, `host`, `account`, and `away` files.

`input` accepts `join`, `part`, `msg`, `notice`, `me`, `topic`, `mode`,
`kick`, `whois`, `away`, `invite`, `nick`, `say`, and `quit`. It also accepts
`profile<TAB>...` to save connection settings and `connect<TAB>...` to save
them and dial the server. User-visible commands are appended to an outbound
queue drained by a token-bucket pacer (`o9ircratelimit` lines per second,
default 2, and `o9ircrateburst`, default 5); `queue` shows the current depth.
PONG and registration bypass the queue. The profile is a private file in the
user's history
directory. The GUI opens Connection setup on a disconnected service and writes
these commands through the same 9P input file. A failed command
returns a write error. The `events` file keeps at most 32 KiB and is a snapshot;
clients that poll slowly can miss old lines. The network actor saves the
transcript for each conversation as a zlib snapshot under the user's history
directory and reloads it from a private index when the service starts. Its
limit applies to compressed bytes. Older complete lines are evicted when the
limit is reached. Outgoing message bodies are split into multiple frames at
UTF-8 boundaries when they exceed the 512-byte IRC limit. The conversation
count defaults to 256 and is read once from `o9ircmaxconversations`.
The GUI reads `networks.list`, follows `selected`, and rebinds its base path
to `networks/<id>` when the selection changes. The network menu offers next,
add, remove, connect, and disconnect; the status line shows the active network
name. Conversation scroll positions remain local to each GUI. Its selection
is local to each GUI, while the IRC data and commands stay in 9P. Monotonic
event IDs are a future addition for clients that need lossless polling.

The 9P boundary is the contract: a terminal renderer, Rio GUI, or remote
client can all consume the same views. UI event handlers only write to
`input`; they do not call `NetDialRaw` or parse IRC protocol lines.

The first Rio GUI in `gui.o9` follows mIRC's classic arrangement: network
tree on the left, topic and conversation in the center, nick list on the
right, and one input line along the bottom. The arrangement is based on
[mIRC's own interface image](https://www.mirc.com/about.html) and a
[full channel screenshot](https://winfuture.de/news,57155.html). The GUI uses
o9c's libdraw functions and reads only the 9P view described above.
It opens the existing `input` file through `uiio.o9`, because command writes
must not attempt to create a new file on the live 9P tree.
The toolbar and mouse button 3 open a native libdraw menu. Menu choices either
change the GUI's local view selection or write a command to the 9P `input` file.
The mouse wheel, PgUp/PgDn, and Home/End scroll the selected view.
