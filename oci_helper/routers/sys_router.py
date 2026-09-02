"""System API routes."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import require_auth
from database import get_db
from exceptions import OciException
from schemas.response import ResponseData
from schemas.sys_schemas import (
    GetTaskLogsParams,
    LoginParams,
    SendMsgParams,
    UpdateAdminCredentialsParams,
    UpdateSysCfgParams,
)
from services.sys_service import SysService
from utils.common import get_client_ip

router = APIRouter(prefix="/api/sys", tags=["系统管理"])


@router.post("/login")
async def login(
    params: LoginParams,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    result = await SysService.login(params, get_client_ip(request), db)
    return ResponseData.success(data=result.model_dump(by_alias=True), message="登录成功")


@router.post("/getSysCfg")
async def get_sys_cfg(
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    result = await SysService.get_sys_cfg(db)
    return ResponseData.success(data=result.model_dump(by_alias=True), message="获取系统配置成功")


@router.post("/updateSysCfg")
async def update_sys_cfg(
    params: UpdateSysCfgParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await SysService.update_sys_cfg(params, db)
    return ResponseData.success(message="更新系统配置成功")


@router.post("/updateAdminCredentials")
async def update_admin_credentials(
    params: UpdateAdminCredentialsParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    await SysService.update_admin_credentials(params, db)
    return ResponseData.success(message="管理员账号已更新，请重新登录")


@router.post("/sendMsg")
async def send_msg(
    params: SendMsgParams,
    _user: dict = Depends(require_auth),
):
    from telegram_bot import send_notification

    if not await send_notification(params.message):
        raise OciException(-1, "Telegram 消息发送失败，请检查 Bot 配置")
    return ResponseData.success(message="发送消息成功")


@router.get("/glance")
async def glance(
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    result = await SysService.glance(db)
    return ResponseData.success(data=result.model_dump(by_alias=True), message="获取仪表盘数据成功")


@router.post("/taskLogs")
async def task_logs(
    params: GetTaskLogsParams,
    _user: dict = Depends(require_auth),
):
    result = await SysService.get_task_logs(params)
    return ResponseData.success(data=result.model_dump(by_alias=True), message="获取任务日志成功")
