# o9irc roadmap

This plan turns the current single-network, single-socket client into a
multi-network IRC client with correct channel state, reconnection, IRCv3
authentication, durable history, and a GUI that can carry several networks.
It is ordered by risk and user-visible value, not by the order the gaps appear
in the audit.

Each phase is independently shippable. Later phases assume the earlier ones,
so the order should not be rearranged without re-checking the dependencies in
the phase headers.

## Current shape

- One `IrcNetwork` actor owns one socket, one profile, one history index, and a
  `FileTree` registered as `o9irc.network.default` (`ircfs.o9:367`).
- `IrcViews` mounts exactly one network at `/view/networks/default`
  (`ircfs.o9:1180`).
- `IrcPump` is a blocking read loop; EOF calls `closed()` and stops
  (`ircfs.o9:704`, `ircfs.o9:1127`).
- The protocol layer knows registration, PING, PRIVMSG, TOPIC/332, NAMES/353,
  and NICK. Everything else is appended to `statuslog` as raw text
  (`ircfs.o9:815`).
- History is a zlib snapshot per server and conversation with oldest-quarter
  eviction (`history.o9`).
- The GUI reads one view subtree and keeps selection, scroll, and search-less
  state locally (`gui.o9:1557`).

## Principles

1. The 9P view is the contract. New state must be readable from 9P before the
   GUI depends on it.
2. Keep the terminal client and the service on the same wire functions in
   `protocol.o9`. A feature added to one frontend should not fork the parser.
3. Never send secrets over plain TCP. SASL and server PASS require TLS.
4. Preserve old profiles, old history files, and `/networks/default` for
   existing installs; migrate instead of breaking.
5. Every phase lands with tests. The existing QEMU harness can run a fake IRC
   server, so new protocol work gets an end-to-end test, not only a host unit
   test.

## Phase summary

| Phase | Theme | Priority | Est. | Depends on |
|---|---|---|---|---|
| 1 | Protocol state and command surface | P0 | 4-6 d | - |
| 2 | Connection lifecycle and reconnect | P0 | 2-3 d | 1 (rejoin state) |
| 3 | Multi-network service and GUI | P1 | 6-9 d | 1, 2 |
| 4 | IRCv3 CAP/tags and SASL | P1 | 4-6 d | 2 |
| 5 | Event IDs, timestamps, unread, search | P2 | 5-7 d | 1, 3, 4 |
| 6 | GUI polish | P2 | 4-6 d | 1, 3, 5 |
| 7 | DCC file transfer and chat | P3 | 5+ d | 3, 6 |

Estimates are working days for one developer familiar with o9c and 9front, and
include tests and documentation but not review latency.

### Status

Phase 1 is implemented. `state.o9` owns the member-prefix, mode,
message-split, CTCP, known-user, rate-limit, and conversation-limit helpers.
`ircfs.o9` keeps per-conversation member, mode, topic-setter, joined, and
away state and maintains it across JOIN/PART/QUIT/KICK/NICK/MODE/TOPIC,
renders system events into the transcript, answers WHOIS/INVITE/NOTICE/AWAY,
and accepts the new commands. `nicks`, `modes`, `topicsetter`, `knownusers`,
and `queue` are exposed through 9P, and query conversations expose `user`,
`host`, `account`, and `away`. An outbound queue drained by a token-bucket
pacer enforces `o9ircratelimit`/`o9ircrateburst` without blocking command
writes. Host tests cover the helpers and the QEMU fake server exercises the
state transitions and commands.

Phase 2 is implemented: `IrcSupervise` owns dial, registration, the read loop,
the watchdog, and teardown; `status`/`lasterror`/`nextattempt` expose the
lifecycle; backoff runs from one second to one minute with jitter and an
optional attempt cap (`o9ircmaxattempts`); `connect`, `disconnect`, and
`reconnect` control it. Reaching `001` resets backoff and rejoins the live
channel set. The watchdog pings after 60 seconds of silence and drops a link
after 120 seconds without data.

---

## Phase 1 - Protocol state and command surface (P0)

**Goal:** channel and user state stays correct during normal IRC use, and the
command set covers everyday actions. This is the foundation for rejoin after
reconnect and for multi-network, so it comes first.

### State model

Replace the flat `Dict<string,string>` blobs with per-conversation state:

- `IrcChannel`: name, topic, topic setter, modes, member map
  (nick -> prefix set), joined flag.
- `IrcUser`: nick, user, host, account, away, last seen; one entry per known
  nick, with channels derived from channel membership.
- `Dict<string,IrcChannel>` and `Dict<string,IrcUser>`, keyed by the existing
  casefolded hex key from `IrcViewKeyRaw`.

The 9P files stay compatible: `nicks` renders members with prefixes
(`@nick`, `+nick`), `topic` stays plain text. Add `modes` and `members` only if
a client needs them; do not replace existing files.

### Incoming handling

Handle and maintain state for:

- `JOIN`, `PART`, `QUIT`, `KICK`, `MODE` (channel and user modes), `TOPIC` and
  `333`, `NOTICE`, `INVITE`, `AWAY`/`305`/`306`.
- `353` with multi-prefix names, terminated by `366`; clear the member set at
  `366` rather than appending forever.
- `433`/`437` during and after registration, `ERROR`, `005`.
- WHOIS replies (`301`, `311`-`319`, `330`) rendered into the status or query
  conversation instead of only raw `statuslog`.

System events (joins, parts, quits, kicks, mode changes) become lines in the
conversation transcript with a distinct `*`/`--` form, not just raw server
text. Raw lines remain available in `events` and `statuslog`.

### Outgoing commands

Extend `IrcNetwork.command` and the terminal client's `/` parser with:

`part`, `topic`, `mode`, `kick`, `whois`, `away`, `notice`, `me` (CTCP
`ACTION`), and `invite`.

Rules:

- Validate targets against `IrcValidTargetRaw` and reject CR/LF (already done by
  `IrcSend`).
- Split outgoing `PRIVMSG`/`NOTICE` bodies on UTF-8 boundaries so each frame is
  at most 510 bytes including the IRC prefix; send multiple frames.
- Add a token-bucket limiter (default roughly 2 lines/s, burst 5) with a
  per-network override. Queue, do not drop, and surface queue depth in
  `status`.
- Keep command writes synchronous from the 9P caller's perspective: return
  failure if the line cannot be queued.

### 64-conversation limit

Make the limit configurable (`o9ircmaxconversations`, default 256, hard cap
1024) and reserve slots for open queries. Evict nothing implicitly; on
overflow return a clear write error and status line.

### Tests

- Extend the fake server in `test/test_ircfs_qemu.py` to send JOIN/PART/QUIT/
  KICK/MODE/NOTICE/TOPIC and assert the `nicks`, `topic`, and `transcript`
  files.
- Extend `test/host_logic.o9` with parser/state tests: prefix stripping,
  multi-prefix NAMES, mode parsing, message splitting at UTF-8 boundaries,
  rate limiter accounting.
- Keep `test/check_task_lifetime.py` green; new parsers must not leak tasks.

**Exit criteria:** a channel's nick list reflects joins, parts, quits, kicks,
and nick changes without a rejoin; `/me`, `/notice`, `/part`, `/topic`,
`/mode`, `/kick`, and `/whois` work from both frontends; oversized messages
are split; existing tests still pass.

---

## Phase 2 - Connection lifecycle and reconnect (P0)

**Goal:** a dropped connection recovers on its own and the service can start
disconnected without a restart.

### Work

- Replace the blocking `IrcPump`/`IrcStart` split with one supervisor that owns
  dial, registration, read loop, and teardown.
- Status values: `disconnected`, `connecting`, `connected`, `reconnecting`,
  `closed`; add `lasterror` and `nextattempt` files.
- Reconnect with exponential backoff from 1 s to 60 s plus jitter, capped at a
  configurable attempt count (default unlimited, `0` disables). Reset the
  delay after a successful `001`.
- On `001`, re-register and rejoin the desired channel set (from phase 1
  state). Do not replay sent messages automatically; keep the outbound queue
  only for messages not yet written to the socket.
- Ping timeout: send `PING` every 60 s when idle and treat 120 s of silence as
  a dead link. This also detects half-open TCP.
- Commands: `disconnect`, `connect`, `reconnect`, and profile `connect` while
  disconnected. A launcher without arguments boots `disconnected` and shows
  Connection setup.
- Clean shutdown must stop the supervisor and close the socket exactly once.

### Tests

- Fake server accepts, sends `001`, then closes; assert status transitions,
  backoff values, and re-registration (NICK/USER/JOIN).
- Server that never answers PING; assert timeout and reconnect.
- Manual `disconnect` during `reconnecting` stops the loop.
- Memory test still passes after several reconnect cycles.

**Exit criteria:** killing the server and restarting it recovers the session
without touching the service; no reconnect storm; no duplicate sockets.

---

## Phase 3 - Multi-network service and GUI (P1)

**Goal:** one service hosts several networks, each with its own socket,
profile, history, and view. This is the headline gap and the largest change.

### Decision to confirm

Use one service with a network registry, not one service per network. One
service keeps a single mount point, a single GUI, and a single history index
per network. The registry approach also lets the GUI add or remove networks
without remounting.

### Service

- Add `IrcNetworkRegistry` (an actor) that owns `Dict<string,IrcNetwork>` and
  allocates stable ids (slug from host, e.g. `libera`, `oftc`, with `-2` on
  collision).
- `IrcNetwork` loses the global `files.register("o9irc.network.default")`;
  each instance gets its own `FileTree` and a per-network history directory
  (`$home/lib/o9irc/networks/<id>/`).
- Profiles move to `$home/lib/o9irc/networks/<id>/connection`. Migrate the
  existing `$home/lib/o9irc/connection` into network `default` on first start.
- Views become:

  ```text
  /view/networks/
    default/                 # alias for the first network, kept for old GUIs
    <id>/
      status, lasterror, profile, events, statuslog, conversations
      channels/<key>/...
      users/<key>/...
      input
    /view/networks/<id>/...  # selection is per network
  ```

- New control files: `networks` (id<TAB>display name<TAB>address<TAB>status),
  `networks/<id>/connect`, `networks/<id>/disconnect`, and a top-level
  `network` input that accepts `add`, `remove`, `rename`, `select`.
- Removing a network closes its socket, stops its supervisor, keeps its history
  on disk, and removes it from the view after clients have seen the update.

### Concurrency risk

`FileTree` mutation while a client has the tree mounted is the main unknown.
Before writing the registry, add a small spike test: mount a view, add and
remove a `networks/<id>` subtree, and confirm that `ls`, `read`, and `walk`
behave on 9front. If live subtree removal is unsafe, the fallback is to keep
the directory entry and mark the network `removed`/`disabled` until the
service restarts.

### GUI

- Left tree gains a network root per network, with status, channels, and
  queries underneath.
- Selection is `(network id, conversation key)`; per-network scroll positions.
- Toolbar `Network` menu: Add, Edit, Connect, Disconnect, Remove.
- Connection setup is reused per network, prefilled from that network's
  profile.
- The GUI resolves its base path from `networks` instead of a hard-coded
  `networks/default` (`gui.o9:1557`).

### Tests

- Extend `test/mock_view.o9` with two networks and assert the GUI can switch
  between them.
- QEMU test with two fake servers on different ports: both connect, messages
  stay isolated, histories are separate, restart restores both.
- Migration test: old `connection` file becomes network `default`; old
  `/networks/default` paths still read.
- Concurrency spike test from above becomes a regression test.

**Exit criteria:** add two networks, chat on both, restart the service, and
resume both with history; removing one network does not disturb the other.

---

## Phase 4 - IRCv3 and SASL (P1)

**Goal:** join networks that require SASL and render modern server metadata.

### CAP and SASL

- Send `CAP LS 302` before `NICK`/`USER`; request `server-time`,
  `message-tags`, `batch`, `multi-prefix`, `userhost-in-names`,
  `away-notify`, `account-notify`, `chghost`, and `invite-notify`; send
  `CAP END` when negotiation finishes or fails.
- SASL PLAIN: `AUTHENTICATE PLAIN`, base64 `authzid\0authcid\0passwd`, handle
  `900`/`903` success and `904`-`907` failures. Add SASL EXTERNAL only if the
  TLS layer can present a client certificate.
- Profile schema gains `sasl` and `password` fields. Passwords live in a
  separate 0600 file, never in the profile line that the GUI displays, and are
  refused when transport is `tcp`.
- Optional server `PASS` follows the same rule.

### Tags and batches

- Parse the IRCv3 tag section in `IrcPayload` (already skipped) into a map:
  `time`, `msgid`, `account`, `batch`, `label`.
- Use `time` for transcript timestamps when present; fall back to local time.
- Track `BATCH +id type` / `BATCH -id` and render `netjoin`/`netsplit` groups
  as a single summary line instead of N joins/quits.
- Preserve unknown tags for `events` so raw inspection still works.

### Tests

- Fake server that advertises CAP and requires SASL: success and each failure
  code; assert no registration before `CAP END`.
- Plain server with no CAP: unchanged behavior.
- Tag escaping (`\:` -> `;`, `\s` -> space) and `server-time` ordering.
- Batch grouping and netjoin rendering.

**Exit criteria:** authenticate to a SASL-only network over TLS; transcripts
carry server timestamps and msgids; non-IRCv3 servers are unaffected.

---

## Phase 5 - Event IDs, timestamps, unread, and search (P2)

**Goal:** lossless polling, durable server timestamps, read state, and search.

### Event stream

- Give every published event a monotonic `uint64` id per service, plus an
  `epoch` value for the process lifetime.
- `events` becomes a bounded snapshot of lines prefixed with `id<TAB>`, and new
  files `events/base` and `events/next` expose the range.
- A client remembers `(epoch, last id)`. If the next id is still inside
  `[base, next)`, it reads only new lines; if it fell behind, it reloads the
  selected transcript and marks a gap. This preserves the existing snapshot
  semantics without pretending 9P has query parameters.
- Persist per-conversation lines with server time, msgid, and sender; dedupe
  by msgid on reconnect.

### Read state and selection

- Per conversation: `unread` (count), `lastread` (event id), and a writable
  `read` file that accepts an event id or `all`.
- Persist read state and the selected conversation per network under the
  network's history directory, so a GUI restart restores both.
- Unread counts exclude the selected conversation while its window is focused.

### Search

- Start with GUI-side search over the loaded transcript (no service change):
  case-insensitive substring, next/previous match, scroll to hit.
- If search must span evicted or unloaded history, add a service-side `search`
  control pair: write `term` to `search`, read newline-separated hits from
  `searchresults`. Keep this out of phase 5 unless the GUI-side version is
  insufficient.

### Tests

- Event ids strictly increase; snapshot base advances only with eviction.
- Cursor resume returns exactly the missed lines; gap detection after eviction.
- Unread counts and read marks survive restart; msgid dedupe after reconnect.
- Search finds wrapped and multi-byte text.

**Exit criteria:** a slow client can resume without missing lines; unread
badges are accurate across restarts; search works in the GUI.

---

## Phase 6 - GUI polish (P2)

**Goal:** the GUI feels like a usable daily client.

- **Tab completion:** nicknames in the active channel (prefix-aware), then
  channels, then commands. Repeated Tab cycles candidates; completion inserts
  `nick: ` at the start of a line.
- **Input history:** per-conversation, Up/Down, persisted with the GUI's local
  state or the service's selected-conversation file.
- **Nick colors:** deterministic hash into a 16-color palette with contrast
  checking; own nick distinct; mentions highlighted.
- **Notifications:** unread badges in the tree and switchbar, a window-title
  counter, and an optional bell. Investigate a Rio `wctl` notification hook;
  fall back to bell plus badge if no stable API exists.
- **Small fixes:** scroll-to-bottom marker, "new messages" divider, channel
  mode display, topic setter display.

### Tests

- Host-side completion and history tests in `test/host_logic.o9`.
- Mouse test covers badges, completion popup, and notification-free operation.
- Memory test still passes with completion state and colors.

**Exit criteria:** typing `ni<Tab>` completes a nick, Up recalls input, unread
badges track the 9P read state, and no test grows memory.

---

## Phase 7 - DCC (P3, optional)

DCC is a separate project, not a checkbox:

- `DCC SEND`/`ACCEPT`/`RESUME` with explicit user consent, a download
  directory, size limits, and no auto-accept.
- `DCC CHAT` as a second socket type in the supervisor.
- Expose transfers as 9P files (`transfers/<id>/name`, `size`, `progress`,
  `accept`, `cancel`) so the GUI and terminal can share the implementation.
- Tests need a second fake peer; keep this out of the main IRC test loop.

---

## Cross-cutting work

- **Test harness:** factor the fake IRC server into a reusable module used by
  the client, service, and reconnect tests. Add flags for CAP/SASL, drops, and
  delayed replies.
- **Memory and lifetime:** every phase runs `O9IRC_MEMORY_ONLY=1` and
  `test/check_task_lifetime.py`. New supervisors must have one owner for each
  socket and one close per task.
- **Docs:** update `DESIGN.md` for the view layout and `README.md` for new
  commands, environment variables, and profile fields in the same commit as
  the feature.
- **Compatibility:** keep `/networks/default`, five-field profiles, and old
  history files readable until the next major version.

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| `FileTree` cannot add/remove mounted subtrees live | Blocks multi-network view | Spike first; fall back to fixed slots marked disabled |
| o9c task/string lifetime bugs under reconnect | Crashes or leaks | One supervisor owner, `check_task_lifetime`, memory test per phase |
| SASL secret handling | Credential leak | Separate 0600 file, TLS-only, never echo in profile/status |
| History migration | Lost logs | Read old paths first, write new paths, keep old files |
| GUI rewrite scope | Phase 6 stalls | Ship completion/history/colors before notifications |
| 9P polling overhead with many networks | GUI latency | Per-network event cursors, avoid full transcript rereads |

## Decisions needed before phase 3

1. Confirm one service with a registry (recommended) versus one service per
   network.
2. Keep `/networks/default` as a permanent alias or only during migration.
3. Whether network removal keeps history indefinitely or moves it to a trash
   directory.
4. Whether SASL credentials use a private file (recommended, matches the
   existing trust-file model) or 9front factotum.

## Immediate next step

Start phase 1 in this order:

1. Add the per-conversation state types and NAMES/JOIN/PART/QUIT/KICK/MODE
   handling with host tests.
2. Add the new commands and `/me`, with QEMU coverage.
3. Add message splitting and the rate limiter.
4. Update `README.md` and `DESIGN.md`, run the full existing suite.
