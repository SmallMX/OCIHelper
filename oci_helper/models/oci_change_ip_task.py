"""Persistent public-IP rotation task."""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class OciChangeIpTask(Base):
    __tablename__ = "oci_change_ip_task"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    instance_id: Mapped[str] = mapped_column(String(255), nullable=False)
    vnic_id: Mapped[str] = mapped_column(String(255), nullable=False)
    cidr_list: Mapped[str | None] = mapped_column(Text, nullable=True)
    interval: Mapped[int] = mapped_column(Integer, default=10)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=120)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
    )

    __table_args__ = (
        Index("oci_change_ip_instance_status", instance_id, status),
        Index("oci_change_ip_create_time", create_time.desc()),
    )
