"""Typed routes for retained OCI auxiliary features."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import require_auth
from database import get_db
from exceptions import OciException
from schemas.other_schemas import (
    AddEgressSecurityRuleParams,
    AddIngressSecurityRuleParams,
    BootVolumePageParams,
    GetLimitsParams,
    GetSecurityRuleListPageParams,
    GetTrafficDataParams,
    OciCfgParams,
    RemoveSecurityRuleParams,
    RemoveVcnParams,
    TerminateBootVolumeParams,
    UpdateBootVolumeParams,
    VcnPageParams,
)
from schemas.response import ResponseData
from services.extra_service import (
    BootVolumeService,
    LimitsService,
    SecurityRuleService,
    TenantService,
    TrafficService,
    VcnService,
)
from services.oci_service import OciService

tenant_router = APIRouter(prefix="/api/tenant", tags=["租户信息"])
security_rule_router = APIRouter(prefix="/api/securityRule", tags=["安全规则"])
vcn_router = APIRouter(prefix="/api/vcn", tags=["VCN 管理"])
boot_volume_router = APIRouter(prefix="/api/bootVolume", tags=["引导卷管理"])
traffic_router = APIRouter(prefix="/api/traffic", tags=["流量统计"])
limits_router = APIRouter(prefix="/api/limits", tags=["限额查询"])


@tenant_router.post("/tenantInfo")
@tenant_router.post("/info", include_in_schema=False)
async def tenant_info(
    params: OciCfgParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    data = await TenantService.get_tenant_info(params.oci_cfg_id, db)
    return ResponseData.success(data=data, message="获取租户信息成功")


@security_rule_router.post("/page")
async def security_rule_page(
    params: GetSecurityRuleListPageParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    data = await SecurityRuleService.get_page(
        params.oci_cfg_id,
        params.vcn_id,
        params.rule_type,
        params.keyword,
        params.current_page,
        params.page_size,
        params.clean_relaunch,
        db,
    )
    return ResponseData.success(data=data, message="获取安全规则列表成功")


@security_rule_router.post("/addIngress")
async def add_ingress(
    params: AddIngressSecurityRuleParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await SecurityRuleService.add_ingress(params, db)
    return ResponseData.success(message="添加入站规则成功")


@security_rule_router.post("/addEgress")
async def add_egress(
    params: AddEgressSecurityRuleParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await SecurityRuleService.add_egress(params, db)
    return ResponseData.success(message="添加出站规则成功")


@security_rule_router.post("/remove")
async def remove_security_rule(
    params: RemoveSecurityRuleParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await SecurityRuleService.remove_rules(
        params.oci_cfg_id, params.vcn_id, params.rule_type, params.rule_ids, db
    )
    return ResponseData.success(message="删除安全规则成功")


@vcn_router.post("/page")
async def vcn_page(
    params: VcnPageParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    data = await VcnService.get_page(
        params.oci_cfg_id,
        params.keyword,
        params.current_page,
        params.page_size,
        params.clean_relaunch,
        db,
    )
    return ResponseData.success(data=data, message="获取 VCN 列表成功")


@vcn_router.post("/remove")
async def remove_vcn(
    params: RemoveVcnParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    if not OciService.verify_captcha(params.oci_cfg_id, None, params.captcha):
        raise OciException(-1, "验证码错误或已过期")
    await VcnService.remove_vcn(params.oci_cfg_id, params.vcn_ids, db)
    return ResponseData.success(message="删除 VCN 成功")


@boot_volume_router.post("/page")
async def boot_volume_page(
    params: BootVolumePageParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    data = await BootVolumeService.get_page(
        params.oci_cfg_id,
        params.keyword,
        params.current_page,
        params.page_size,
        params.clean_relaunch,
        db,
    )
    return ResponseData.success(data=data, message="获取引导卷列表成功")


@boot_volume_router.post("/update")
async def update_boot_volume(
    params: UpdateBootVolumeParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await BootVolumeService.update(
        params.oci_cfg_id,
        params.boot_volume_id,
        params.boot_volume_size,
        params.boot_volume_vpu,
        db,
    )
    return ResponseData.success(message="更改引导卷配置成功")


@boot_volume_router.post("/terminate")
async def terminate_boot_volume(
    params: TerminateBootVolumeParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    if not OciService.verify_captcha(params.oci_cfg_id, None, params.captcha):
        raise OciException(-1, "验证码错误或已过期")
    await BootVolumeService.terminate(params.oci_cfg_id, params.boot_volume_ids, db)
    return ResponseData.success(message="终止引导卷命令已下发")


@traffic_router.post("/data")
async def traffic_data(
    params: GetTrafficDataParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    data = await TrafficService.get_traffic_data(params, db)
    return ResponseData.success(data=data, message="获取流量数据成功")


@limits_router.get("/services")
async def limit_services(
    oci_cfg_id: str = Query(..., alias="ociCfgId"),
    region: str | None = Query(default=None),
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    data = await LimitsService.get_service_names(oci_cfg_id, region, db)
    return ResponseData.success(data=data, message="获取服务列表成功")


@limits_router.post("/query")
async def query_limits(
    params: GetLimitsParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    data = await LimitsService.query(params.oci_cfg_id, params.region, params.service_name, db)
    return ResponseData.success(data=data, message="查询配额成功")
