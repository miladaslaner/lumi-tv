"""The maintenance scripts under scripts/ (loaded by path: they are not a package). Network-free parts only."""

import importlib.util
import os
import sqlite3
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parent.parent / "scripts"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---- rotate_token ----------------------------------------------------------------------------


def test_rotate_matches_the_assignment_however_it_is_written(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes("﻿TMDB_TOKEN=keep\nexport SERVER_TOKEN = old\nOTHER=1\n".encode())
    load("rotate_token").rotate(env)
    raw = env.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    lines = raw.decode("utf-8").splitlines()
    assert lines[0] == "TMDB_TOKEN=keep" and lines[2] == "OTHER=1" and len(lines) == 3
    assert lines[1].startswith("SERVER_TOKEN=") and "old" not in lines[1]


def test_rotate_leaves_a_commented_token_alone(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# SERVER_TOKEN=note\nSERVER_TOKEN=old\n", encoding="utf-8")
    load("rotate_token").rotate(env)
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# SERVER_TOKEN=note" and len(lines) == 2 and "old" not in lines[1]


def test_rotate_keeps_the_previous_file_and_leaves_no_temp(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SERVER_TOKEN=old\n", encoding="utf-8")
    env.chmod(0o600)
    load("rotate_token").rotate(env)
    assert (tmp_path / ".env.bak").read_text(encoding="utf-8") == "SERVER_TOKEN=old\n"
    assert not (tmp_path / ".env.tmp").exists()
    assert env.read_text(encoding="utf-8") != "SERVER_TOKEN=old\n"
    if os.name != "nt":  # NTFS has no mode bits; the file's ACL is inherited from the folder
        assert env.stat().st_mode & 0o777 == 0o600


def test_rotate_without_a_file_creates_one_and_no_backup(tmp_path):
    env = tmp_path / ".env"
    load("rotate_token").rotate(env)
    assert env.read_text(encoding="utf-8").startswith("SERVER_TOKEN=")
    assert not (tmp_path / ".env.bak").exists()


# ---- asks_report ------------------------------------------------------------------------------


def test_percentile_is_nearest_rank():
    m = load("asks_report")
    # rank ceil(0.95 * 20) = 19: the 19th value, where rounding 19.5 to even picked the 20th
    assert m.percentile(list(range(1, 21)), 0.95) == 19
    assert m.percentile([5], 0.5) == 5 and m.percentile([1, 2, 3, 4], 0.5) == 2


def test_targets_come_from_the_config_budgets(tmp_path, capsys):
    m = load("asks_report")
    toml = tmp_path / "lumitv.toml"
    toml.write_text("[budgets]\nask_posters_p50_ms = 9000\n", encoding="utf-8")
    assert m.read_budgets(toml) == (9000, m.DEFAULT_P95_MS)
    assert m.read_budgets(tmp_path / "missing.toml") == (m.DEFAULT_P50_MS, m.DEFAULT_P95_MS)
    assert "missing.toml" in capsys.readouterr().err
    line = m.target_line([9500] * 10, 9000, 15000)
    assert "p50 <= 9 s" in line and "MISSED" in line
    assert m.report([], 20, (9000, 15000)).endswith("target: no asks with poster timings")


# ---- list_providers ---------------------------------------------------------------------------


def test_list_providers_help_and_default_regions(capsys):
    m = load("list_providers")
    with pytest.raises(SystemExit) as e:
        m.main(["--help"])
    assert e.value.code == 0 and "REGION" in capsys.readouterr().out
    regions = m.default_regions()
    assert regions and all(len(r) == 2 and r.isupper() for r in regions)


# ---- fake_tv ----------------------------------------------------------------------------------


def test_fake_tv_state_reports_playing_known():
    m = load("fake_tv")
    assert m.state_message(True, False, False) == {
        "type": "state", "screen_on": True, "playing": False, "vpn": False, "playing_known": True,
    }  # fmt: skip
    unknown = m.state_message(True, False, True, playing_known=False)
    assert unknown["playing"] is True and unknown["playing_known"] is False


# ---- e2e_check --------------------------------------------------------------------------------


def test_e2e_check_helpers(tmp_path):
    m = load("e2e_check")
    with pytest.raises(m.E2EFailure, match="binge"):
        m.check(False, "1 binge: no titles")
    m.check(True, "fine")
    assert 1024 <= m.free_port() <= 65535

    src, dst = tmp_path / "lumitv.db", tmp_path / "copy.db"
    assert m.copy_database(src, dst) is False and not dst.exists()
    with sqlite3.connect(src) as c:
        c.execute("CREATE TABLE t (x)")
        c.execute("INSERT INTO t VALUES (1)")
    assert m.copy_database(src, dst) is True
    with sqlite3.connect(dst) as c:
        assert c.execute("SELECT x FROM t").fetchall() == [(1,)]
