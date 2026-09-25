"""tickets.source + users.telegram_chat_id

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-26

A ticket can now arrive from Telegram as well as the web form, and a decision
can be made from a phone. Both facts need somewhere to live: which channel a
ticket came in on, and which person a given Telegram chat belongs to.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    postgresql.ENUM("WEB", "TELEGRAM", name="ticket_source").create(bind, checkfirst=True)
    ticket_source = postgresql.ENUM("WEB", "TELEGRAM", name="ticket_source", create_type=False)

    # server_default rather than a nullable column backfilled later: every
    # ticket that already exists arrived through the web form, so WEB is the
    # true answer for all of them and the column can be NOT NULL from the
    # start. Dropping the default afterwards would only push the decision
    # onto every future INSERT for no gain.
    op.add_column(
        "tickets",
        sa.Column("source", ticket_source, nullable=False, server_default="WEB"),
    )

    # BigInteger: Telegram chat ids for groups and channels are already past
    # what a 32-bit int holds, and getting this wrong only shows up the first
    # time someone adds the bot to a group.
    op.add_column("users", sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True))
    # Unique so one chat cannot be linked to two accounts. Without it, "who
    # approved this" becomes ambiguous the moment either account taps a button.
    op.create_unique_constraint("uq_users_telegram_chat_id", "users", ["telegram_chat_id"])


def downgrade() -> None:
    op.drop_constraint("uq_users_telegram_chat_id", "users", type_="unique")
    op.drop_column("users", "telegram_chat_id")
    op.drop_column("tickets", "source")
    # Unlike the enum *value* added in 0005, a whole type can be dropped -
    # nothing else references it once the column above is gone.
    postgresql.ENUM(name="ticket_source").drop(op.get_bind(), checkfirst=True)
