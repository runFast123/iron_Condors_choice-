"""Bring a stopped forward run back so the next login resumes it.

Needed because a run could be marked stopped for reasons the user never chose:
a session ending, an engine restart before sessions were durable, a superseded
login. Those runs still hold open positions and a ladder mid-flight, and until
now the only way back was editing the database by hand.

Deliberately conservative:

* It refuses a run the user actually stopped, unless told otherwise. "Stopped
  by user" is a decision, not an accident.
* It refuses when the user already has a different run going, because only one
  is ever resumed and silently picking between them would lose the other.
* It shows what it is about to do and asks, unless run with --yes.

    python -m engine.tools.restore_run --list
    python -m engine.tools.restore_run --vendor M09984
    python -m engine.tools.restore_run --session 646a33474ba8 --yes
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

from engine.store.db import LADDER, STRATEGIES, Store


def _load_env_file() -> None:
    """Read .env.engine.local, as the supervisor does.

    Without this the tool reports "no shared secret" on an engine that has one,
    because the secret lives in a file the supervisor loads rather than in this
    process's environment -- a warning that would send someone chasing a
    problem they do not have.
    """
    path = pathlib.Path(__file__).resolve().parents[2] / ".env.engine.local"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        os.environ.setdefault(name.strip(), value.strip().strip("\"'"))

# Reasons a run stopped through no decision of the user's. Matched as prefixes,
# because these are written as sentences -- "superseded by a newer run; not
# resumed after the engine restarted" is the same reason as "superseded", and
# exact matching quietly refused to restore exactly the runs that most needed
# it.
RESUMABLE_PREFIXES = (
    "session ended",
    "superseded",
    "engine error",
    "retired so an earlier run",
)


def _is_resumable(reason: str | None) -> bool:
    """Whether this run stopped for a reason the user did not choose."""
    if not reason:
        return True
    return reason.strip().lower().startswith(RESUMABLE_PREFIXES)


def _describe(row: dict) -> str:
    state = json.loads(row["state_json"]) if row.get("state_json") else {}
    condors = state.get("condors") or []
    open_condors = [c for c in condors if c.get("status") == "OPEN"]
    ladder = state.get("ladder") or {}
    lines = [
        f"  session   : {row['session_id']}",
        f"  user      : {row['user_id']}",
        f"  strategy  : {row['strategy_id'] or LADDER}",
        f"  status    : {row['status']}"
        + (f"  ({row['stopped_reason']})" if row.get("stopped_reason") else ""),
        f"  started   : {row['started_at'][:19]}",
        f"  last tick : {state.get('last_tick', '--')}",
        f"  expiry    : {state.get('expiry')}",
        f"  condors   : {len(condors)} ({len(open_condors)} open)"
        f"   fills {len(state.get('fills') or [])}",
        f"  ladder    : anchor {ladder.get('anchor')} fired {ladder.get('fired_levels')}",
        f"  realised  : {state.get('realised')}",
    ]
    for condor in open_condors:
        legs = ", ".join(
            f"{leg['side'][0]}{leg['right']} {leg['strike']:.0f}@{leg['entry_price']:.2f}"
            for leg in (condor.get("legs") or [])
        )
        lines.append(f"    open condor {condor.get('level'):,.0f}: {legs}")
    return "\n".join(lines)


def _rows(store: Store) -> list[dict]:
    return [dict(r) for r in store._rows("SELECT * FROM forward_sessions ORDER BY started_at")]


def _vendor_to_user(store: Store, vendor: str) -> str | None:
    """Find a user id from the vendor id on their signed-in session."""
    for row in store._rows("SELECT user_id, vendor_id FROM auth_sessions"):
        if (row["vendor_id"] or "").strip().upper() == vendor.strip().upper():
            return row["user_id"]
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="show every run and exit")
    parser.add_argument("--session", help="session id, or a unique prefix of one")
    parser.add_argument("--vendor", help="restore the newest resumable run for this vendor id")
    parser.add_argument("--user", help="restore the newest resumable run for this user id")
    parser.add_argument("--yes", action="store_true", help="do not ask")
    parser.add_argument(
        "--strategy",
        choices=STRATEGIES,
        help="narrow a --vendor/--user search to one strategy",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="restore even a run the user stopped deliberately",
    )
    args = parser.parse_args(argv)

    _load_env_file()
    store = Store()
    try:
        rows = _rows(store)
        if args.list or not (args.session or args.vendor or args.user):
            print(f"{len(rows)} run(s):\n")
            for row in rows:
                print(_describe(row))
                print()
            if not args.list:
                print("Nothing selected. Pass --session, --vendor or --user.")
            return 0

        user_id = args.user
        if args.vendor and not user_id:
            user_id = _vendor_to_user(store, args.vendor)
            if user_id is None:
                print(f"No signed-in session carries vendor id {args.vendor!r}.")
                print("Vendor ids are only recorded for sessions, so the user must")
                print("have logged in at least once since sessions became durable.")
                return 1
            print(f"vendor {args.vendor} -> user {user_id}\n")

        if args.session:
            matches = [r for r in rows if r["session_id"].startswith(args.session)]
        else:
            matches = [
                r
                for r in rows
                if r["user_id"] == user_id
                and r["status"] != "running"
                and (args.strategy is None or (r["strategy_id"] or LADDER) == args.strategy)
                and (args.force or _is_resumable(r.get("stopped_reason")))
            ]
            matches = matches[-1:]

        if not matches:
            print("Nothing matched. Run with --list to see what is stored.")
            return 1
        if len(matches) > 1:
            print(f"{args.session!r} matches {len(matches)} runs; be more specific.")
            return 1

        target = matches[0]
        if target["status"] == "running":
            print("That run is already marked running. Nothing to do.")
            return 0

        reason = target.get("stopped_reason")
        if not _is_resumable(reason) and not args.force:
            print(f"This run was stopped deliberately ({reason!r}).")
            print("Restoring it would reopen positions the user chose to close.")
            print("Pass --force if that is genuinely what you want.")
            return 1

        # Same user *and* same strategy. Without the second half, restoring an
        # old ladder run would retire this user's live run of another strategy
        # -- a different book with its own open positions, which has nothing to
        # do with the one being restored.
        target_strategy = target["strategy_id"] or LADDER
        live = [
            r for r in rows
            if r["user_id"] == target["user_id"]
            and r["status"] == "running"
            and (r["strategy_id"] or LADDER) == target_strategy
        ]

        print("About to restore:\n")
        print(_describe(target))
        if live:
            print("\nThis user already has a run of that strategy marked running:\n")
            for row in live:
                print(_describe(row))
            print("\nOnly one run is ever resumed, so that one will be retired.")
        print()

        if not args.yes:
            reply = input("Proceed? [y/N] ").strip().lower()
            if reply not in ("y", "yes"):
                print("Cancelled; nothing was changed.")
                return 1

        for row in live:
            store.mark_stopped(
                row["session_id"], "retired so an earlier run could be restored"
            )
            print(f"  retired {row['session_id'][:12]}")

        # Clear the stop, keeping everything else exactly as it was.
        state = json.loads(target["state_json"])
        state["stopped_reason"] = None
        store.save_forward(
            session_id=target["session_id"],
            user_id=target["user_id"],
            status="running",
            started_at=target["started_at"],
            stopped_reason=None,
            state=state,
            strategy_id=target_strategy,
        )
        print(f"  restored {target['session_id'][:12]}")
        print()
        print("It resumes the next time that user signs in -- the engine needs")
        print("their Choice credentials to fetch quotes, and those are never stored.")
        # Read the environment, not engine_config: that singleton is built at
        # import time, before the env file above is loaded, so it would always
        # report the secret missing.
        if not os.environ.get("ENGINE_SHARED_SECRET"):
            print()
            print("Note: ENGINE_SHARED_SECRET is not set, so sessions do not survive")
            print("      restarts and this can happen again.")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
