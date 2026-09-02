"""
键值表模型

对应 Java 原项目：com.yohann.ocihelper.bean.entity.OciKv
对应数据库表：oci_kv
功能：存储系统配置的键值对（如 Telegram Bot Token、聊天 ID 等）。
"""

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class OciKv(Base):
    """
    键值配置表

    通用的键值对存储表，用于持久化系统配置。
    相当于一个简易的配置中心，支持按 code 和 type 检索。

    对应 Java 原项目中 @TableName("oci_kv") 实体类。
    """

    __tablename__ = "oci_kv"

    # ============================
    #  主键
    # ============================
    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # ============================
    #  键值数据
    # ============================
    # 配置项代码（如 "Y101" 对应 TG Bot Token）
    code: Mapped[str] = mapped_column(String(64), nullable=False)

    # 配置项值（文本类型，支持较长的值）
    value: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 配置项类型（对应 SysCfgTypeEnum 的 code）
    type: Mapped[str] = mapped_column(String(64), nullable=False)

    # ============================
    #  时间戳
    # ============================
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)

    # ============================
    #  索引定义
    # ============================
    __table_args__ = (
        Index("oci_kv_code", code.desc()),
        Index("oci_kv_type", type.desc()),
        Index("oci_kv_create_time", create_time.desc()),
    )

    def __repr__(self) -> str:
        return f"<OciKv(id={self.id}, code={self.code}, type={self.type})>"
