import importlib.util
from pathlib import Path


def load():
    path = Path(__file__).parent.parent / "scripts" / "rotate_token.py"
    spec = importlib.util.spec_from_file_location("rotate_token", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rotate_replaces_only_the_server_token(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TMDB_TOKEN=keep-me\nSERVER_TOKEN=old\nOTHER=1\n", encoding="utf-8")
    load().rotate(env)
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "TMDB_TOKEN=keep-me" and lines[2] == "OTHER=1"
    new = lines[1].split("=", 1)[1]
    assert lines[1].startswith("SERVER_TOKEN=") and new != "old" and len(new) >= 40
    assert not env.read_bytes().startswith(b"\xef\xbb\xbf")


def test_rotate_adds_a_missing_token(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TMDB_TOKEN=x\n", encoding="utf-8")
    load().rotate(env)
    assert env.read_text(encoding="utf-8").splitlines()[1].startswith("SERVER_TOKEN=")
