"""create trades table

Revision ID: b3a7e4c19f02
Revises: da2173380c55
Create Date: 2026-04-25 16:42:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3a7e4c19f02'
down_revision: Union[str, Sequence[str], None] = 'da2173380c55'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'trades',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('market_pk', sa.Integer(), nullable=False),
        sa.Column('trade_id', sa.String(length=128), nullable=False),
        sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
        sa.Column('yes_price_dollars', sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column('no_price_dollars', sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column('count_fp', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('taker_side', sa.String(length=3), nullable=True),
        sa.Column(
            'received_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['market_pk'], ['markets.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_trades_market_pk'), 'trades', ['market_pk'], unique=False)
    op.create_index(op.f('ix_trades_ts'), 'trades', ['ts'], unique=False)
    op.create_index(op.f('ix_trades_trade_id'), 'trades', ['trade_id'], unique=True)
    op.create_index('ix_trades_market_pk_ts', 'trades', ['market_pk', 'ts'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_trades_market_pk_ts', table_name='trades')
    op.drop_index(op.f('ix_trades_trade_id'), table_name='trades')
    op.drop_index(op.f('ix_trades_ts'), table_name='trades')
    op.drop_index(op.f('ix_trades_market_pk'), table_name='trades')
    op.drop_table('trades')
