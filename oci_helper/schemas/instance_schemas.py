"""Typed contracts for instance actions and persistent OCI tasks."""

from __future__ import annotations

import ipaddress
import math
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from core.ssh_keys import normalize_ssh_public_key, normalize_ssh_public_keys
from enums.architecture import Architecture
from enums.operation_system import OperationSystem


class ApiModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


def _validate_positive_number(value: str | float | int, *, maximum: float) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("must be a number") from exc
    if not math.isfinite(number) or number <= 0 or number > maximum:
        raise ValueError(f"must be greater than 0 and no more than {maximum:g}")
    return str(value)


class InstanceCreateSpec(ApiModel):
    instance_name: str | None = Field(
        default=None,
        max_length=251,
        description="实例显示名称；批量创建时自动追加三位序号，留空时自动生成",
        validation_alias=AliasChoices("instanceName", "instance_name"),
        serialization_alias="instanceName",
    )
    ocpus: int = Field(ge=1, le=4)
    memory: str
    disk: int = Field(ge=50, le=200)
    boot_volume_vpus_per_gb: int = Field(
        default=20,
        ge=10,
        le=120,
        description="引导卷性能（VPU/GB）；默认 20（高性能）",
        validation_alias=AliasChoices(
            "bootVolumeVpusPerGB",
            "bootVolumeVpu",
            "boot_volume_vpus_per_gb",
        ),
        serialization_alias="bootVolumeVpusPerGB",
    )
    architecture: str
    interval: int = Field(ge=5, le=86_400, description="随机创建间隔下限（秒）")
    interval_max: int | None = Field(
        default=None,
        ge=5,
        le=86_400,
        description="随机创建间隔上限（秒）；省略时与 interval 相同",
        validation_alias=AliasChoices("intervalMax", "interval_max"),
        serialization_alias="intervalMax",
    )
    max_attempts: int | None = Field(
        default=None,
        ge=0,
        le=100_000,
        description="任务总尝试次数上限；0 表示不限次数",
        validation_alias=AliasChoices("maxAttempts", "max_attempts"),
        serialization_alias="maxAttempts",
    )
    availability_domain: str | None = Field(
        default=None,
        max_length=255,
        description="固定可用域；留空时自动轮换",
        validation_alias=AliasChoices("availabilityDomain", "availability_domain"),
        serialization_alias="availabilityDomain",
    )
    create_numbers: int = Field(
        ge=1,
        le=100,
        validation_alias=AliasChoices("createNumbers", "create_numbers"),
        serialization_alias="createNumbers",
    )
    operation_system: str = Field(
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("operationSystem", "operation_system"),
        serialization_alias="operationSystem",
    )
    operation_system_version: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices(
            "operationSystemVersion",
            "operation_system_version",
        ),
        serialization_alias="operationSystemVersion",
    )
    ssh_public_key: str = Field(
        min_length=1,
        max_length=16_384,
        validation_alias=AliasChoices("sshPublicKey", "ssh_public_key"),
        serialization_alias="sshPublicKey",
    )

    @field_validator("memory")
    @classmethod
    def validate_memory(cls, value: str) -> str:
        return _validate_positive_number(value, maximum=24)

    @field_validator("boot_volume_vpus_per_gb")
    @classmethod
    def validate_boot_volume_vpus_per_gb(cls, value: int) -> int:
        if value not in {10, 20} and not 30 <= value <= 120:
            raise ValueError("must be 10, 20, or between 30 and 120")
        return value

    @field_validator("architecture")
    @classmethod
    def validate_architecture(cls, value: str) -> str:
        normalized = value.strip()
        shape = Architecture.get_shape_by_type(normalized)
        supported_shapes = {item.shape_detail for item in Architecture}
        if shape not in supported_shapes:
            raise ValueError("unsupported architecture or shape")
        return normalized

    @field_validator("ssh_public_key")
    @classmethod
    def validate_ssh_public_key(cls, value: str) -> str:
        return normalize_ssh_public_keys(value)

    @field_validator("operation_system", "operation_system_version")
    @classmethod
    def normalize_operation_system(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("must not be blank or contain control characters")
        return normalized

    @field_validator("availability_domain")
    @classmethod
    def normalize_availability_domain(cls, value: str | None) -> str | None:
        normalized = (value or "").strip()
        return normalized or None

    @field_validator("instance_name")
    @classmethod
    def normalize_instance_name(cls, value: str | None) -> str | None:
        normalized = (value or "").strip()
        if not normalized:
            return None
        if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
            raise ValueError("must not contain control characters")
        return normalized

    @model_validator(mode="after")
    def validate_instance_constraints(self):
        OperationSystem.resolve(self.operation_system, self.operation_system_version)
        if self.interval_max is None:
            self.interval_max = self.interval
        if self.interval_max < self.interval:
            raise ValueError("intervalMax must be greater than or equal to interval")
        shape = Architecture.get_shape_by_type(self.architecture)
        if shape.endswith(".Flex") and float(self.memory) < max(1.0, float(self.ocpus)):
            raise ValueError("Flex shape memory must be at least 1 GB per OCPU")
        return self


class CreateInstanceParams(InstanceCreateSpec):
    user_id: str = Field(
        min_length=1,
        validation_alias=AliasChoices("userId", "user_id"),
        serialization_alias="userId",
    )


class GetShapeOptionsParams(ApiModel):
    oci_cfg_id: str = Field(
        min_length=1,
        validation_alias=AliasChoices("ociCfgId", "oci_cfg_id"),
        serialization_alias="ociCfgId",
    )
    architecture: str = Field(min_length=1, max_length=100)

    @field_validator("architecture")
    @classmethod
    def validate_architecture(cls, value: str) -> str:
        normalized = value.strip()
        shape = Architecture.get_shape_by_type(normalized)
        if shape not in {item.shape_detail for item in Architecture}:
            raise ValueError("unsupported architecture or shape")
        return normalized


class GetImageOptionsParams(GetShapeOptionsParams):
    pass


class ImageOptionRsp(ApiModel):
    operating_system: str = Field(serialization_alias="operatingSystem")
    operating_system_version: str = Field(serialization_alias="operatingSystemVersion")
    label: str


class AvailabilityDomainOptionRsp(ApiModel):
    name: str
    label: str


class InstanceInfoForBatch(InstanceCreateSpec):
    pass


class CreateInstanceBatchParams(ApiModel):
    user_ids: list[str] = Field(
        min_length=1,
        max_length=100,
        validation_alias=AliasChoices("userIds", "user_ids"),
        serialization_alias="userIds",
    )
    instance_info: InstanceInfoForBatch = Field(
        validation_alias=AliasChoices("instanceInfo", "instance_info"),
        serialization_alias="instanceInfo",
    )


class OciInstanceParams(ApiModel):
    oci_cfg_id: str = Field(
        min_length=1,
        validation_alias=AliasChoices("ociCfgId", "oci_cfg_id"),
        serialization_alias="ociCfgId",
    )
    instance_id: str = Field(
        min_length=1,
        validation_alias=AliasChoices("instanceId", "instance_id"),
        serialization_alias="instanceId",
    )


class UpdateInstanceStateParams(OciInstanceParams):
    action: Literal["STOP", "START", "RESET"]


class TerminateInstanceParams(OciInstanceParams):
    preserve_boot_volume: int = Field(
        ge=0,
        le=1,
        validation_alias=AliasChoices("preserveBootVolume", "preserve_boot_volume"),
        serialization_alias="preserveBootVolume",
    )
    captcha: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class ChangeIpParams(OciInstanceParams):
    cidr_list: list[str] | None = Field(
        default=None,
        max_length=100,
        validation_alias=AliasChoices("cidrList", "cidr_list"),
        serialization_alias="cidrList",
    )
    vnic_id: str = Field(
        min_length=1,
        validation_alias=AliasChoices("vnicId", "vnic_id"),
        serialization_alias="vnicId",
    )

    @field_validator("cidr_list")
    @classmethod
    def validate_cidrs(cls, values: list[str] | None) -> list[str] | None:
        if not values:
            return None
        normalized: list[str] = []
        for value in values:
            try:
                normalized.append(str(ipaddress.ip_network(value.strip(), strict=False)))
            except ValueError as exc:
                raise ValueError(f"invalid CIDR: {value}") from exc
        return list(dict.fromkeys(normalized))


class UpdateInstanceNameParams(OciInstanceParams):
    name: str = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized


class UpdateShapeParams(OciInstanceParams):
    shape: str = Field(min_length=1, max_length=100)
    ocpus: str | None = None
    memory: str | None = None

    @field_validator("ocpus")
    @classmethod
    def validate_ocpus(cls, value: str | None) -> str | None:
        return None if value is None else _validate_positive_number(value, maximum=160)

    @field_validator("memory")
    @classmethod
    def validate_memory(cls, value: str | None) -> str | None:
        return None if value is None else _validate_positive_number(value, maximum=2048)


class GetInstanceCfgInfoParams(OciInstanceParams):
    pass


class CreateIpv6Params(OciInstanceParams):
    pass


class StartConsoleConnectionParams(OciInstanceParams):
    public_key: str = Field(
        min_length=1,
        max_length=16_384,
        validation_alias=AliasChoices("publicKey", "public_key"),
        serialization_alias="publicKey",
    )

    @field_validator("public_key")
    @classmethod
    def validate_public_key(cls, value: str) -> str:
        return normalize_ssh_public_key(value, require_rsa=True)


class UpdateInstanceCfgParams(OciInstanceParams):
    ocpus: str | None = None
    memory: str | None = Field(
        default=None,
        validation_alias=AliasChoices("memory", "memoryInGBs"),
        serialization_alias="memory",
    )

    @model_validator(mode="after")
    def validate_values(self):
        if self.ocpus is None and self.memory is None:
            raise ValueError("ocpus or memory is required")
        if self.ocpus is not None:
            self.ocpus = _validate_positive_number(self.ocpus, maximum=160)
        if self.memory is not None:
            self.memory = _validate_positive_number(self.memory, maximum=2048)
        return self


class StopCreateParams(ApiModel):
    user_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("userId", "user_id"),
        serialization_alias="userId",
    )
    task_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("taskId", "task_id"),
        serialization_alias="taskId",
    )

    @model_validator(mode="after")
    def validate_identifier(self):
        if not self.user_id and not self.task_id:
            raise ValueError("userId or taskId is required")
        return self


class PauseCreateParams(ApiModel):
    id_list: list[str] = Field(
        min_length=1,
        max_length=100,
        validation_alias=AliasChoices("idList", "id_list"),
        serialization_alias="idList",
    )


class StopChangeIpParams(ApiModel):
    instance_id: str = Field(
        min_length=1,
        validation_alias=AliasChoices("instanceId", "instance_id"),
        serialization_alias="instanceId",
    )
    oci_cfg_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ociCfgId", "oci_cfg_id"),
        serialization_alias="ociCfgId",
    )


class CreateTaskRsp(ApiModel):
    id: str
    username: str | None = None
    region: str | None = None
    instance_name: str | None = Field(default=None, serialization_alias="instanceName")
    ocpus: str | None = None
    memory: str | None = None
    disk: int | None = None
    boot_volume_vpus_per_gb: int | None = Field(
        default=None,
        serialization_alias="bootVolumeVpusPerGB",
    )
    architecture: str | None = None
    interval: int | None = None
    interval_max: int | None = Field(default=None, serialization_alias="intervalMax")
    max_attempts: int | None = Field(default=None, serialization_alias="maxAttempts")
    availability_domain: str | None = Field(
        default=None, serialization_alias="availabilityDomain"
    )
    create_numbers: int | None = Field(default=None, serialization_alias="createNumbers")
    operation_system: str | None = Field(default=None, serialization_alias="operationSystem")
    operation_system_version: str | None = Field(
        default=None,
        serialization_alias="operationSystemVersion",
    )
    create_time: str | None = Field(default=None, serialization_alias="createTime")
    counts: str | None = None
    paused: int | None = None
    status: str | None = None
    last_error: str | None = Field(default=None, serialization_alias="lastError")
