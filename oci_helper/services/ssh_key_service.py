"""Persistence and one-time key-pair generation for SSH public keys."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.ssh_keys import generate_ed25519_key_pair, ssh_public_key_fingerprint
from exceptions import OciException
from models.ssh_public_key import SshPublicKey
from schemas.ssh_key_schemas import (
    AddSshPublicKeyParams,
    RenameSshPublicKeyParams,
    SshPublicKeyRsp,
)
from utils.common import DATETIME_FMT_NORM, generate_id


class SshKeyService:
    @staticmethod
    async def list_keys(db: AsyncSession) -> list[dict]:
        result = await db.execute(
            select(SshPublicKey).order_by(SshPublicKey.create_time.desc(), SshPublicKey.id.desc())
        )
        return [SshKeyService._serialize(item) for item in result.scalars()]

    @staticmethod
    async def add_key(params: AddSshPublicKeyParams, db: AsyncSession) -> dict:
        item = await SshKeyService._create_key(params.name, params.public_key, db)
        return SshKeyService._serialize(item)

    @staticmethod
    async def rename_key(params: RenameSshPublicKeyParams, db: AsyncSession) -> dict:
        result = await db.execute(select(SshPublicKey).where(SshPublicKey.id == params.id))
        item = result.scalar_one_or_none()
        if item is None:
            raise OciException(-1, "SSH 公钥不存在")

        name_owner = await db.execute(
            select(SshPublicKey.id).where(
                SshPublicKey.name == params.name,
                SshPublicKey.id != params.id,
            )
        )
        if name_owner.first():
            raise OciException(-1, "SSH 公钥名称已存在")

        item.name = params.name
        item.update_time = datetime.now()
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise OciException(-1, "SSH 公钥名称已存在") from exc
        return SshKeyService._serialize(item)

    @staticmethod
    async def generate_key_pair(name: str, db: AsyncSession) -> bytes:
        public_key, archive = generate_ed25519_key_pair(name)
        await SshKeyService._create_key(name, public_key, db)
        return archive

    @staticmethod
    async def _create_key(name: str, public_key: str, db: AsyncSession) -> SshPublicKey:
        fingerprint = ssh_public_key_fingerprint(public_key)
        name_owner = await db.execute(select(SshPublicKey.id).where(SshPublicKey.name == name))
        if name_owner.first():
            raise OciException(-1, "SSH 公钥名称已存在")
        key_owner = await db.execute(
            select(SshPublicKey.id).where(SshPublicKey.fingerprint == fingerprint)
        )
        if key_owner.first():
            raise OciException(-1, "SSH 公钥已存在")

        now = datetime.now()
        item = SshPublicKey(
            id=generate_id(),
            name=name,
            public_key=public_key,
            fingerprint=fingerprint,
            create_time=now,
            update_time=now,
        )
        db.add(item)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise OciException(-1, "SSH 公钥名称或内容已存在") from exc
        return item

    @staticmethod
    def _serialize(item: SshPublicKey) -> dict:
        return SshPublicKeyRsp(
            id=item.id,
            name=item.name,
            public_key=item.public_key,
            fingerprint=item.fingerprint,
            create_time=item.create_time.strftime(DATETIME_FMT_NORM),
            update_time=item.update_time.strftime(DATETIME_FMT_NORM),
        ).model_dump(by_alias=True)
