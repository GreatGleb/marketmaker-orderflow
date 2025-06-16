from uuid import uuid4


from datetime import datetime

from sqlalchemy import func, types, String, ForeignKey, Table, Column
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
    relationship,
    declarative_base,
)


Base = declarative_base()


class BaseUUID(Base):
    __abstract__ = True

    id: Mapped[int] = mapped_column(
        types.UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        default=uuid4,
    )
    created_at: Mapped[datetime] = mapped_column(
        types.DateTime,
        server_default=func.now(),
        nullable=False,
        index=True,
        comment="Date and time of create",
    )
    updated_at: Mapped[datetime] = mapped_column(
        types.DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="Date and time of update",
    )