"""
操作系统枚举模块

对应 Java 原项目：com.yohann.ocihelper.enums.OperationSystemEnum
功能：兼容旧任务和旧版 API 使用的操作系统枚举值。
"""

from enum import Enum


class OperationSystem(Enum):
    """
    旧版操作系统镜像枚举

    对应 Java 原项目中的 OperationSystemEnum。
    新建任务的选项来自 OCI API；此枚举用于解析已保存的旧值。
    """

    # (镜像厂商/类型, 版本号)
    ORACLE_LINUX = ("Oracle Linux", "9")
    UBUNTU_20_04 = ("Canonical Ubuntu", "20.04")
    UBUNTU_20_04_MINIMAL = ("Canonical Ubuntu", "20.04 Minimal")
    UBUNTU_22_04 = ("Canonical Ubuntu", "22.04")
    UBUNTU_22_04_MINIMAL = ("Canonical Ubuntu", "22.04 Minimal")
    UBUNTU_22_04_MINIMAL_AARCH64 = ("Canonical Ubuntu", "22.04 Minimal aarch64")
    UBUNTU_24_04 = ("Canonical Ubuntu", "24.04")
    CENT_OS_7 = ("CentOS", "7")
    CENT_OS_8_STREAM = ("CentOS", "8 Stream")

    def __init__(self, os_type: str, version: str):
        """
        初始化操作系统枚举

        参数:
            os_type: 操作系统类型/厂商名称
            version: 版本号
        """
        self.os_type = os_type
        self.version = version

    @classmethod
    def get_system_type(cls, os_type: str) -> "OperationSystem":
        """
        根据操作系统类型获取枚举实例

        对应 Java: OperationSystemEnum.getSystemType(type)
        对于 Ubuntu 系列，默认返回 22.04 版本。

        参数:
            os_type: 操作系统类型标识

        返回:
            匹配的枚举实例，未找到时默认返回 UBUNTU_22_04
        """
        normalized = (os_type or "").strip().lower()
        if normalized in {"ubuntu", "canonical ubuntu"}:
            return cls.UBUNTU_22_04
        for item in cls:
            candidates = {
                item.name.lower(),
                item.os_type.lower(),
                f"{item.os_type} {item.version}".lower(),
            }
            if normalized in candidates:
                return item
        raise ValueError(f"unsupported operating system: {os_type}")

    @classmethod
    def resolve(cls, os_type: str, version: str | None = None) -> tuple[str, str]:
        """Resolve a dynamic OCI image pair or a legacy enum selection."""
        normalized_type = (os_type or "").strip()
        normalized_version = (version or "").strip()
        if normalized_version:
            if not normalized_type:
                raise ValueError("operating system is required")
            return normalized_type, normalized_version
        legacy = cls.get_system_type(normalized_type)
        return legacy.os_type, legacy.version
