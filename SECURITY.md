# Security

## Supported versions

1.0.x, the current release. Older household builds (server 0.1.0, app 0.2.0) are not supported: upgrade.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository (the Security tab, "Report a vulnerability"), not a
public issue: the product runs in people's homes with their children's ages and viewing history. One maintainer,
spare time; expect an acknowledgement within a week.

## Threat model

Lumi is a home-LAN product: one server on a machine you own, one TV app, one shared secret. It is built on these
assumptions, and the code enforces what it can.

- **The LAN is trusted enough, the internet is not.** The server listens on the LAN (`[server] host`), speaks plain
  HTTP and WebSocket (no TLS: cleartext by design, the TV app allows it for `ws://`) and must never be exposed to
  the internet. Anyone who can sniff the LAN can read the token; anyone with the token can use the tools and the
  asks. There is no user model: everyone with the token is the household.
- **One bearer token guards everything except `GET /health`.** `/health` is unauthenticated on purpose (the
  watchdog's probe) and returns the literal `ok`, no version, nothing else. Every other route, the MCP endpoint
  included, requires `Authorization: Bearer <SERVER_TOKEN>`, compared in constant time. The token is read from that
  header only: a `?token=` in a URL is ignored, and a defence-in-depth log filter masks `token=` query values in
  uvicorn's lines anyway. On the WebSocket the handshake completes (HTTP 101) before the check, because the check
  reads the request's headers; a bad token then closes the socket with 4401. Rotation: `scripts/rotate_token.py`,
  then restart the server, re-send CONFIGURE to the TV and update Claude Desktop.
- **Token at rest, in plaintext, in three places.** On the TV in the app's private preferences (backups are off,
  the release build is not debuggable, so other apps and `run-as` cannot read it); on the server in `.env`
  (`.env.bak` after a rotation holds the previous one); and, while a Claude Code process runs, in
  `claude-home/mcp-*.json`, written readable by the owner only on POSIX. On Windows that file mode is a no-op and the
  folder's ACL is the protection: keep the install under the account's own profile (`deploy/windows.md`).
- **Configuration of the TV app only over adb.** The CONFIGURE receiver requires the `DUMP` permission, which only
  the shell holds, so no app on the TV can repoint Lumi at another server or read its settings.
- **Exported Android components, and why each is exported.** `AnswerActivity` (the launcher entry);
  `AssistActivity` (the assistant role's entry for the remote's mic button; it does nothing unless Lumi holds the
  role, and while it does, any app on the TV can fire `ACTION_ASSIST` at it, which pauses playback and opens the ask
  screen: a nuisance, not a breach); `LumiVoiceService` and `LumiSessionService` (bound by the system only,
  `BIND_VOICE_INTERACTION`); `LumiRecognitionService` (a stub that always refuses; the assistant role makes Android
  point `voice_recognition_service` at it, see the README's trade-off); `PlaybackWatcher` (bound by the system only,
  `BIND_NOTIFICATION_LISTENER_SERVICE`; it reads media-session state and never notification content); `ConfigReceiver`
  (`DUMP`). `LinkService` and `BootReceiver` are not exported.
- **What Claude Code may do.** Asks from the TV run `claude -p` with only Lumi's five MCP tools (`--tools ""`,
  `--allowedTools mcp__lumi`, `--strict-mcp-config`, `--setting-sources local`), in an empty working directory,
  with `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `TMDB_TOKEN` and `SERVER_TOKEN` removed from its environment. It
  cannot run commands, read files or start playback: launches are built server-side from the toml, and Claude passes
  a pick token and a service name, nothing more. Claude Code keeps its own session history under the server user's
  `~/.claude`.
- **TMDB and Wikidata text is untrusted input.** Titles, overviews and keywords come from public databases that
  anyone can edit. The server strips control and invisible characters and links from them and tells Claude to treat
  them as data. What the code bounds regardless of what the text or Claude says: in kids mode every candidate is
  filtered by certificate before Claude sees it, unrated or unmapped titles never qualify, a certificate is the
  strictest of the home country's entries cross-checked with the fallback country, the youngest named child's age
  cannot be raised by Claude, `kids_age` is capped at `[kids] max_age`, picks are re-checked when shown or opened, and
  nothing can start playback. What it does not bound: outside kids mode (or during the kids hours when the question
  says the answer is for grown-ups and no child is named), an injected overview can steer which titles Claude
  shows. The blast radius is posters and Claude's wording on the TV, and `open_title` landing on a title page.
- **Ask quotas.** `/ask` and `/ask/audio` are capped per minute and per day (`[ask] per_minute`, `per_day`; HTTP
  429), so a client with the token cannot loop the Claude subscription dry. The per-day count lives in memory and
  starts over when the server restarts.
- **What is logged.** The event log replaces the configured secrets with `***` and drops keys named `token` or
  `authorization`; it holds questions, transcripts, titles and timings, never audio; viewers are logged as counts,
  never names. The TV's logcat carries ask ids, states and timings, never text or the token. `/stats` exposes
  timings, cache size, TV flags and the last ask's outcome: no names, ages, questions or paths.
- **Microphone.** Recorded only after a press, only while the answer screen is in front, never by the background
  service; capped at `[speech] max_audio_s`; held in memory, zeroed after sending, never written to disk. With the
  TV's own recognizer, speech goes to that recognizer's provider, not to Lumi.

## Out of scope

Physical access to the TV or the server machine, a compromised home router, and the security of TMDB, Wikidata,
Claude Desktop and Claude Code themselves.
