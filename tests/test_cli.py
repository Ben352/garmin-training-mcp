from typing import ClassVar

import pytest
from garminconnect import GarminConnectAuthenticationError

from garmin_mcp import cli


class FakeGarmin:
    """Stands in for garminconnect.Garmin. `token_ok` decides whether the cached
    token works; `password_ok` whether a credential login succeeds."""

    token_ok = True
    password_ok = True
    instances: ClassVar[list["FakeGarmin"]] = []

    def __init__(self, email=None, password=None, prompt_mfa=None):
        self.email, self.password = email, password
        FakeGarmin.instances.append(self)

    def login(self, tokenstore):
        if self.password is None:
            if not FakeGarmin.token_ok:
                raise GarminConnectAuthenticationError(
                    "Username and password are required"
                )
        elif not FakeGarmin.password_ok:
            raise GarminConnectAuthenticationError("401 Unauthorized")


@pytest.fixture
def fake(monkeypatch, tmp_path):
    FakeGarmin.token_ok, FakeGarmin.password_ok, FakeGarmin.instances = True, True, []
    monkeypatch.setattr(cli, "Garmin", FakeGarmin)
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("GARMINTOKENS", str(tmp_path / "tokens"))
    monkeypatch.delenv("GARMIN_SYNTHETIC", raising=False)
    monkeypatch.delenv("GARMIN_PASSWORD", raising=False)
    monkeypatch.setenv("GARMIN_EMAIL", "runner@example.com")
    prompts = []
    monkeypatch.setattr(cli.getpass, "getpass", lambda p: prompts.append(p) or "pw")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    return prompts


def test_valid_token_needs_no_password(fake):
    cli.login()
    assert fake == []
    assert [g.password for g in FakeGarmin.instances] == [None]


def test_expired_token_asks_for_password_once(fake):
    FakeGarmin.token_ok = False
    cli.login()
    assert fake == ["Garmin password: "]
    _token_try, password_login = FakeGarmin.instances
    assert password_login.email == "runner@example.com"
    assert password_login.password == "pw"


def test_wrong_password_exits_without_retrying(fake):
    FakeGarmin.token_ok = FakeGarmin.password_ok = False
    with pytest.raises(SystemExit, match="Not retried"):
        cli.login()
    assert len(fake) == 1


def test_no_terminal_never_prompts(fake, monkeypatch):
    FakeGarmin.token_ok = False
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit, match="in a terminal once to log in"):
        cli.login()
    assert fake == []


def test_password_env_var_is_ignored(fake, monkeypatch, capsys):
    FakeGarmin.token_ok = False
    monkeypatch.setenv("GARMIN_PASSWORD", "from-env")
    cli.login()
    assert FakeGarmin.instances[-1].password == "pw"  # typed, not from env
    assert "GARMIN_PASSWORD is ignored" in capsys.readouterr().err


def test_email_prompted_when_not_in_env(fake, monkeypatch):
    FakeGarmin.token_ok = False
    monkeypatch.delenv("GARMIN_EMAIL")
    monkeypatch.setattr("builtins.input", lambda p: "typed@example.com")
    cli.login()
    assert FakeGarmin.instances[-1].email == "typed@example.com"
