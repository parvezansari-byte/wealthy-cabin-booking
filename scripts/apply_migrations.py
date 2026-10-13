"""Apply versioned SQL migrations in order, exactly once each.

Usage:
    DATABASE_URL=postgresql://postgres:...@db.<ref>.supabase.co:5432/postgres \
        python scripts/apply_migrations.py [--dry-run]

Applied versions are recorded in private.schema_migrations with a checksum.
A migration whose file changed after being applied is reported as an error:
write a new migration instead of editing an applied one.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "database" / "migrations"

BOOKKEEPING_SQL = """
create schema if not exists private;
create table if not exists private.schema_migrations (
    version     text primary key,
    checksum    text not null,
    applied_at  timestamptz not null default now()
);
"""


def migration_files(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    return sorted(p for p in directory.glob("*.sql") if p.name[:4].isdigit())


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply_migrations(conninfo: str, directory: Path = MIGRATIONS_DIR, dry_run: bool = False,
                     out=print) -> list[str]:
    applied_now: list[str] = []
    with psycopg.connect(conninfo, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(BOOKKEEPING_SQL)
            cur.execute("select version, checksum from private.schema_migrations")
            applied = dict(cur.fetchall())
        conn.commit()

        for path in migration_files(directory):
            version, digest = path.name, checksum(path)
            if version in applied:
                if applied[version] != digest:
                    raise SystemExit(
                        f"ERROR: {version} was modified after it was applied. "
                        "Create a new migration instead.")
                continue
            if dry_run:
                out(f"would apply {version}")
                continue
            out(f"applying {version} …")
            try:
                with conn.cursor() as cur:
                    cur.execute(path.read_text(encoding="utf-8"))
                    cur.execute(
                        "insert into private.schema_migrations (version, checksum) values (%s, %s)",
                        (version, digest))
                conn.commit()
            except Exception:
                conn.rollback()
                out(f"FAILED {version} — rolled back")
                raise
            applied_now.append(version)
    out("database is up to date" if not applied_now else f"applied {len(applied_now)} migration(s)")
    return applied_now


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="list pending migrations only")
    args = parser.parse_args()
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    apply_migrations(url, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
