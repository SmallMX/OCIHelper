"""Contracts for retained OCI networking, storage, traffic and limits APIs."""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from schemas.oci_schemas import BasicPageParams


class ApiModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class OciCfgParams(ApiModel):
    oci_cfg_id: str = Field(
        min_length=1,
        validation_alias=AliasChoices("ociCfgId", "cfgId", "oci_cfg_id"),
        serialization_alias="ociCfgId",
    )


class CachedPageParams(BasicPageParams):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    clean_relaunch: bool = Field(
        default=False,
        validation_alias=AliasChoices("cleanReLaunch", "clean_relaunch"),
        serialization_alias="cleanReLaunch",
    )


class GetSecurityRuleListPageParams(CachedPageParams, OciCfgParams):
    vcn_id: str = Field(min_length=1, alias="vcnId")
    rule_type: int = Field(
        default=0,
        ge=0,
        le=1,
        validation_alias=AliasChoices("type", "ruleType", "rule_type"),
        serialization_alias="type",
    )


class IcmpOptions(ApiModel):
    type: int | None = Field(default=None, ge=0, le=255)
    code: int | None = Field(default=None, ge=0, le=255)

    @model_validator(mode="after")
    def validate_type_and_code(self):
        if self.code is not None and self.type is None:
            raise ValueError("ICMP code requires an ICMP type")
        return self


class SecurityRuleInput(ApiModel):
    is_stateless: bool = Field(default=False, alias="isStateless")
    protocol: str = Field(pattern=r"^(all|\d{1,3})$")
    icmp_options: IcmpOptions | None = Field(default=None, alias="icmpOptions")
    source_port: str | None = Field(default=None, alias="sourcePort", max_length=32)
    destination_port: str | None = Field(default=None, alias="destinationPort", max_length=32)
    description: str | None = Field(default=None, max_length=255)

    @field_validator("protocol")
    @classmethod
    def validate_protocol(cls, value: str) -> str:
        if value == "all":
            return value
        protocol = int(value)
        if protocol > 255:
            raise ValueError("protocol number must be between 0 and 255")
        return str(protocol)

    @field_validator("source_port", "destination_port")
    @classmethod
    def validate_port_range(cls, value: str | None) -> str | None:
        if not value:
            return None
        parts = value.strip().split("-", 1)
        try:
            ports = [int(item) for item in parts]
        except ValueError as exc:
            raise ValueError("port must be a number or range such as 20-22") from exc
        if any(port < 1 or port > 65535 for port in ports):
            raise ValueError("port must be between 1 and 65535")
        if len(ports) == 2 and ports[0] > ports[1]:
            raise ValueError("port range minimum must not exceed maximum")
        return value.strip()

    @model_validator(mode="after")
    def validate_protocol_options(self):
        if self.protocol not in {"6", "17"} and (
            self.source_port is not None or self.destination_port is not None
        ):
            raise ValueError("port ranges are supported only for TCP or UDP")
        if self.protocol not in {"1", "58"} and self.icmp_options is not None:
            raise ValueError("icmpOptions is supported only for ICMP or ICMPv6")
        return self


class IngressRuleInput(SecurityRuleInput):
    source_type: Literal["CIDR_BLOCK", "SERVICE_CIDR_BLOCK"] = Field(alias="sourceType")
    source: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_source(self):
        if self.source_type == "CIDR_BLOCK":
            network = ipaddress.ip_network(self.source, strict=False)
            if self.protocol == "1" and network.version != 4:
                raise ValueError("ICMP requires an IPv4 source CIDR")
            if self.protocol == "58" and network.version != 6:
                raise ValueError("ICMPv6 requires an IPv6 source CIDR")
        return self


class EgressRuleInput(SecurityRuleInput):
    destination_type: Literal["CIDR_BLOCK", "SERVICE_CIDR_BLOCK"] = Field(alias="destinationType")
    destination: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_destination(self):
        if self.destination_type == "CIDR_BLOCK":
            network = ipaddress.ip_network(self.destination, strict=False)
            if self.protocol == "1" and network.version != 4:
                raise ValueError("ICMP requires an IPv4 destination CIDR")
            if self.protocol == "58" and network.version != 6:
                raise ValueError("ICMPv6 requires an IPv6 destination CIDR")
        return self


class AddIngressSecurityRuleParams(OciCfgParams):
    vcn_id: str = Field(min_length=1, alias="vcnId")
    inbound_rule: IngressRuleInput = Field(alias="inboundRule")


class AddEgressSecurityRuleParams(OciCfgParams):
    vcn_id: str = Field(min_length=1, alias="vcnId")
    outbound_rule: EgressRuleInput = Field(alias="outboundRule")


class RemoveSecurityRuleParams(OciCfgParams):
    vcn_id: str = Field(min_length=1, alias="vcnId")
    rule_type: int = Field(
        ge=0,
        le=1,
        validation_alias=AliasChoices("type", "ruleType", "rule_type"),
        serialization_alias="type",
    )
    rule_ids: list[str] = Field(min_length=1, max_length=100, alias="ruleIds")


class VcnPageParams(CachedPageParams, OciCfgParams):
    pass


class RemoveVcnParams(OciCfgParams):
    vcn_ids: list[str] = Field(min_length=1, max_length=100, alias="vcnIds")
    captcha: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class BootVolumePageParams(CachedPageParams, OciCfgParams):
    pass


class TerminateBootVolumeParams(OciCfgParams):
    boot_volume_ids: list[str] = Field(min_length=1, max_length=100, alias="bootVolumeIds")
    captcha: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class UpdateBootVolumeParams(OciCfgParams):
    boot_volume_id: str = Field(min_length=1, alias="bootVolumeId")
    boot_volume_size: int = Field(ge=50, le=32_768, alias="bootVolumeSize")
    boot_volume_vpu: int = Field(ge=0, le=120, alias="bootVolumeVpu")

    @field_validator("boot_volume_vpu")
    @classmethod
    def validate_boot_volume_vpu(cls, value: int) -> int:
        return _validate_boot_volume_vpu(value)


class UpdateBootVolumeCfgParams(OciCfgParams):
    boot_volume_id: str = Field(
        min_length=1,
        validation_alias=AliasChoices("bootVolumeId", "boot_volume_id"),
        serialization_alias="bootVolumeId",
    )
    size_in_gbs: int | None = Field(
        default=None,
        ge=50,
        le=32_768,
        validation_alias=AliasChoices("sizeInGBs", "bootVolumeSize", "size_in_gbs"),
        serialization_alias="sizeInGBs",
    )
    vpus_per_gb: int | None = Field(
        default=None,
        ge=0,
        le=120,
        validation_alias=AliasChoices("vpusPerGB", "bootVolumeVpu", "vpus_per_gb"),
        serialization_alias="vpusPerGB",
    )

    @model_validator(mode="after")
    def validate_update(self):
        if self.size_in_gbs is None and self.vpus_per_gb is None:
            raise ValueError("sizeInGBs or vpusPerGB is required")
        if self.vpus_per_gb is not None:
            self.vpus_per_gb = _validate_boot_volume_vpu(self.vpus_per_gb)
        return self


class GetTrafficDataParams(OciCfgParams):
    instance_id: str = Field(min_length=1, alias="instanceId")
    begin_time: datetime | None = Field(default=None, alias="beginTime")
    end_time: datetime | None = Field(default=None, alias="endTime")
    region: str | None = Field(default=None, pattern=r"^[a-z0-9-]+$", max_length=64)
    in_query: str | None = Field(default=None, alias="inQuery", max_length=1000)
    out_query: str | None = Field(default=None, alias="outQuery", max_length=1000)
    namespace: str = Field(
        default="oci_computeagent",
        pattern=r"^[A-Za-z0-9_.-]+$",
        max_length=100,
    )

    @model_validator(mode="after")
    def validate_query_contract(self):
        if (self.begin_time is None) != (self.end_time is None):
            raise ValueError("beginTime and endTime must be provided together")
        if self.begin_time is not None and self.end_time is not None:
            begin = _as_utc(self.begin_time)
            end = _as_utc(self.end_time)
            if end <= begin:
                raise ValueError("endTime must be later than beginTime")
            if end - begin > timedelta(days=90):
                raise ValueError("traffic time range cannot exceed 90 days")
            self.begin_time = begin
            self.end_time = end
        if (self.in_query is None) != (self.out_query is None):
            raise ValueError("inQuery and outQuery must be provided together")
        if self.in_query is not None:
            self.in_query = self.in_query.strip()
            self.out_query = self.out_query.strip() if self.out_query is not None else None
            if not self.in_query or not self.out_query:
                raise ValueError("inQuery and outQuery must not be blank")
        elif self.namespace != "oci_computeagent":
            raise ValueError("custom namespace requires inQuery and outQuery")
        return self


class GetLimitsParams(OciCfgParams):
    region: str | None = Field(default=None, max_length=64)
    service_name: str | None = Field(default=None, alias="serviceName", max_length=100)


def _validate_boot_volume_vpu(value: int) -> int:
    if value not in {10, 20} and not 30 <= value <= 120:
        raise ValueError("boot volume VPU/GB must be 10, 20, or between 30 and 120")
    return value


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
