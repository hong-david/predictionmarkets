"""Add ON DELETE CASCADE to all FKs pointing at markets.id.

Why: the prune script (and any future retention sweep) needs to be able
to issue a single `DELETE FROM markets WHERE ...` and have child rows go
with the parent. Without DB-level cascade, that DELETE fails on FK
violation; the only alternative is iterating every Market through the
ORM, which is ~100x slower and won't scale to the 285k exotic-combo
purge we're about to run.

We previously relied on SQLAlchemy's `cascade="all, delete-orphan"` on
the relationship — that handles ORM `db.delete(market)` but is silent at
the SQL level. This migration brings the DB schema in line with what the
ORM already promises.

Affects four FK constraints:
  - market_snapshots.market_pk -> markets.id
  - trades.market_pk            -> markets.id
  - book_events.market_pk       -> markets.id
  - anomalies.market_pk         -> markets.id

Postgres-only migration (uses `op.drop_constraint` + recreate). On
SQLite this would be a table rebuild, but Alembic is configured for
Postgres only in this project.

Revision ID: f1a2c3d4b5e6
Revises: d5e9f120ab37
Create Date: 2026-04-25
"""
from collections.abc import Sequence
from typing import Union

from alembic import op

revision: str = "f1a2c3d4b5e6"
down_revision: Union[str, Sequence[str], None] = "d5e9f120ab37"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table_name, fk_name, local_col)
_CHILD_FKS = [
    ("market_snapshots", "market_snapshots_market_pk_fkey", "market_pk"),
    ("trades", "trades_market_pk_fkey", "market_pk"),
    ("book_events", "book_events_market_pk_fkey", "market_pk"),
    ("anomalies", "anomalies_market_pk_fkey", "market_pk"),
]


def upgrade() -> None:
    for table, fk_name, col in _CHILD_FKS:
        op.drop_constraint(fk_name, table, type_="foreignkey")
        op.create_foreign_key(
            fk_name,
            table,
            "markets",
            [col],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    for table, fk_name, col in _CHILD_FKS:
        op.drop_constraint(fk_name, table, type_="foreignkey")
        op.create_foreign_key(
            fk_name,
            table,
            "markets",
            [col],
            ["id"],
        )
