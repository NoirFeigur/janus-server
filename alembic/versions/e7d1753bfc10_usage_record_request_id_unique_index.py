"""usage_record.request_id 加唯一索引防重复扣费

Revision ID: e7d1753bfc10
Revises: 9e9aff0d54ff
Create Date: 2026-06-26 11:24:23.087009

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7d1753bfc10'
down_revision: str | None = '9e9aff0d54ff'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 0) 同步列注释（模型已标注唯一语义）。
    op.alter_column(
        "usage_record",
        "request_id",
        existing_type=sa.VARCHAR(length=64),
        existing_nullable=True,
        comment="贯穿网关日志/Redis 的关联 id（唯一：记账幂等防重复扣费）",
        existing_comment="贯穿网关日志/Redis 的关联 id",
    )
    # 1) 清历史重复：每个非空 request_id 仅保留最早一行（最小 id），其余删除，
    #    否则唯一索引创建会失败。NULL request_id 互不冲突，保持原样。
    op.execute(
        sa.text(
            "DELETE FROM usage_record a "
            "USING usage_record b "
            "WHERE a.request_id IS NOT NULL "
            "AND a.request_id = b.request_id "
            "AND a.id > b.id"
        )
    )
    # 2) 丢弃旧的非唯一索引（同名将被唯一索引取代）。
    op.drop_index("ix_usage_record_request_id", table_name="usage_record")
    # 3) 并发建唯一索引（不锁表，热路径友好）；CONCURRENTLY 必须在事务外。
    with op.get_context().autocommit_block():
        op.execute(
            sa.text(
                "CREATE UNIQUE INDEX CONCURRENTLY ix_usage_record_request_id "
                "ON usage_record (request_id)"
            )
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(sa.text("DROP INDEX CONCURRENTLY ix_usage_record_request_id"))
    op.create_index(
        "ix_usage_record_request_id",
        "usage_record",
        ["request_id"],
        unique=False,
    )
    op.alter_column(
        "usage_record",
        "request_id",
        existing_type=sa.VARCHAR(length=64),
        existing_nullable=True,
        comment="贯穿网关日志/Redis 的关联 id",
        existing_comment="贯穿网关日志/Redis 的关联 id（唯一：记账幂等防重复扣费）",
    )
