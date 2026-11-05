# Lumi TV protocol

WebSocket at `/tv` with `Authorization: Bearer <server_token>`. JSON, one object per message, `type` selects the
message. The server reads the token from that header only; a token in the URL is ignored and the socket then closes
like any bad token. The handshake completes before the check (the check needs the request's headers), after which a
wrong or missing token closes the socket with code **4401**: do not retry without fixing the token.
Only one TV connection is live; a new connection replaces (and closes, code 4000) the old one.

## Compatibility

Server 1.0.0 and app 1.0.0 speak this protocol. Fields marked `?` are optional: a newer server sends them to an
older app, which ignores what it does not know, and an older server does not send them. An app from before
`launches` reads the legacy `launch` chain (below). An app that gets no `limits` in `hello` uses its built-in
numbers (55 s / 65 s ask deadlines, 200 characters, 15 s of audio); one that gets no `speech` uses its own
recognizer; one that gets no `strings` key keeps its built-in English text for it.

| Direction | `type` | Fields |
|---|---|---|
| server→TV | `hello` | `assistant_name`, `theme` (design tokens, `content/theme.json`), `strings` (UI copy for the configured language, `content/strings.<code>.json`, placeholders filled; see Strings), `speech?: bool` (the server transcribes spoken questions now: `[speech]` engine configured and installed, model loaded; absent, from older servers, is false), `viewers` (who watches this TV, `[{name, age?}]`, may be empty), `kids_hours: {from, until}` (`"HH:MM"` each, both `null` when the config sets no default hours; the TV's "who's watching" default: everyone inside those hours, the grown-ups outside), `limits?: {ask_timeout_s, max_question_chars, max_audio_s, speech_timeout_s}` (from `[ask] timeout_s`, `[ask] max_question_chars`, `[speech] max_audio_s`, `[speech] timeout_s`; the TV's typed-ask deadline is `ask_timeout_s + 10` s, its voice deadline `ask_timeout_s + speech_timeout_s + 10` s, its keyboard stops at `max_question_chars` (clamped to 1..1000) and its mic at `max_audio_s`). Sent first on every connect. |
| TV→server | `state` | `screen_on: bool`, `playing: bool`, `vpn: bool`, `playing_known?: bool` (default true). Send on connect and on every change. `playing` means someone is watching: a media session is playing, buffering, or was paused in the last 20 minutes. `playing_known: false` means the TV cannot read playback state (notification listener not bound); it then also sends `playing: true`, and the server treats it as playing. |
| server→TV | `show` | `answer_id`, `question`, `headline`, `subline`, `picks: [{token, title, poster_url?, service, service_label, length_label?, reason?, launches, launch, search_hint?, providers}]` |
| TV→server | `shown` | `answer_id`, `mode: "screen" \| "notice" \| "tv_off"`. `tv_off`: the screen is off, nothing was shown (the TV is not woken). Must arrive within the ack timeout (5 s) or the server treats the TV as offline; the TV sends no `shown` when it cannot draw the answer at all. |
| server→TV | `open` | `title`, `launches`, `launch`, `service_label` (name of the app, for messages), `search_hint?` |
| TV→server | `opened` | `token` (user pressed OK on a poster), `service?` (the service it opened in) |
| TV→server | `open_failed` | `token`, `service` (no launch step started) |
| TV→server | `viewers_set` | `viewers: [{name, age?}]` (who watches this TV; `age` for children; at most 12, names up to 30 characters) |
| server→TV | `viewers` | `viewers` as saved (names unique; answers `viewers_set`) |
| TV→server | `play_check` | `token`, `service`, `played` (anything playing in that app within 90 s of opening; Netflix previews count) |
| server→TV | `looking` | `turn_id`, `question`. Show the "Looking for…" screen; ignore while playing. No `show` may follow (the turn can fail server-side), so the TV drops it after 30 s. |
| TV→server | `ask_warm` | `ask_id`. Sent when the mic opens: the server runs its speech model once (quiet noise) so a model the PC paged out is in memory when the clip arrives; logged as `stt_warm` (`ms`). |
| TV→server | `rendered` | `answer_id`, `ms` (time from receiving `show` to posters drawn), optional `phases`: ms from receiving `show` to `start` (answer screen asked to start), `created` (it was created), `frame` (its first frame); each only when it happened for this answer. When the answer waited behind the "Answer ready · press OK" notice, the clock restarts at that OK: `ms` leaves out the wait and `phases` has no `start`. |

`length_label` is "1h 52m" (or "45m", "2h") for a film. For a series it is the whole run, "≈ 9h", or, when the
user asked for a length and only the first seasons fit, that span: "S1 ≈ 6h" / "S1–2 ≈ 13h". Absent when unknown.

`search_hint` (the title) is set for services configured with `search_hint = true`: their launch lands on the app's
search screen but cannot fill it in. When the step that opened has `hint: true` (that search link, not Google TV's
page or the plain app), the TV shows the `search_hint` string for a few seconds, without taking key focus from the app.
`providers` lists every service the title is on, best first, each `{service, service_label, launches, launch,
search_hint?}`; the pick's own `service`…`search_hint` fields are the first one's (OK opens it). Best = the service
the user asked for, then one that did not lately fail to open or play the title (two failed opens, or three opens
that never played, in the last 14 days put a service last), then one the family opened or watched it on, then one
that opens the exact title, then one that works with the TV's VPN as last reported, then config order. On the TV,
▼ opens an "Open in" row of the others.
`reason` (≤ 48 chars, optional) is why Lumi picked the title, drawn under its poster. The answer screen ends with a
"More like these" tile; OK on it sends a typed ask (`more_question` from the strings) about the answer on screen.
`poster_url` may be null. The server does not send `show`/`open`/`looking` while the last `state` has
`screen_on: false` (and not `looking` while `playing: true` or `playing_known: false`). If the TV disconnects
after reporting `screen_on: false` (standby cuts its network), `show_on_tv`/`open_title` answer `tv_off`, not
`offline`.

## Asking from the TV

The TV can ask Lumi itself (mic button or typing); the server answers with `claude -p` and the same tools Claude
Desktop uses. Everything above is unchanged: these are new message types and optional fields, and answers that
come from Claude Desktop never carry `ask_id`.

| Direction | `type` / route | Fields |
|---|---|---|
| TV→server HTTP | `POST /ask` (bearer) | JSON `ask_id` (`^[A-Za-z0-9_-]{8,40}$`, made by the TV), `text` (≤ `[ask] max_question_chars`, default 200, the number `hello.limits` carries), `source: "voice"\|"typed"`, `answer_id?`, `focused_token?`, `viewers?: [names]` (who is watching, as chosen on the TV; names the household does not know are ignored; at most 12) → `202 {"ask_id"}`; 400 `{"error":"invalid json"}` or `{"error":"invalid","field":"<name>"}`; 401; 413 `{"error":"too large"}` (body over 8 KB); 429 `{"error":"limit","cap":"per_minute"\|"per_day"}` (`[ask]` caps; the TV shows its `ask_limit` line); 503 `{"error":"disabled"}` |
| TV→server HTTP | `POST /ask/audio?ask_id=&answer_id=&focused_token=&viewers=` (bearer) | body: the spoken question as WAV (`Content-Type: audio/wav`, 16 kHz mono 16-bit PCM, ≤ `[speech] max_audio_s` (15 s), ≤ 1 MB); `ask_id` as for `/ask`, `answer_id` / `focused_token` optional query parameters, `viewers` the names watching joined with commas (a name must not contain a comma; the TV refuses one when a person is added); source is `voice` → `202 {"ask_id"}`, then the same messages as `/ask` (`heard` once transcribed, or `ask_error not_heard`); 400 `{"error":"invalid","field":"ask_id"}`; 401; 413 too large; 415 not 16 kHz mono 16-bit WAV; 429 as for `/ask`; 503 speech-to-text off (`[speech] engine = "none"`). The audio is held in memory only and never logged |
| server→TV | `heard` | `ask_id`, `text` (what Lumi heard; the TV shows "Looking for “text”") |
| server→TV | `preview` | `ask_id`, `posters: [{title, poster_url?}]` (the likely picks as soon as `find_titles` has them; the Looking screen shows them without focus until `show` replaces them) |
| server→TV | `ask_error` | `ask_id`, `code`: `not_heard` \| `failed` \| `timeout` \| `login` (Claude Code logged out) \| `limit` (usage limit) \| `not_installed` (no Claude Code at `[ask] claude_path`). The TV shows a line per code (`ask_<code>` in the strings file; unknown codes read as `failed`) |
| server→TV | `ask_reply` | `ask_id`, `text` (≤ 140; Claude answered without picks) |
| server→TV | `show`, `looking` | + optional `ask_id` (the answer to that press) |
| TV→server | `ask_cancel` | `ask_id` (BACK or a newer press) |
| TV→server | `ask_timing` | `ask_id`, `listen_ms?`, `heard_ms?`, `posters_ms?`, `typed_posters_ms?` (TV clock: press → listening drawn; end of speech → Looking drawn; end of speech → posters drawn; typed submit → posters drawn) |

`answer_id` and `focused_token` name the answer on screen and the focused poster when the user asked, so a
follow-up ("something lighter", "not that one") is seeded with those titles; the server remembers the last 20
answers it showed, and an unknown `answer_id` or `focused_token` is ignored. `looking` with an `ask_id` is sent even
while `playing` is true (the user just pressed the mic, so Lumi's screen is in front), but still not while
`screen_on` is false. `heard`, `ask_error` and `ask_reply` are fire and forget; they are dropped while no TV is
connected. The TV ignores any of these whose `ask_id` is not its newest ask. `ask_timing` values are non-negative
integers in milliseconds; a message with a bad value is ignored as a whole.

`POST /ask` returns at once; everything else about the ask arrives over the TV link. The newest ask wins: a new
`POST /ask` stops the running ask and nothing more of the older one is sent. An `ask_cancel` that arrives before its
`POST /ask` (the POST still in flight) is remembered for 60 s, and that ask is then accepted (202) but never run. A
repeated `POST /ask` with an `ask_id` already taken is accepted and ignored. A spoken question (`source: "voice"`, and
every `/ask/audio` transcript) that is noise (fewer than three letters once `[…]`/`(…)` tags and punctuation are gone)
or one of `[speech] ignore_phrases` (Whisper's "Thank you." on silence) gets `ask_error not_heard` without asking
Claude. A typed question is never dropped as noise: anything with a letter or digit reaches Claude. A child among
`viewers` sets the kids limit on the server (the youngest's age), whatever Claude passes.

`POST /ask/audio` answers `202` as soon as the body is checked (auth, size, WAV format and length), before it is
transcribed; the TV's upload timeout is 10 s, as for `/ask`. The transcript then goes the way of a `/ask` text
(`heard` or `ask_error not_heard`); a transcription that fails or takes longer than `[speech] timeout_s` gets
`ask_error failed`. It shares the newest-ask-wins rule, the early `ask_cancel` and the repeated `ask_id` handling
with `POST /ask`. Bodies: 503 `{"error":"disabled"}` (asks off) or `{"error":"no speech-to-text"}`, 413
`{"error":"too large"}` (over 1 MB) or `{"error":"too long"}` (over `max_audio_s`), 415 `{"error":"not audio/wav"}`
(content type) or `{"error":"not 16 kHz mono 16-bit wav"}`. The TV shows a 503 to its audio as "can't hear speech"
(`ask_no_speech`), not as a failed answer.

**Which voice route the TV uses.** Unless `CONFIGURE --es voice …` sets one, a mic press records the remote's mic and
sends the audio here (`remote_mic`) when the last `hello` said `speech: true`, and uses the TV's own recognizer
otherwise (also while not connected, and with servers that don't send `speech`). `--es voice auto` forgets a set
route.

**Playback.** A mic press while a video plays pauses it. BACK on Lumi's screen ends the ask and plays the video
again; leaving Lumi any other way (HOME, another app, the screensaver, the screen turning off) or opening a pick ends
the ask (`ask_cancel` while the server still works on it) and leaves the video paused.

**`/stats` `ask.last`.** `{outcome, age_s}` of the last ask that ran to an end (cancelled or superseded asks don't
count; `null` before the first one; forgotten on restart). Outcomes: `shown`, `reply`, `not_heard`, the `ask_error`
codes, `stt_failed`, `stt_timeout`. `lumitv doctor` fails when it is `login`, `limit` or `not_installed` and under 6 h
old.

## Launch

```
Launch = {package, action, data?, extras?: {str: str}, component?, vpn: "on"|"off"|"any", hint: bool,
          relaunch_after_s: [number]}
launches = [Launch, ...]   // ordered, never empty
```

Built server-side so deep links can change without rebuilding the app (templates live in `lumitv.toml`). `launches`
is the ordered list of ways to open the title; the TV tries them first to last and stops at the first one that
starts (a step whose app is missing or refuses is skipped; last resort is the plain launch of the last step's app):

1. Google TV's own title page, `VIEW https://tv.google.com/asset/<URL-encoded Google id>` in Google TV's launcher
   (`[open]` in the toml), for services with `google_tv = true` when the title's Google Knowledge Graph id is known.
2. The service's own `launch` template filled with the title (e.g. Prime's search link, Netflix's title link). Left
   out when it needs a per-title id the server could not resolve (`{netflix_id}`).
3. The plain app launch.

The TV starts an Android intent from `package`/`action`/`data`/`extras`; `component` ("package/.Activity") makes it
explicit to that activity. `action` `android.intent.action.MAIN` with only a package means "launch the app"
(leanback launcher first). Other actions are sent with the package set. `hint: true` marks the step after which the
search hint is shown. `relaunch_after_s` (e.g. Netflix `[10]`): an app that drops a link on a cold start (profile picker first) gets
the same step again once, after the first of these seconds (later values are ignored: a second re-send could pull
the user out of a video), but only if that app had no activity in the last hour, and not if its own last activity
event is a pause/stop (the user left it) or Lumi came back. Playback state is not checked (a cold-starting Netflix
reports PLAYING on its profile picker). The TV then shows `profile_relaunch` with the title. It needs usage access (an
appop granted over adb); without it the TV never re-sends, shows `profile_retry` and keeps the answer so OK can be
pressed again.

`launch` is the same list as one Launch nested through `fallback` (each step's `fallback` is the next), for TV apps
from before `launches`; such an app shows the search hint after the first step. New apps read `launches`.

`vpn` is the VPN state the service works with (on every step, Google TV's page included: the title plays in the
service's app). It never blocks: the title always launches. If the TV's current VPN state differs, the TV also shows
a warning pill (`needs_vpn_on` / `needs_vpn_off` strings, `{service}` replaced by `service_label`), joined to the
search hint when both apply. The server answers `open_title` with `sent` and, from the last `state`, an optional
`vpn_warning` for Claude to mention. The TV decides its own warning from its current VPN state, so the server's
`vpn_warning` is best effort and can be off by one stale `state`.

## Strings

`hello.strings` carries every key in `content/strings.<code>.json` (the shipped file is `strings.en.json`; its keys
are the contract, and the TV keeps a built-in English text for any key a server does not send). Placeholders: the TV
fills `{service}` (a service label) and `{title}` (in `search_hint` and `profile_relaunch`); the server fills
`{assistant}` (`[assistant] name`) and `{vpn}` (`[assistant] vpn_name`, default "the VPN") in every value, the items
of the `hints` list included, before sending `hello`. The hints use the `key: label` form ("OK: open"): the TV draws
the part before the colon as a key cap. The app's built-in defaults say "Lumi" and "the VPN".
