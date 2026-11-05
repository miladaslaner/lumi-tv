# Running the Lumi server on a Windows PC

Native Windows, no WSL or Docker. Placeholders: `<user>` (Windows account), `<pc-ip>`, `<lan-cidr>` (for example a /24), `<install-dir>` (for example `C:\Users\<user>\mytv`).

## 1. Install uv (per user)
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
& "$env:USERPROFILE\.local\bin\uv.exe" --version
```
uv installs Python 3.12 itself (`.python-version` names it); git is not needed on the PC.

## 2. Copy the code and configuration
Copy `lumi-tv\` to `<install-dir>\lumi-tv` (for example `tar` on the source machine plus `scp`; exclude `.venv`, `__pycache__`, `*.db`, `*.db-wal`, `*.db-shm`, `logs/`, `models/`, `backups/`, `claude-home/`, `lumitv.toml`). Copy `server\.env` (TMDB and server tokens) as a file; never print or commit it. Keep `<install-dir>` under the account's own profile, as suggested: `claude-home\mcp-*.json` holds the server token while a Claude Code process runs, and the owner-only file mode the server sets is a no-op on Windows, so the folder's ACL is the protection. In `<install-dir>\lumi-tv\server` create the config:
```powershell
Copy-Item lumitv.example.toml lumitv.toml
```
Then edit `lumitv.toml`: set `[open] user_agent_contact` to the household's email or a web page (Wikidata refuses
lookups without a contact, and Netflix picks then open Netflix's home screen). `lumitv doctor` fails until it is set.
When re-copying later, keep the PC's `.env` and `lumitv.toml`.

## 3. Install and test
```powershell
cd <install-dir>\lumi-tv\server
uv sync
uv run pytest -q
```

## 4. Firewall (LAN only, one rule)
Run in an elevated PowerShell. Use the profile your network is actually categorized as (`Get-NetConnectionProfile`); the remote range keeps it LAN-only.
```powershell
New-NetFirewallRule -DisplayName lumitv -Direction Inbound -Protocol TCP -LocalPort 8765 -Profile Public -RemoteAddress <lan-cidr> -Action Allow
```

## 5. Autostart
`deploy\start-lumitv.cmd` runs `uv run lumitv serve` from the server folder and appends output to `logs\service.log` (at start, a log over 5 MB becomes `logs\service.1.log`, replacing the previous one). Register it as a task that runs at startup without a stored password:
```powershell
$a = New-ScheduledTaskAction -Execute '<install-dir>\lumi-tv\server\deploy\start-lumitv.cmd'
$t = New-ScheduledTaskTrigger -AtStartup
$p = New-ScheduledTaskPrincipal -UserId '<user>' -LogonType S4U -RunLevel Limited
$s = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName lumitv -Action $a -Trigger $t -Principal $p -Settings $s
Start-ScheduledTask -TaskName lumitv
```

## 6. Verify
- On the PC: `Invoke-WebRequest http://127.0.0.1:8765/health` returns `ok`.
- From another LAN machine: `curl http://<pc-ip>:8765/health` returns `ok`.
- `uv run lumitv doctor` in the server folder (the TV check fails until the TV app is connected).

## Stop and restart
`Stop-ScheduledTask` ends the task but can leave the wrapper and the Python server running, and the scheduled task
restarts a server that exits (its `RestartCount`). Stop the wrapper tree first, then update dependencies, then start:
```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'start-lumitv' } | ForEach-Object { taskkill /PID $_.ProcessId /T /F }
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'lumitv' -and $_.CommandLine -match 'serve' } | ForEach-Object { taskkill /PID $_.ProcessId /T /F }
uv sync      # in the server folder; fails with "being used by another process" if the server still runs
Start-ScheduledTask -TaskName lumitv
Invoke-WebRequest http://127.0.0.1:8765/health -UseBasicParsing   # ok within ~20 s on an idle PC; see below
```
Check the matched command lines are under `<install-dir>` before killing anything. A start answers `ok` within about
20 s on an idle PC, but a memory-starved PC has taken up to 6 minutes (uv starts, Python appears minutes later):
poll `/health` rather than assume a hang. A restart also starts the `[ask] per_day` count over (it lives in memory).

## Backups
After each warm (at start-up and daily at `[cache] warm_at`, 17:00 in the example), the server copies the five
household tables (watch history, feedback, opened titles, open outcomes, viewers; not the TMDB cache) to
`backups\household-<date>.db` next to the database (one file per day; a later warm the same day replaces it) and
keeps the newest 14; then it prunes cache entries older than 45 days (and the oldest past 300,000). To restore: stop the server, then
`uv run python scripts/restore_household.py backups\household-<date>.db`, then start it. Leave `backups\` out of
code copies, like the database.

## Watchdog (restarts a server that runs but doesn't answer)
`deploy\watchdog.ps1` checks `/health` every 2 minutes; after 3 failures in a row it stops this install's server tree
(only processes whose command line names this install's folder; another install on the same PC is left alone) and
starts the `lumitv` task again, but never within 8 minutes of a start (a start can take ~6 minutes on a busy PC).
It writes only `logs\watchdog.log` and `logs\watchdog.state`. Register it once:
```powershell
$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -File "<install-dir>\lumi-tv\server\deploy\watchdog.ps1"'
$t = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 2)
$p = New-ScheduledTaskPrincipal -UserId '<user>' -LogonType S4U -RunLevel Limited
$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName lumitv-watchdog -Action $a -Trigger $t -Principal $p -Settings $s
```
Remove it with `Unregister-ScheduledTask -TaskName lumitv-watchdog -Confirm:$false`.

## Claude Code for asks from the TV
Asks typed or spoken on the TV are answered by the Claude Code CLI (`claude -p`) on the PC, logged in with the family's Claude subscription (no API key). Skip this section if `[ask] enabled = false`.

1. **Install natively, not in WSL** (as the Windows account that runs the `lumitv` task): `winget install Anthropic.ClaudeCode`, or the official script `irm https://claude.ai/install.ps1 | iex`. Check `claude --version`.
2. **Log in once, interactively:** run `claude` in a normal terminal as that account, sign in with the family's subscription, quit.
3. **Check it works under the task.** The server runs as an S4U task, which can lack the interactive login. Register a one-off task with the same principal as `lumitv` (section 5) running `cmd /c "echo Reply with ok | claude -p --output-format json > %USERPROFILE%\claude-check.json 2>&1"`, start it, read the file: pass = JSON with `"is_error": false` and `ok` in `result`. Then `Unregister-ScheduledTask` it and delete the file. If it fails with a login error, run `claude setup-token` once in a terminal and put `CLAUDE_CODE_OAUTH_TOKEN=<token>` in the server's `.env` (a file, never in chat or git; copy it with the clipboard rather than printing it). Re-run the check.
4. **`claude-home\`** (`[ask] workdir`, relative to the server folder) is the empty working directory Claude runs in. The server creates it; leave it empty. Claude is given only Lumi's MCP tools, and `--setting-sources local` keeps the machine user's own Claude settings, plugins and `CLAUDE.md` out of TV asks.
5. **What runs:** the server keeps one warm Claude Code process, plus one per active TV conversation (it starts a new one ahead of use). Idle processes use no CPU. Start-up was 10 to 25 s cold against about 2 s warm on the reference PC (8 cores, 30 GB), which is why they are kept warm.
6. **Deploy order:** stop the server tree (see Stop and restart), then `uv sync --extra speech` if PC speech-to-text is used (plain `uv sync` otherwise; every later `uv sync` needs the same flag or uv removes the extra), then start the task. Keep the PC's `.env` and `lumitv.toml`.

## Revert
```powershell
Stop-ScheduledTask -TaskName lumitv   # then stop the process tree as above
Unregister-ScheduledTask -TaskName lumitv -Confirm:$false
Remove-NetFirewallRule -DisplayName lumitv
Remove-Item -Recurse -Force <install-dir>
```
Optionally remove uv (`%USERPROFILE%\.local\bin\uv*.exe` and its PATH entry).

Claude Code (only if you set it up above): uninstall it as Anthropic's docs describe (`winget uninstall Anthropic.ClaudeCode`, or remove what the install script placed), and delete `%USERPROFILE%\.claude` only if nothing else on the machine uses it. On a shared machine that folder holds the other users' logins, settings and history; if unsure, leave it and just run `claude` then `/logout`. Also remove `CLAUDE_CODE_OAUTH_TOKEN` from `.env` and revoke the token in the Claude account settings.
