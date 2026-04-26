"""add classifier columns to markets

Revision ID: d5e9f120ab37
Revises: c4d8f2e0a91b
Create Date: 2026-04-25 19:35:00.000000

Adds the output columns for the layered market classifier (see
app/services/classifier/). Each row records what the classifier decided plus
which layer / rule produced the verdict, so the priority assignment can be
audited and reclassified deterministically when the rule set is bumped.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5e9f120ab37'
down_revision: Union[str, Sequence[str], None] = 'c4d8f2e0a91b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('markets', sa.Column('category', sa.String(length=64), nullable=True))
    op.add_column('markets', sa.Column('subcategory', sa.String(length=64), nullable=True))
    op.add_column('markets', sa.Column('manipulability_prior', sa.String(length=16), nullable=True))
    op.add_column('markets', sa.Column('classifier_tags', sa.JSON(), nullable=True))
    op.add_column('markets', sa.Column('classifier_layer', sa.String(length=32), nullable=True))
    op.add_column('markets', sa.Column('classifier_rule', sa.String(length=128), nullable=True))
    op.add_column('markets', sa.Column('classifier_confidence', sa.String(length=16), nullable=True))
    op.add_column('markets', sa.Column('classifier_version', sa.Integer(), nullable=True))

    op.create_index(op.f('ix_markets_category'), 'markets', ['category'], unique=False)
    op.create_index(op.f('ix_markets_subcategory'), 'markets', ['subcategory'], unique=False)
    op.create_index(
        op.f('ix_markets_manipulability_prior'),
        'markets',
        ['manipulability_prior'],
        unique=False,
    )
    op.create_index(
        op.f('ix_markets_classifier_confidence'),
        'markets',
        ['classifier_confidence'],
        unique=False,
    )
    op.create_index(
        op.f('ix_markets_classifier_version'),
        'markets',
        ['classifier_version'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_markets_classifier_version'), table_name='markets')
    op.drop_index(op.f('ix_markets_classifier_confidence'), table_name='markets')
    op.drop_index(op.f('ix_markets_manipulability_prior'), table_name='markets')
    op.drop_index(op.f('ix_markets_subcategory'), table_name='markets')
    op.drop_index(op.f('ix_markets_category'), table_name='markets')

    op.drop_column('markets', 'classifier_version')
    op.drop_column('markets', 'classifier_confidence')
    op.drop_column('markets', 'classifier_rule')
    op.drop_column('markets', 'classifier_layer')
    op.drop_column('markets', 'classifier_tags')
    op.drop_column('markets', 'manipulability_prior')
    op.drop_column('markets', 'subcategory')
    op.drop_column('markets', 'category')
