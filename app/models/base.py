from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JsonB = JSON().with_variant(JSONB(), "postgresql")

TimestampTZ = DateTime(timezone=True)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JsonB, list[dict[str, Any]]: JsonB}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(TimestampTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TimestampTZ, server_default=func.now(), onupdate=func.now()
    )
