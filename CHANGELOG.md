# Changelog

All notable changes to Lumi TV. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the
server package and the TV app share one version ([Semantic Versioning](https://semver.org/)).

## [Unreleased]

## [1.0.0] - 2026-11-05

First public release, after a whole-product audit of the household builds (server 0.1.0, app 0.2.0). Compared with
them:

### Security

- Kids mode: a title's certificate is the strictest of the home country's TMDB entries, cross-checked with the
  fallback country's mapped certificate (the stricter wins). Love Actually and La La Land no longer pass as "AL".
- `[kids] max_age` (15): an age above it is treated as 15, so 16+ and 18+ titles never appear in kids mode.
- A child named by the TV keeps the kids limit even when the household table cannot be read (fails closed, logged).
- TMDB's adult titles are dropped (`include_adult=false`, `adult` rows skipped).
- Ask quota: refused, duplicate and cancelled asks no longer count against `per_day`.
- The Claude Code OAuth token is masked in the event log like the other secrets.
- Release builds sign with your own key (`tv/keystore.properties` or the `LUMI_*` variables); the debug-key
  fallback is explicit and logged.

### Fixed

- Kids mode works with any rating scheme: `[kids] ages` maps labels to ages; the old code crashed on labels that
  were not numbers.
- The daily warm also warms kids-mode searches, so daytime answers are fast.
- A TMDB outage no longer reads as "nothing matched": results carry `degraded` and Claude is told.
- TMDB 429 is retried once honoring `Retry-After`; lookups are bounded by one semaphore.
- One malformed pick no longer drops a whole answer or marks the TV offline.
- `hello` says `speech: true` only once the speech model has loaded (and is re-sent when it does), so the first
  spoken ask after a restart works.
- Viewer names in `/ask/audio?viewers=` are trimmed.
- TV: a rejected token or a server that is down is told apart from "not configured"; the status dot follows the
  link; error copy no longer offers a dead "Try again" for a logged-out, limited or missing Claude Code.
- TV: the "other services" hint renders as one key cap; titles get two lines, reasons three; length labels do not
  break across lines; the remove marker uses a glyph the bundled font has.
- TV: the first OK on an empty household does something; names keep their spelling; BACK on the keyboard keeps the
  typed text; household edits survive a link outage.
- TV: `lintRelease` passes.
- Server loggers are configured (INFO visible, WARNINGs with timestamps); the config refuses zero or negative
  timeouts, bad ports and a zero `lookup_concurrency`.
- The store keeps one long-lived SQLite connection instead of opening and committing per call.
- Scripts: `e2e_check.py` runs on its own port, a copy of the database and a temporary log, on any OS;
  `rotate_token.py` rewrites `.env` atomically and keeps `.env.bak`; `asks_report.py` reads its targets from
  `[budgets]`; `list_providers.py` has `--help`; `bench.py` runs the server's keyword rule; `fake_tv.py` reports
  `playing_known`.

### Changed

- Version 1.0.0 for the server and the app; `protocol.md` states the compatibility.
- `hello` carries `limits`: the TV takes its ask deadlines, question length and audio cap from the server.
- Strings: `{assistant}` and `{vpn}` placeholders filled by the server (`[assistant] name`, `[assistant] vpn_name`);
  no brand names in shipped copy; family-facing error copy never mentions the server, Claude or signing in.
- Package visibility by intent: adding a service in the toml needs no app rebuild.
- The example config is labelled an example household; tests use neutral names.
- `check.sh` is portable (JDK detection, online by default, lint and format checks, `--e2e`).
- Cold "like" budget 3000 ms, as measured.
- `quality_check.py` is a regression snapshot with a stricter gate (`--fresh`, "never" lists).

### Added

- CI (GitHub Actions: server lint, format and tests; TV tests and lint; Gradle wrapper validation), Dependabot,
  ruff and pyright configuration, `.editorconfig`.
- `SECURITY.md`, `CONTRIBUTING.md`, this changelog; the README rewritten for a stranger (requirements, a 5-minute
  path without a TV, architecture, the MCP tools, kids mode, who's watching, release signing).
- `[kids] ages`, `[kids] max_age`, `[assistant] vpn_name`.
- Tests for the kids rule on every path (open, hours, excluded ids, window edges), the 429 body, the daily backup,
  the toddler boundary, the scripts.

[Unreleased]: https://github.com/miladaslaner/lumi-tv/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/miladaslaner/lumi-tv/releases/tag/v1.0.0
