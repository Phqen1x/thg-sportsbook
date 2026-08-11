"""Merge the orphaned guild-0 DB (sportsbook_0.db) into a real guild's DB.

Before the component-interaction guild-context fix, every button/select/modal
action fell back to guild 0 and wrote to ``data/sportsbook_0.db`` instead of the
caller's per-guild DB. This recovers that data.

Only the user-scoped tables are migrated. Of those, the cleanly recoverable one
is ``users`` (chip balances + lifetime stats), keyed by ``discord_id``. The
``bets`` / ``parlays`` / ``pending_parlay_legs`` rows carry ``market_id`` /
``tribute_id`` foreign keys into per-DB game state whose autoincrement IDs do NOT
line up across databases, so migrating them would create dangling references —
they are reported and skipped by default. ``betting_restrictions`` of type 'ALL'
(no FK) can be carried with --restrictions.

Dry-run by default: prints a full diff and changes nothing. Re-run with --apply.

Container usage (from /app, the WORKDIR):

    # dry run — see exactly what would change
    docker compose exec bot python -m tools.migrate_guild0 --target 793600464570548254

    # apply, taking the guild-0 chip balance where a user exists in both DBs
    docker compose exec bot python -m tools.migrate_guild0 --target 793600464570548254 \
        --balance source --apply

A timestamped backup of the target DB is written next to it before any --apply.
"""
import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

DATA_DIR = Path("data")
USER_COLS = ("discord_id", "username", "chips", "total_wagered", "total_won", "created_at")


def _resolve(target: str) -> Path:
    """Accept either a full path or a bare guild_id."""
    p = Path(target)
    if p.exists():
        return p
    cand = DATA_DIR / f"sportsbook_{target}.db"
    if cand.exists():
        return cand
    sys.exit(f"target DB not found: tried '{p}' and '{cand}'")


def _table_count(con: sqlite3.Connection, table: str) -> int:
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=str(DATA_DIR / "sportsbook_0.db"),
                    help="orphaned guild-0 DB (default: data/sportsbook_0.db)")
    ap.add_argument("--target", required=True,
                    help="destination: a guild_id or a path to its sportsbook_<id>.db")
    ap.add_argument("--balance", choices=("skip", "source", "target", "max"), default="skip",
                    help="for users present in BOTH DBs: skip=leave target untouched (default), "
                         "source=overwrite with guild-0 values, target=keep target, max=higher chips")
    ap.add_argument("--restrictions", action="store_true",
                    help="also migrate betting_restrictions of type 'ALL' (no broken FKs)")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    src_path = Path(args.source)
    if not src_path.exists():
        sys.exit(f"source DB not found: {src_path}")
    dst_path = _resolve(args.target)
    if src_path.resolve() == dst_path.resolve():
        sys.exit("source and target are the same file")

    # Read-only so we never mutate the source or auto-create a missing file.
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(dst_path)
    dst.row_factory = sqlite3.Row

    print(f"source: {src_path}")
    print(f"target: {dst_path}")
    print(f"mode:   {'APPLY' if args.apply else 'DRY RUN'}   balance-conflict: {args.balance}\n")

    # Report what is being left behind (broken market/tribute FKs).
    for t in ("bets", "parlays", "pending_parlay_legs"):
        n = _table_count(src, t)
        if n:
            print(f"  ! skipping {n} row(s) in '{t}' (market FKs do not map across DBs)")
    print()

    src_users = {r["discord_id"]: r for r in src.execute(f"SELECT {', '.join(USER_COLS)} FROM users")}
    dst_ids = {r["discord_id"] for r in dst.execute("SELECT discord_id FROM users")}

    inserts, updates, conflicts = [], [], []
    for did, row in src_users.items():
        if did not in dst_ids:
            inserts.append(row)
        else:
            conflicts.append(did)
            if args.balance == "source":
                updates.append(row)
            elif args.balance == "max":
                cur = dst.execute("SELECT chips FROM users WHERE discord_id = ?", (did,)).fetchone()
                if row["chips"] > cur["chips"]:
                    updates.append(row)

    print(f"users in guild-0 DB:        {len(src_users)}")
    print(f"  → new (insert):           {len(inserts)}")
    print(f"  → already in target:      {len(conflicts)}  "
          f"(balance={args.balance} ⇒ {len(updates)} updated)")
    for row in inserts:
        print(f"      + {row['discord_id']:<20} {row['username']:<22} chips={row['chips']}")
    for row in updates:
        cur = dst.execute("SELECT chips FROM users WHERE discord_id = ?", (row["discord_id"],)).fetchone()
        print(f"      ~ {row['discord_id']:<20} {row['username']:<22} chips {cur['chips']} → {row['chips']}")

    restr_rows = []
    if args.restrictions:
        try:
            restr_rows = src.execute(
                "SELECT discord_user_id, restriction_type, district, tribute_id "
                "FROM betting_restrictions WHERE restriction_type = 'ALL'"
            ).fetchall()
        except sqlite3.OperationalError:
            pass
        print(f"\nbetting_restrictions (type ALL): {len(restr_rows)} to migrate")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
        return

    backup = dst_path.with_name(f"{dst_path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(dst_path, backup)
    print(f"\nbackup written: {backup}")

    gid = int(dst_path.stem.split("_")[-1]) if dst_path.stem.split("_")[-1].isdigit() else 0
    cur = dst.cursor()
    for row in inserts:
        cur.execute(
            "INSERT INTO users (guild_id, discord_id, username, chips, total_wagered, total_won, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (gid, row["discord_id"], row["username"], row["chips"],
             row["total_wagered"], row["total_won"], row["created_at"]),
        )
    for row in updates:
        cur.execute(
            "UPDATE users SET username=?, chips=?, total_wagered=?, total_won=? WHERE discord_id=?",
            (row["username"], row["chips"], row["total_wagered"], row["total_won"], row["discord_id"]),
        )
    for r in restr_rows:
        cur.execute(
            "INSERT INTO betting_restrictions (guild_id, discord_user_id, restriction_type, district, tribute_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (gid, r["discord_user_id"], r["restriction_type"], r["district"], r["tribute_id"]),
        )
    dst.commit()
    print(f"committed: {len(inserts)} inserted, {len(updates)} updated, {len(restr_rows)} restrictions.")


if __name__ == "__main__":
    main()
