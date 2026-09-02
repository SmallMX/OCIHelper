"""
OCI 用户表模型

对应 Java 原项目：com.yohann.ocihelper.bean.entity.OciUser
对应数据库表：oci_user
功能：存储 OCI API 密钥配置信息，每条记录代表一个 OCI 租户/用户的认证凭据。
"""

from datetime import datetime

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class OciUser(Base):
    """
    OCI 用户配置表

    存储用户添加的 OCI API 密钥配置信息。
    每条记录对应 OCI 控制台中一个 API Key 的完整配置。

    对应 Java 原项目中 @TableName("oci_user") 实体类。
    """

    __tablename__ = "oci_user"

    # ============================
    #  主键
    # ============================
    # 唯一标识符（UUID），对应 Java 中 @TableId
    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # ============================
    #  用户信息
    # ============================
    # 配置名称（用户自定义的标识名）
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 租户名称（从 OCI API 自动获取）
    tenant_name: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 租户创建时间（从 OCI API 自动获取）
    tenant_create_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # ============================
    #  OCI 认证配置
    # ============================
    # OCI 租户 OCID
    oci_tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # OCI 用户 OCID
    oci_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # OCI API 密钥指纹
    oci_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    # OCI 区域标识（如 "ap-tokyo-1"）
    oci_region: Mapped[str] = mapped_column(String(32), nullable=False)

    # OCI API 私钥文件在服务器上的绝对路径
    oci_key_path: Mapped[str] = mapped_column(String(256), nullable=False)

    # ============================
    #  时间戳
    # ============================
    # 记录创建时间（默认当前时间）
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)

    # ============================
    #  索引定义
    # ============================
    __table_args__ = (Index("oci_user_create_time", create_time.desc()),)

    def __repr__(self) -> str:
        return f"<OciUser(id={self.id}, username={self.username}, region={self.oci_region})>"
