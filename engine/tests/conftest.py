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
