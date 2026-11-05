import pytest

from lumitv import config


@pytest.fixture(autouse=True)
def _no_kids_hours(monkeypatch, request):
    """The example config turns kids mode on by default during the day ([kids] default_from/until), which would make
    every test's result depend on the time it runs. Tests marked `kids_hours` keep the real rule (and pass a clock)."""
    if "kids_hours" not in request.keywords:
        monkeypatch.setattr(config.Kids, "default_on", lambda self, now: False)
