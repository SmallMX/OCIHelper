"""OCI configuration, task and instance API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import require_auth
from core.oracle_fetcher import OracleInstanceFetcher
from database import get_db
from exceptions import OciException
from schemas.instance_schemas import (
    ChangeIpParams,
    CreateInstanceBatchParams,
    CreateInstanceParams,
    CreateIpv6Params,
    GetImageOptionsParams,
    GetInstanceCfgInfoParams,
    GetShapeOptionsParams,
    PauseCreateParams,
    StartConsoleConnectionParams,
    StopChangeIpParams,
    StopCreateParams,
    TerminateInstanceParams,
    UpdateInstanceCfgParams,
    UpdateInstanceNameParams,
    UpdateInstanceStateParams,
    UpdateShapeParams,
)
from schemas.oci_schemas import (
    BasicPageParams,
    GetOciCfgDetailsParams,
    IdListParams,
    SendCaptchaParams,
    UpdateCfgNameParams,
)
from schemas.other_schemas import UpdateBootVolumeCfgParams
from schemas.response import ResponseData
from services.common import run_oci
from services.instance_service import InstanceService
from services.oci_service import OciService

router = APIRouter(prefix="/api/oci", tags=["OCI 管理"])


@router.post("/userPage")
async def user_page(
    params: BasicPageParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    return ResponseData.success(
        data=await OciService.get_user_list(params, db), message="获取用户分页成功"
    )


@router.post("/addCfg")
async def add_cfg(
    username: str = Form(..., min_length=1, max_length=64),
    oci_cfg_str: str = Form(..., alias="ociCfgStr", min_length=1, max_length=64_000),
    file: UploadFile = File(...),
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    content = await file.read(128 * 1024 + 1)
    cfg_id = await OciService.add_cfg(username, oci_cfg_str, content, db)
    return ResponseData.success(data={"id": cfg_id}, message="新增配置成功")


@router.post("/uploadCfg")
async def upload_cfg(
    file_list: list[UploadFile] = File(..., alias="fileList"),
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    if len(file_list) > 100:
        raise OciException(-1, "一次最多导入 100 个配置文件")
    imported: list[str] = []
    failures: list[dict] = []
    for file in file_list:
        try:
            imported.append(
                await OciService.import_cfg_file(
                    file.filename or "config.ini", await file.read(256 * 1024 + 1), db
                )
            )
        except Exception as exc:
            failures.append({"file": file.filename, "error": str(exc)})
    if not imported:
        raise OciException(-1, failures[0]["error"] if failures else "没有可导入的配置")
    return ResponseData.success(
        data={"importedIds": imported, "failures": failures},
        message=f"成功导入 {len(imported)} 个配置",
    )


@router.post("/updateCfgName")
async def update_cfg_name(
    params: UpdateCfgNameParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await OciService.update_cfg_name(params.id, params.username, db)
    return ResponseData.success(message="更新配置名称成功")


@router.post("/removeCfg")
async def remove_cfg(
    params: IdListParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await OciService.remove_cfg(params.id_list, db)
    return ResponseData.success(message="删除配置成功")


@router.post("/details")
async def details(
    params: GetOciCfgDetailsParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    return ResponseData.success(
        data=await OciService.get_cfg_details(params.oci_cfg_id, db),
        message="获取配置详情成功",
    )


@router.post("/createInstance")
async def create_instance(
    params: CreateInstanceParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    task_id = await OciService.create_task(params, db)
    return ResponseData.success(data={"taskId": task_id}, message="创建开机任务成功")


@router.post("/imageOptions")
async def image_options(
    params: GetImageOptionsParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    options = await OciService.get_image_options(
        params.oci_cfg_id,
        params.architecture,
        db,
    )
    return ResponseData.success(data=options, message="获取可用操作系统镜像成功")


@router.post("/availabilityDomainOptions")
async def availability_domain_options(
    params: GetShapeOptionsParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    options = await OciService.get_availability_domain_options(
        params.oci_cfg_id,
        params.architecture,
        db,
    )
    return ResponseData.success(data=options, message="获取可用域成功")


@router.post("/createInstanceBatch")
async def create_instance_batch(
    params: CreateInstanceBatchParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    task_ids: list[str] = []
    failures: list[dict[str, str]] = []
    spec = params.instance_info
    for user_id in params.user_ids:
        try:
            task_ids.append(
                await OciService.create_task(
                    CreateInstanceParams(
                        user_id=user_id,
                        instance_name=spec.instance_name,
                        ocpus=spec.ocpus,
                        memory=spec.memory,
                        disk=spec.disk,
                        boot_volume_vpus_per_gb=spec.boot_volume_vpus_per_gb,
                        architecture=spec.architecture,
                        interval=spec.interval,
                        interval_max=spec.interval_max,
                        max_attempts=spec.max_attempts,
                        availability_domain=spec.availability_domain,
                        create_numbers=spec.create_numbers,
                        operation_system=spec.operation_system,
                        operation_system_version=spec.operation_system_version,
                        ssh_public_key=spec.ssh_public_key,
                    ),
                    db,
                )
            )
        except OciException as exc:
            failures.append({"userId": user_id, "error": exc.message})
    if not task_ids:
        raise OciException(-1, failures[0]["error"] if failures else "没有可提交的配置")
    return ResponseData.success(
        data={"taskIds": task_ids, "failures": failures},
        message=f"已提交 {len(task_ids)} 个开机任务",
    )


@router.post("/createTaskPage")
async def create_task_page(
    params: BasicPageParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    return ResponseData.success(
        data=await OciService.get_create_task_list(params, db),
        message="获取开机任务列表成功",
    )


@router.post("/stopCreate")
async def stop_create(
    params: StopCreateParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    count = await OciService.stop_create_tasks(
        db,
        task_ids=[params.task_id] if params.task_id else None,
        user_id=params.user_id,
    )
    return ResponseData.success(data={"stopped": count}, message="停止开机任务成功")


@router.post("/stopCreateBatch")
async def stop_create_batch(
    params: IdListParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    count = await OciService.stop_create_tasks(db, task_ids=params.id_list)
    return ResponseData.success(data={"stopped": count}, message="停止开机任务成功")


@router.post("/pauseCreateBatch")
async def pause_create_batch(
    params: PauseCreateParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    count = await OciService.pause_create_tasks(params.id_list, db)
    return ResponseData.success(data={"paused": count}, message="暂停开机任务成功")


@router.post("/resumeCreateBatch")
async def resume_create_batch(
    params: PauseCreateParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    count = await OciService.resume_create_tasks(params.id_list, db)
    return ResponseData.success(data={"resumed": count}, message="恢复开机任务成功")


@router.post("/updateInstanceState")
async def update_instance_state(
    params: UpdateInstanceStateParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await OciService.update_instance_state(params, db)
    return ResponseData.success(message="更新实例状态成功")


@router.post("/changeIp")
async def change_ip(
    params: ChangeIpParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    task_id = await OciService.change_ip(params, db)
    return ResponseData.success(data={"taskId": task_id}, message="换 IP 任务已提交")


@router.post("/stopChangeIp")
async def stop_change_ip(
    params: StopChangeIpParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    count = await OciService.stop_change_ip(params.instance_id, db, params.oci_cfg_id)
    return ResponseData.success(data={"stopped": count}, message="停止更换 IP 任务成功")


@router.post("/sendCaptcha")
async def send_captcha(
    params: SendCaptchaParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await OciService.send_captcha(params.oci_cfg_id, params.resource_id, db)
    return ResponseData.success(message="验证码已发送，请查看 Telegram 消息")


@router.post("/terminateInstance")
async def terminate_instance(
    params: TerminateInstanceParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await OciService.terminate_instance(params, db)
    return ResponseData.success(message="终止实例命令已下发")


@router.post("/updateInstanceName")
async def update_instance_name(
    params: UpdateInstanceNameParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await OciService.update_instance_name(params, db)
    return ResponseData.success(message="修改实例名称成功")


@router.post("/releaseSecurityRule")
async def release_security_rule(
    params: GetOciCfgDetailsParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await InstanceService.release_security_rule(params.oci_cfg_id, db)
    return ResponseData.success(message="安全列表放行成功")


@router.post("/getInstanceCfgInfo")
async def get_instance_cfg_info(
    params: GetInstanceCfgInfoParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    data = await InstanceService.get_instance_cfg_info(params.oci_cfg_id, params.instance_id, db)
    return ResponseData.success(data=data, message="获取实例配置成功")


@router.post("/updateInstanceCfg")
async def update_instance_cfg(
    params: UpdateInstanceCfgParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    current = await InstanceService.get_instance_cfg_info(params.oci_cfg_id, params.instance_id, db)
    await InstanceService.update_instance_cfg(
        params.oci_cfg_id,
        params.instance_id,
        float(params.ocpus or current["ocpus"]),
        float(params.memory or current["memoryInGBs"]),
        db,
    )
    return ResponseData.success(message="更新实例配置成功")


@router.post("/updateInstanceShape")
async def update_instance_shape(
    params: UpdateShapeParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await InstanceService.update_instance_shape(
        params.oci_cfg_id,
        params.instance_id,
        params.shape,
        params.ocpus,
        params.memory,
        db,
    )
    return ResponseData.success(message="更新实例 Shape 成功")


@router.post("/createIpv6")
async def create_ipv6(
    params: CreateIpv6Params,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    address = await InstanceService.create_ipv6(params.oci_cfg_id, params.instance_id, db)
    return ResponseData.success(data={"ipv6": address}, message="IPv6 地址创建成功")


@router.post("/updateBootVolumeCfg")
async def update_boot_volume_cfg(
    params: UpdateBootVolumeCfgParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    user = await OciService._get_user(params.oci_cfg_id, db)

    def operation() -> None:
        with OracleInstanceFetcher(
            OciService._build_oci_config(user), user.username or ""
        ) as fetcher:
            fetcher.update_boot_volume_cfg(
                params.boot_volume_id,
                params.size_in_gbs,
                params.vpus_per_gb,
            )

    await run_oci(operation, "更新引导卷配置失败")
    return ResponseData.success(message="更新引导卷配置成功")


@router.post("/startVnc")
async def start_vnc(
    params: StartConsoleConnectionParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    user = await OciService._get_user(params.oci_cfg_id, db)

    def operation() -> str:
        with OracleInstanceFetcher(
            OciService._build_oci_config(user), user.username or ""
        ) as fetcher:
            return fetcher.create_console_connection(params.instance_id, params.public_key)

    url = await run_oci(operation, "创建控制台连接失败")
    return ResponseData.success(data={"vncConnectionString": url}, message="VNC 连接已创建")


@router.post("/checkAlive")
async def check_alive(_user: dict = Depends(require_auth)):
    return ResponseData.success(message="服务正常运行")
