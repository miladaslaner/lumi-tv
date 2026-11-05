"""Replace SERVER_TOKEN in .env with a new random one. The token is never printed.

    uv run python scripts/rotate_token.py [--env .env]

The file is written next to itself and renamed over the old one, so a crash never leaves it half written; the
previous file stays as .env.bak (it still holds the old token: delete it once everything uses the new one).
Then, with the new token (read it from .env yourself):
  1. restart the server (deploy/windows.md: Start-ScheduledTask lumitv);
  2. send the TV the CONFIGURE broadcast with the new token (README, TV app setup);
  3. update the `Authorization: Bearer` header in Claude Desktop's `tv` entry and restart Claude Desktop.
Until all three are done, the old token stops working everywhere and the new one works only where it's set.
"""

import argparse
import os
import re
import secrets
import shutil
from pathlib import Path

TOKEN_BYTES = 32
# the assignment however it was written: `export SERVER_TOKEN = ...` counts too (python-dotenv reads both)
ASSIGNMENT = re.compile(r"^\s*(export\s+)?SERVER_TOKEN\s*=")


def rotate(env: Path) -> None:
    """Rewrites `env` with a fresh SERVER_TOKEN, keeping every other line (adding it when missing) and dropping a
    BOM an editor may have put in front. Atomic: written to <env>.tmp and renamed over `env`; the previous file is
    kept as <env>.bak."""
    text = env.read_text(encoding="utf-8").lstrip("﻿") if env.exists() else ""
    new = f"SERVER_TOKEN={secrets.token_urlsafe(TOKEN_BYTES)}"
    out = [new if ASSIGNMENT.match(ln) else ln for ln in text.splitlines()]
    if new not in out:
        out.append(new)
    tmp = env.with_name(env.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")  # no BOM: the PC reads it as UTF-8
    if env.exists():
        shutil.copymode(env, tmp)  # a .env kept private stays private
        shutil.copy2(env, env.with_name(env.name + ".bak"))
    os.replace(tmp, env)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env", type=Path, default=Path(".env"))
    args = p.parse_args()
    rotate(args.env)
    print(
        f"SERVER_TOKEN replaced in {args.env} (not shown); the previous file is {args.env.name}.bak. "
        "Next: restart, CONFIGURE the TV, update Claude Desktop."
    )


if __name__ == "__main__":
    main()
