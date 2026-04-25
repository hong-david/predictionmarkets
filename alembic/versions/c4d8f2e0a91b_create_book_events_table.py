"""create book_events table

Revision ID: c4d8f2e0a91b
Revises: b3a7e4c19f02
Create Date: 2026-04-25 17:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d8f2e0a91b'
down_revision: Union[str, Sequence[str], None] = 'b3a7e4c19f02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'book_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('market_pk', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.String(length=36), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('ts', sa.DateTime(timezone=True), nullable=True),
        sa.Column('side', sa.String(length=3), nullable=False),
        sa.Column('price_dollars', sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column('size_fp', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('delta_fp', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('is_snapshot', sa.Boolean(), nullable=False),
        sa.Column(
            'received_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['market_pk'], ['markets.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'session_id',
            'seq',
            'side',
            'price_dollars',
            name='uq_book_events_session_seq_side_price',
        ),
    )
    op.create_index(
        op.f('ix_book_events_market_pk'),
        'book_events',
        ['market_pk'],
        unique=False,
    )
    op.create_index(
        op.f('ix_book_events_session_id'),
        'book_events',
        ['session_id'],
        unique=False,
    )
    op.create_index(
        'ix_book_events_market_pk_id',
        'book_events',
        ['market_pk', 'id'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_book_events_market_pk_id', table_name='book_events')
    op.drop_index(op.f('ix_book_events_session_id'), table_name='book_events')
    op.drop_index(op.f('ix_book_events_market_pk'), table_name='book_events')
    op.drop_table('book_events')
