"""Replay a settled campaign with other settings.

The campaign's own window -- from the moment it began to its expiry, on that
expiry alone -- through the same backtest engine every backtest uses: real
recorded prices, the live run's fill model, the charges in force then,
settlement against the official close. Run once as traded and once as
edited, so the difference between the two is the edit's alone; the as-traded
replay beside what the live run actually did shows how closely the replay
reproduces it.
"""

from __future__ import annotations

from typing import Any

from engine.backtest.jobs import BacktestJob, BacktestRunner

#: What a replay may change. Anything else in a request is ignored.
EDITABLE = (
    "step", "short_offset", "long_offset", "lots", "max_condors", "direction",
    "anchor_mode", "max_down", "max_up", "max_entry_vix", "min_entry_dte",
    "min_credit_ratio", "take_profit", "stop_loss", "trailing_sl", "trailing_sl_trigger",
    "campaign_stop_loss", "campaign_trailing_sl_pct", "campaign_trailing_sl",
    "campaign_trailing_sl_trigger",
    "dynamic_step", "dynamic_step_condors",
    "bar_minutes", "daily_loss_limit",
    "full_band_steps", "half_mode", "debit_shift", "max_put_spreads", "max_call_spreads",
)


def merged_settings(settings: dict[str, Any], overrides: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(settings)
    edits = overrides or {}
    for key, value in edits.items():
        if key in EDITABLE:
            out[key] = value
    # Keep UI alias keys ("stop_loss", "trailing_sl", "trailing_sl_trigger") and
    # campaign keys ("campaign_stop_loss", "campaign_trailing_sl_pct", etc.) in
    # sync when either is edited or cleared in the Playground.
    if "stop_loss" in edits and "campaign_stop_loss" not in edits:
        out["campaign_stop_loss"] = edits["stop_loss"]
    elif "campaign_stop_loss" in edits and "stop_loss" not in edits:
        out["stop_loss"] = edits["campaign_stop_loss"]

    if "trailing_sl" in edits and "campaign_trailing_sl_pct" not in edits:
        out["campaign_trailing_sl_pct"] = edits["trailing_sl"]
        out["campaign_trailing_sl"] = edits["trailing_sl"]
    elif "campaign_trailing_sl_pct" in edits and "trailing_sl" not in edits:
        out["trailing_sl"] = edits["campaign_trailing_sl_pct"]
        out["campaign_trailing_sl"] = edits["campaign_trailing_sl_pct"]

    if "trailing_sl_trigger" in edits and "campaign_trailing_sl_trigger" not in edits:
        out["campaign_trailing_sl_trigger"] = edits["trailing_sl_trigger"]
    elif "campaign_trailing_sl_trigger" in edits and "trailing_sl_trigger" not in edits:
        out["trailing_sl_trigger"] = edits["campaign_trailing_sl_trigger"]
    return out


def backtest_params(settings: dict[str, Any], campaign: dict[str, Any]) -> dict[str, Any]:
    """The backtest that replays `campaign` under `settings`."""
    bar = int(settings.get("bar_minutes") or 1)
    stop_loss = (
        settings["stop_loss"]
        if "stop_loss" in settings
        else settings.get("campaign_stop_loss")
    )
    campaign_stop_loss = (
        settings["campaign_stop_loss"]
        if settings.get("campaign_stop_loss") is not None or "campaign_stop_loss" in settings
        else settings.get("stop_loss")
    )
    trailing_sl_pct = (
        settings["campaign_trailing_sl_pct"]
        if settings.get("campaign_trailing_sl_pct") is not None
        else settings["campaign_trailing_sl"]
        if settings.get("campaign_trailing_sl") is not None
        else settings.get("trailing_sl")
    )
    trailing_trigger = (
        settings["campaign_trailing_sl_trigger"]
        if settings.get("campaign_trailing_sl_trigger") is not None
        else settings.get("trailing_sl_trigger")
    )
    params: dict[str, Any] = {
        "strategy": settings.get("strategy") or "ladder",
        "days": 1,                                   # superseded by the window
        "resolution": str(bar),
        "option_resolution": "1",
        "lots": int(settings.get("lots") or 1),
        "step": float(settings.get("step") or 100.0),
        "max_condors": int(settings.get("max_condors") or 20),
        "direction": settings.get("direction") or "down",
        "anchor_mode": settings.get("anchor_mode"),
        "max_down": settings.get("max_down"),
        "max_up": settings.get("max_up"),
        "max_entry_vix": settings.get("max_entry_vix"),
        "min_entry_dte": settings.get("min_entry_dte"),
        "min_credit_ratio": settings.get("min_credit_ratio"),
        "take_profit": settings.get("take_profit"),
        "stop_loss": stop_loss,
        "campaign_stop_loss": campaign_stop_loss,
        "campaign_trailing_sl_pct": trailing_sl_pct,
        "trailing_sl": settings.get("trailing_sl"),
        "trailing_sl_trigger": trailing_trigger,
        "campaign_trailing_sl_trigger": trailing_trigger,
        "dynamic_step": bool(settings.get("dynamic_step", False)),
        "dynamic_step_condors": int(settings.get("dynamic_step_condors") or 5),
        "short_offset": settings.get("short_offset"),
        "long_offset": settings.get("long_offset"),
        "expiry_cadence": settings.get("expiry_cadence") or "monthly",
        # The live run's daily loss limit, applied as it applies it.
        "daily_loss_limit": settings.get("daily_loss_limit"),
        "roll": False,
        "window": {"start_at": campaign["started_at"], "expiry": campaign["expiry"]},
    }
    if params["strategy"] == "hic":
        for key in ("full_band_steps", "half_mode", "debit_shift", "max_put_spreads", "max_call_spreads"):
            if settings.get(key) is not None:
                params[key] = settings[key]
    return params


def summarise(dataset: dict[str, Any]) -> dict[str, Any]:
    """What the comparison shows, from a backtest's full dataset."""
    m = dataset["metrics"]
    equity = dataset.get("equity") or []
    stride = max(1, len(equity) // 400)
    curve = [{"ts": p["ts"], "equity": p["equity"], "spot": p.get("spot")} for p in equity[::stride]]
    if equity and (not curve or curve[-1]["ts"] != equity[-1]["ts"]):
        curve.append({"ts": equity[-1]["ts"], "equity": equity[-1]["equity"], "spot": equity[-1].get("spot")})
    return {
        "metrics": {key: m.get(key) for key in (
            "net_pnl", "total_pnl", "open_positions", "open_pnl", "condors", "wins", "losses",
            "max_drawdown", "capital_at_risk", "total_credit", "total_costs", "best", "worst",
        )},
        "positions": [
            {
                "level": c["level"], "side": c.get("side"), "entry_time": c["entry_time"],
                "credit": c["credit"], "max_loss": c["max_loss"], "pnl": c["pnl"],
                "status": c["status"], "exit_reason": c.get("exit_reason"),
            }
            for c in dataset.get("condors") or []
        ],
        "equity": curve,
        "real_fraction": (dataset.get("provenance") or {}).get("provider", {}).get("real_fraction"),
        "warnings": dataset.get("warnings") or [],
        "skipped": len(dataset.get("skipped") or []),
    }


def run_replay(market, user_id: str, settings: dict[str, Any], campaign: dict[str, Any], progress) -> dict[str, Any]:
    params = backtest_params(settings, campaign)
    job = BacktestJob(job_id="playground", user_id=user_id, params=params)

    class _Watch(BacktestRunner):
        def _step(self, stage: str, value: float, message: str = "") -> None:
            super()._step(stage, value, message)
            progress(value, message)

    _Watch(market, job).run()
    if job.status != "done" or job.result is None:
        raise RuntimeError(job.error or "The replay did not finish.")
    return {"settings": settings, **summarise(job.result)}
