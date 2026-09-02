"""Persisted OpenSSH public keys available to instance provisioning tasks."""

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class SshPublicKey(Base):
    __tablename__ = "ssh_public_key"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128, collation="NOCASE"), nullable=False)
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    update_time: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.now,
        onupdate=datetime.now,
    )

    __table_args__ = (
        Index("ssh_public_key_name", name, unique=True),
        Index("ssh_public_key_fingerprint", fingerprint, unique=True),
        Index("ssh_public_key_create_time", create_time.desc()),
    )
