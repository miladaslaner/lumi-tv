# Contributing

A small project with one maintainer. Issues and pull requests are welcome; a short issue before a large change
saves both of us time. There is no CLA: by sending a change you agree that it is released under the repository's
MIT license.

## Set up

Server (`server/`):

```sh
cd server
uv sync --group dev                 # Python 3.12 (uv fetches it), the server, pytest, ruff, pyright
uv sync --group dev --extra speech  # also faster-whisper, for the speech-to-text code
cp lumitv.example.toml lumitv.toml && cp .env.example .env   # to run it; tests need neither
```

TV app (`tv/`): JDK 17 and the Android SDK with platform 34 (`sdk.dir` in `tv/local.properties`, or `ANDROID_HOME`).
`./gradlew assembleRelease` builds the APK; the README's "TV app setup" installs it.

## Check before you push

```sh
./check.sh            # server: ruff check, ruff format --check, pytest; TV: unit tests and lintRelease
./check.sh --server   # one half only; --tv the other
./check.sh --quality  # scored picks against real TMDB (needs TMDB_TOKEN in server/.env and the network)
./check.sh --e2e      # a throwaway server, the fake TV and an MCP client against real TMDB
```

CI runs the first line on every push and pull request. Without a TV, `scripts/fake_tv.py` stands in for one
(it prints every message and acks answers) and `scripts/e2e_check.py` plays a whole conversation; see the README.

## Style

- Python: ruff, configured in `server/pyproject.toml` (120 columns, `E F I B UP SIM RUF`). Run `uv run ruff format .`
  and `uv run ruff check .` before committing. `uv run pyright` (basic mode, `src/`) is advisory.
- Kotlin: the official style, 120 columns, lint clean (`./gradlew lintRelease`).
- Comments say why, not what; no narration of the obvious; no TODOs in committed code (open an issue instead).
- Nothing personal or household-specific in the tree: names, addresses, tokens, device ids. Tests use neutral names.

## Tests are the proof

- Server tests never touch the network (`respx` for HTTP) and never depend on wall-clock budgets; assert outcomes
  with hang guards. Every behavior change comes with the test that would have caught the old one.
- TV tests are JVM unit tests (Robolectric where Android is needed); keep them fast.
- Never delete or weaken a test to make a change pass; if a test pinned wrong behavior, fix it and say so in the
  commit message.
- Anything only a real TV can show (grants, launches, overlays) is verified on a TV before it is called done, and
  the pull request says which device.

## Commits and pull requests

Conventional commits with the half as scope: `feat(server): …`, `fix(tv): …`, `test(server): …`, `docs: …`,
`build: …`, `chore: …`. Subject in the imperative, body says why. Small commits, one concern each.

A change to the wire format updates `protocol.md` in the same commit, and the compatibility note if an older app
or server would notice. User-visible changes go under "Unreleased" in `CHANGELOG.md`.

## Dependencies

`uv.lock` is the pin: CI installs with `uv sync --frozen`, so a dependency change is a lock change too. Bump one
with `uv lock --upgrade-package <name>` (or everything with `uv lock --upgrade`) and commit the lock. Gradle
versions live in `tv/app/build.gradle.kts` and `tv/build.gradle.kts`. Dependabot proposes updates weekly for both,
and for the CI actions. New dependencies need a reason in the pull request.

## Versions

The server package (`server/pyproject.toml`) and the app (`tv/app/build.gradle.kts`: `versionName`, and
`versionCode` goes up by one) carry the same version and are bumped together for a release, with the changelog
section and a tag `v<version>`.
