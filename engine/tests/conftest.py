"""Shared test setup."""

import pytest


@pytest.fixture(autouse=True)
def _fresh_login_ledger():
    """The Choice login ledger is process-wide by design -- it is what stops a
    loop of logins across session objects -- so each test starts it empty."""
    from engine.choice.session import LOGIN_LEDGER

    LOGIN_LEDGER.forget()
    yield
    LOGIN_LEDGER.forget()


@pytest.fixture(autouse=True)
def _no_backup_source(monkeypatch):
    """Tests never reach the backup source.

    A backtest asks it about any day or option leg its market did not serve,
    and the fake markets here serve almost nothing -- so if the machine
    running the tests had backup credentials in its environment, ordinary job
    tests would go to the network and pass or fail on what came back. A test
    of the backup patches in a fake client of its own.
    """
    from engine.data import groww

    monkeypatch.setattr(groww, "shared_backup", lambda: None)
