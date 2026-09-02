"""
开机任务表模型

对应 Java 原项目：com.yohann.ocihelper.bean.entity.OciCreateTask
对应数据库表：oci_create_task
功能：存储自动创建 OCI 实例的任务配置和状态。
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class OciCreateTask(Base):
    """
    开机任务表

    记录用户提交的自动开机/创建实例的任务。
    任务调度器会定期检查此表并执行开机操作。

    对应 Java 原项目中 @TableName("oci_create_task") 实体类。
    """

    __tablename__ = "oci_create_task"

    # ============================
    #  主键
    # ============================
    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # ============================
    #  关联用户
    # ============================
    # 关联的 OCI 用户配置 ID（外键关系在应用层维护）
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ============================
    #  实例配置
    # ============================
    # OCI 区域（可覆盖用户默认区域）
    oci_region: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 用户指定的实例显示名称；批量任务执行时自动追加序号
    instance_name: Mapped[str | None] = mapped_column(String(251), nullable=True)

    # CPU 核数（ARM 灵活配置，默认 1.0）
    ocpus: Mapped[float] = mapped_column(Float, default=1.0)

    # 内存大小（GB，默认 6.0）
    memory: Mapped[float] = mapped_column(Float, default=6.0)

    # 磁盘大小（GB，默认 50）
    disk: Mapped[int] = mapped_column(Integer, default=50)

    # 引导卷性能（VPU/GB，默认 20 = 高性能）
    boot_volume_vpus_per_gb: Mapped[int] = mapped_column(Integer, nullable=False, default=20)

    # CPU 架构类型（"ARM" 或 "AMD"，默认 "ARM"）
    architecture: Mapped[str] = mapped_column(String(64), default="ARM")

    # 随机重试间隔下限（秒，兼容旧版 interval 字段）
    interval: Mapped[int] = mapped_column(Integer, default=60)

    # 随机重试间隔上限（秒）
    interval_max: Mapped[int] = mapped_column(Integer, default=60)

    # 固定可用域；为空时在支持当前 Shape 的可用域之间轮换
    availability_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # 自动可用域轮换游标；网络结果不确定时保持不变以安全复用幂等令牌
    availability_domain_index: Mapped[int] = mapped_column(Integer, default=0)

    # 要创建的实例数量（默认 1）
    create_numbers: Mapped[int] = mapped_column(Integer, default=1)

    # 任务最初请求的实例数量，用于生成稳定的批量实例名称序号
    initial_create_numbers: Mapped[int] = mapped_column(Integer, default=1)

    # API 和新建数据库均强制要求 SSH 公钥非空。
    ssh_public_key: Mapped[str] = mapped_column(Text, nullable=False)

    # OCI 镜像的操作系统名称和版本；版本为空时兼容旧版枚举值。
    operation_system: Mapped[str] = mapped_column(String(128), default="Ubuntu")
    operation_system_version: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # 任务暂停标志：0 = 运行中，1 = 已暂停
    paused: Mapped[int] = mapped_column(Integer, default=0)

    # Persistent state used to safely recover tasks after restart.
    status: Mapped[str] = mapped_column(String(32), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # 任务总尝试次数上限；0 表示不限次数
    max_attempts: Mapped[int] = mapped_column(Integer, default=1000)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    # ============================
    #  时间戳
    # ============================
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)

    # ============================
    #  索引定义
    # ============================
    __table_args__ = (Index("oci_create_task_create_time", create_time.desc()),)

    def __repr__(self) -> str:
        return (
            f"<OciCreateTask(id={self.id}, architecture={self.architecture}, "
            f"ocpus={self.ocpus}, memory={self.memory})>"
        )
