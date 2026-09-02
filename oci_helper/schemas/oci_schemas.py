"""Typed request and response contracts for OCI configuration management."""

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class IdListParams(ApiModel):
    id_list: list[str] = Field(
        min_length=1,
        max_length=100,
        validation_alias=AliasChoices("idList", "id_list"),
        serialization_alias="idList",
    )


class BasicPageParams(ApiModel):
    keyword: str | None = Field(default=None, max_length=100)
    current_page: int = Field(
        default=1,
        ge=1,
        validation_alias=AliasChoices("currentPage", "current", "current_page"),
        serialization_alias="currentPage",
    )
    page_size: int = Field(
        default=10,
        ge=1,
        le=100,
        validation_alias=AliasChoices("pageSize", "size", "page_size"),
        serialization_alias="pageSize",
    )

    @property
    def offset(self) -> int:
        return (self.current_page - 1) * self.page_size


class UpdateCfgNameParams(ApiModel):
    id: str = Field(
        min_length=1,
        validation_alias=AliasChoices("cfgId", "id"),
        serialization_alias="cfgId",
    )
    username: str = Field(
        min_length=1,
        max_length=64,
        validation_alias=AliasChoices("updateCfgName", "username"),
        serialization_alias="updateCfgName",
    )


class GetOciCfgDetailsParams(ApiModel):
    oci_cfg_id: str = Field(
        min_length=1,
        validation_alias=AliasChoices("cfgId", "ociCfgId", "oci_cfg_id"),
        serialization_alias="cfgId",
    )
    clean_relaunch_details: bool = Field(
        default=False,
        validation_alias=AliasChoices("cleanReLaunchDetails", "clean_relaunch_details"),
        serialization_alias="cleanReLaunchDetails",
    )


class SendCaptchaParams(ApiModel):
    oci_cfg_id: str = Field(
        min_length=1,
        validation_alias=AliasChoices("ociCfgId", "oci_cfg_id"),
        serialization_alias="ociCfgId",
    )
    resource_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("instanceId", "bootVolumeId", "resourceId", "resource_id"),
        serialization_alias="resourceId",
    )


class OciUserListRsp(ApiModel):
    id: str
    username: str | None = None
    tenant_name: str | None = Field(default=None, serialization_alias="tenantName")
    region: str | None = None
    region_name: str | None = Field(default=None, serialization_alias="regionName")
    create_time: str | None = Field(default=None, serialization_alias="createTime")
    enable_create: int | None = Field(default=None, serialization_alias="enableCreate")


class InstanceVnicInfo(ApiModel):
    vnic_id: str | None = Field(default=None, serialization_alias="vnicId")
    name: str | None = None


class InstanceInfo(ApiModel):
    oc_id: str | None = Field(default=None, serialization_alias="ocId")
    region: str | None = None
    name: str | None = None
    public_ip: list[str] = Field(default_factory=list, serialization_alias="publicIp")
    shape: str | None = None
    enable_change_ip: int = Field(default=0, serialization_alias="enableChangeIp")
    ocpus: str | None = None
    memory: str | None = None
    boot_volume_size: str | None = Field(default=None, serialization_alias="bootVolumeSize")
    create_time: str | None = Field(default=None, serialization_alias="createTime")
    state: str | None = None
    availability_domain: str | None = Field(default=None, serialization_alias="availabilityDomain")
    vnic_list: list[InstanceVnicInfo | dict] = Field(
        default_factory=list, serialization_alias="vnicList"
    )


class NetLoadBalancer(ApiModel):
    name: str | None = None
    status: str | None = None
    public_ip: str | None = Field(default=None, serialization_alias="publicIp")


class OciCfgDetailsRsp(ApiModel):
    user_id: str | None = Field(default=None, serialization_alias="userId")
    tenant_id: str | None = Field(default=None, serialization_alias="tenantId")
    fingerprint: str | None = None
    private_key_path: str | None = Field(default=None, serialization_alias="privateKeyPath")
    region: str | None = None
    instance_list: list[InstanceInfo | dict] = Field(
        default_factory=list, serialization_alias="instanceList"
    )
    nlb_list: list[NetLoadBalancer | dict] = Field(
        default_factory=list, serialization_alias="nlbList"
    )
