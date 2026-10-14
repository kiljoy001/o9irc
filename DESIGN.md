# Views and actors

The GUI will be a client of the application's 9P view. It reads presentation
data and writes commands as files. The network subsystem owns IRC parsing and
socket I/O; its actor owns the connection state and published view. The GUI can
restart without reconnecting the network actor.

The current vertical slice has two actors:

- `IrcNetwork` owns the IRC connection state and a `FileTree` registered as
  `o9irc.network.default`. It handles incoming lines and command requests.
- `IrcViews` is the `ViewController`. It mounts the network tree into the
  application view and handles writes to the live input file.

The app posts one `/srv/ircfs` 9P service. The actors compose their trees
inside that service. No internal actor opens a separate listener.

```text
/srv/ircfs
  /view/networks/default/
    status    read: disconnected | connecting | connected | closed
    profile   read: host<TAB>port<TAB>nick<TAB>channel<TAB>full name
    events    read: recent raw IRC lines, oldest first
    statuslog read: recent server replies and events, oldest first
    conversations read: key<TAB>display name, one conversation per line
    channels/<casefolded-target-hex>/
      name, kind, topic, nicks, transcript, historypath, historystatus
    users/<casefolded-target-hex>/
      name, kind, topic, nicks, transcript, historypath, historystatus
    channel   read: selected channel
    topic, nicks, transcript, historypath, historystatus
              legacy selected-channel aliases
    input     write: one command per write
```

`input` accepts `join`, `msg`, `nick`, `say`, and `quit`. It also accepts
`profile<TAB>...` to save connection settings and `connect<TAB>...` to save
them and dial the server. The profile is a private file in the user's history
directory. The GUI opens Connection setup on a disconnected service and writes
these commands through the same 9P input file. A failed command
returns a write error. The `events` file keeps at most 32 KiB and is a snapshot;
clients that poll slowly can miss old lines. The network actor saves the
transcript for each conversation as a zlib snapshot under the user's history
directory and reloads it from a private index when the service starts. Its
limit applies to compressed bytes. Older complete lines are evicted when the
limit is reached. The GUI reads the plain text `statuslog` and selected
conversation view, and keeps separate scroll positions for each. Its selection
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
