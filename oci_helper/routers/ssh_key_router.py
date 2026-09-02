"""Authenticated SSH public-key management routes."""

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import require_auth
from database import get_db
from schemas.response import ResponseData
from schemas.ssh_key_schemas import (
    AddSshPublicKeyParams,
    GenerateSshKeyPairParams,
    RenameSshPublicKeyParams,
)
from services.ssh_key_service import SshKeyService

router = APIRouter(prefix="/api/sshKey", tags=["SSH 公钥管理"])


@router.get("/list")
async def list_keys(
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    return ResponseData.success(data=await SshKeyService.list_keys(db), message="获取 SSH 公钥成功")


@router.post("/add")
async def add_key(
    params: AddSshPublicKeyParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    return ResponseData.success(data=await SshKeyService.add_key(params, db), message="添加 SSH 公钥成功")


@router.post("/rename")
async def rename_key(
    params: RenameSshPublicKeyParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    return ResponseData.success(
        data=await SshKeyService.rename_key(params, db),
        message="修改 SSH 公钥名称成功",
    )


@router.post("/generate")
async def generate_key_pair(
    params: GenerateSshKeyPairParams,
    _user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    archive = await SshKeyService.generate_key_pair(params.name, db)
    return Response(
        content=archive,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="oci-helper-ssh-key.zip"',
            "Cache-Control": "no-store",
        },
    )
