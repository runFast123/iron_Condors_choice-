"""Shared test setup."""

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _test_log_file(tmp_path_factory):
    """Keep the test suite out of the engine's operational log.

    Any test that starts the API app runs its lifespan, which installs the
    file handler -- and it went to engine/state/logs/api.log, the file an
    operator reads during an incident. A simulated "OSError: disk full" and a
    fake VIX pause from the suite sat among the live engine's own lines, which
    is exactly the kind of entry that sends someone chasing a problem that
    does not exist. Pointed at a temporary file before the first test runs.
    """
    previous = os.environ.get("ENGINE_LOG")
    os.environ["ENGINE_LOG"] = str(tmp_path_factory.mktemp("logs") / "api.log")
    yield
    if previous is None:
        os.environ.pop("ENGINE_LOG", None)
    else:
        os.environ["ENGINE_LOG"] = previous


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
