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

    # Memory only: a test must never write the engine's real ledger file. And
    # no night rule, or every login test would fail when run before 08:00;
    # the rule has tests of its own on a ledger that keeps it.
    rule, days = LOGIN_LEDGER.first_automatic_login, LOGIN_LEDGER.trading_days_only
    LOGIN_LEDGER.bind(None)
    LOGIN_LEDGER.first_automatic_login = None
    LOGIN_LEDGER.trading_days_only = False
    yield
    LOGIN_LEDGER.bind(None)
    LOGIN_LEDGER.first_automatic_login = rule
    LOGIN_LEDGER.trading_days_only = days


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


@pytest.fixture(autouse=True)
def _no_exchange_archive(monkeypatch):
    """Tests never download the exchange's daily files.

    Every backtest reads the exchange's record for its range, downloading any
    day not yet cached -- so without this, an ordinary job test would reach the
    archive and depend on what the machine's cache happened to hold. A test of
    the exchange data patches in an archive of its own.
    """
    from engine.data import nse_bhavcopy

    monkeypatch.setattr(nse_bhavcopy, "shared_archive", lambda: None)
