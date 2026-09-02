"""
CPU 架构枚举模块

对应 Java 原项目：com.yohann.ocihelper.enums.ArchitectureEnum
功能：定义 OCI 支持的计算实例 Shape 类型及其计费模式。
"""

from enum import Enum


class BillingType(Enum):
    """
    计费类型枚举

    对应 Java: com.oracle.bmc.core.model.Shape.BillingType
    """

    ALWAYS_FREE = "ALWAYS_FREE"  # 永久免费
    LIMITED_FREE = "LIMITED_FREE"  # 限时免费
    PAID = "PAID"  # 付费


class Architecture(Enum):
    """
    CPU 架构和实例 Shape 枚举

    对应 Java 原项目中的 ArchitectureEnum。
    每个枚举值包含: (架构类型标识, OCI Shape 名称, 计费类型)

    示例:
        arch = Architecture.ARM
        print(arch.arch_type)      # "ARM"
        print(arch.shape_detail)   # "VM.Standard.A1.Flex"
        print(arch.billing_type)   # BillingType.LIMITED_FREE
    """

    # 架构类型：(类型标识, OCI Shape 全称, 计费类型)
    AMD = ("AMD", "VM.Standard.E2.1.Micro", BillingType.ALWAYS_FREE)
    ARM = ("ARM", "VM.Standard.A1.Flex", BillingType.LIMITED_FREE)
    ARM_A2 = ("VM.Standard.A2.Flex", "VM.Standard.A2.Flex", BillingType.PAID)
    AMD_E5 = ("AMD_E5", "VM.Standard.E5.Flex", BillingType.PAID)

    def __init__(self, arch_type: str, shape_detail: str, billing_type: BillingType):
        """
        初始化架构枚举

        参数:
            arch_type: 架构类型标识，如 "AMD"、"ARM"
            shape_detail: OCI Shape 的完整名称
            billing_type: 计费类型
        """
        self.arch_type = arch_type
        self.shape_detail = shape_detail
        self.billing_type = billing_type

    @classmethod
    def get_shape_by_type(cls, arch_type: str) -> str:
        """
        根据架构类型获取对应的 OCI Shape 名称

        对应 Java: ArchitectureEnum.getType(type)

        参数:
            arch_type: 架构类型标识

        返回:
            匹配的 Shape 名称，未找到时返回原始输入
        """
        normalized = (arch_type or "").strip().lower()
        for arch in cls:
            if normalized in {
                arch.name.lower(),
                arch.arch_type.lower(),
                arch.shape_detail.lower(),
            }:
                return arch.shape_detail
        return arch_type
