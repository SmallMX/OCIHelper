"""Application use cases for OCI configuration, instances and tasks."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from core.cache import captcha_cache
from core.oracle_fetcher import OciConfig, OracleInstanceFetcher
from core.task_scheduler import scheduler
from enums.architecture import Architecture
from enums.operation_system import OperationSystem
from exceptions import OciException
from models.oci_change_ip_task import OciChangeIpTask
from models.oci_create_task import OciCreateTask
from models.oci_user import OciUser
from schemas.instance_schemas import (
    AvailabilityDomainOptionRsp,
    ChangeIpParams,
    CreateInstanceParams,
    CreateTaskRsp,
    ImageOptionRsp,
    TerminateInstanceParams,
    UpdateInstanceNameParams,
    UpdateInstanceStateParams,
)
from schemas.oci_schemas import (
    BasicPageParams,
    InstanceInfo,
    InstanceVnicInfo,
    OciCfgDetailsRsp,
    OciUserListRsp,
)
from services.common import run_oci, safe_error
from services.notification_service import enqueue_notification
from utils.common import DATETIME_FMT_NORM, generate_id, parse_config_content

ACTIVE_TASK_STATUSES = {"pending", "running", "paused"}


class OciService:
    @staticmethod
    async def add_cfg(
        username: str,
        oci_cfg_str: str,
        key_content: bytes,
        db: AsyncSession,
    ) -> str:
        username = username.strip()
        if not username:
            raise OciException(-1, "配置名称不能为空")
        cfg = parse_config_content(oci_cfg_str)
        for key in ("user", "tenancy", "region", "fingerprint"):
            if not cfg.get(key):
                raise OciException(-1, f"配置文件缺少必要字段: {key}")
        if not key_content or len(key_content) > 128 * 1024:
            raise OciException(-1, "私钥文件为空或超过 128 KiB")

        key_dir = Path(settings.key_dir_path).expanduser().resolve()
        key_dir.mkdir(parents=True, exist_ok=True)
        key_path = key_dir / f"{generate_id()}.pem"
        descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(key_content)

        config = OciConfig(
            user_id=cfg["user"],
            tenant_id=cfg["tenancy"],
            region=cfg["region"],
            fingerprint=cfg["fingerprint"],
            private_key_path=str(key_path),
        )

        def validate() -> None:
            with OracleInstanceFetcher(config, username) as fetcher:
                fetcher.get_availability_domains()

        committed = False
        try:
            await asyncio.to_thread(validate)
            user_id = generate_id()
            db.add(
                OciUser(
                    id=user_id,
                    username=username,
                    oci_user_id=cfg["user"],
                    oci_tenant_id=cfg["tenancy"],
                    oci_region=cfg["region"],
                    oci_fingerprint=cfg["fingerprint"],
                    oci_key_path=str(key_path),
                    create_time=datetime.now(),
                )
            )
            await db.commit()
            committed = True
            logger.info("OCI 配置已添加: {} ({})", username, user_id)
            return user_id
        except OciException:
            raise
        except Exception as exc:
            await db.rollback()
            logger.warning("OCI 配置验证失败: {}", exc)
            raise OciException(-1, f"OCI 配置验证失败: {safe_error(exc)}") from exc
        finally:
            # A committed row owns the key.  Otherwise remove the temporary key.
            if not committed and key_path.exists():
                key_path.unlink(missing_ok=True)

    @staticmethod
    async def import_cfg_file(filename: str, content: bytes, db: AsyncSession) -> str:
        if not filename.lower().endswith((".ini", ".txt", ".conf")):
            raise OciException(-1, "配置文件必须是 .ini、.txt 或 .conf 文本文件")
        if len(content) > 256 * 1024:
            raise OciException(-1, "配置文件超过 256 KiB")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OciException(-1, f"配置文件 {filename} 不是 UTF-8 文本") from exc
        cfg = parse_config_content(text)
        for key in ("user", "tenancy", "region", "fingerprint", "key_file"):
            if not cfg.get(key):
                raise OciException(-1, f"配置文件 {filename} 缺少字段: {key}")

        key_dir = Path(settings.key_dir_path).expanduser().resolve()
        referenced_key = (key_dir / Path(cfg["key_file"]).name).resolve()
        if referenced_key.parent != key_dir or not referenced_key.is_file():
            raise OciException(-1, f"配置引用的私钥不存在: {referenced_key.name}")
        key_content = await asyncio.to_thread(referenced_key.read_bytes)
        username = Path(filename).stem
        return await OciService.add_cfg(username, text, key_content, db)

    @staticmethod
    async def remove_cfg(id_list: list[str], db: AsyncSession) -> None:
        result = await db.execute(select(OciUser).where(OciUser.id.in_(id_list)))
        users = list(result.scalars())
        task_result = await db.execute(
            select(OciCreateTask).where(
                OciCreateTask.user_id.in_(id_list),
                OciCreateTask.status.in_(ACTIVE_TASK_STATUSES),
            )
        )
        create_tasks = list(task_result.scalars())
        change_result = await db.execute(
            select(OciChangeIpTask).where(
                OciChangeIpTask.user_id.in_(id_list),
                OciChangeIpTask.status.in_(ACTIVE_TASK_STATUSES),
            )
        )
        change_tasks = list(change_result.scalars())

        for task in create_tasks:
            scheduler.stop_create_task(task.id)
            task.status = "cancelled"
            task.updated_at = datetime.now()
        for task in change_tasks:
            scheduler.stop_change_ip_task(task.user_id, task.instance_id)
            task.status = "cancelled"
            task.updated_at = datetime.now()
        for user in users:
            await db.delete(user)
        await db.commit()

        for user in users:
            try:
                key_path = Path(user.oci_key_path).resolve()
                key_dir = Path(settings.key_dir_path).expanduser().resolve()
                if key_path.parent == key_dir:
                    key_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("清理已删除配置的私钥失败: {}", exc)

    @staticmethod
    async def update_cfg_name(cfg_id: str, new_name: str, db: AsyncSession) -> None:
        user = await OciService._get_user(cfg_id, db)
        cleaned_name = new_name.strip()
        if not cleaned_name:
            raise OciException(-1, "配置名称不能为空")
        user.username = cleaned_name
        await db.commit()

    @staticmethod
    async def get_user_list(params: BasicPageParams, db: AsyncSession) -> dict[str, Any]:
        query = select(OciUser)
        count_query = select(func.count()).select_from(OciUser)
        if params.keyword:
            condition = OciUser.username.contains(params.keyword.strip())
            query = query.where(condition)
            count_query = count_query.where(condition)
        total = (await db.execute(count_query)).scalar() or 0
        users = list(
            (
                await db.execute(
                    query.order_by(OciUser.create_time.desc(), OciUser.id)
                    .offset(params.offset)
                    .limit(params.page_size)
                )
            ).scalars()
        )
        active_ids = {
            row[0]
            for row in (
                await db.execute(
                    select(OciCreateTask.user_id)
                    .where(OciCreateTask.status.in_(ACTIVE_TASK_STATUSES))
                    .distinct()
                )
            ).all()
        }
        records = [
            OciUserListRsp(
                id=user.id,
                username=user.username,
                tenant_name=user.tenant_name,
                region=user.oci_region,
                region_name=user.oci_region,
                create_time=user.create_time.strftime(DATETIME_FMT_NORM),
                enable_create=1 if user.id in active_ids else 0,
            ).model_dump(by_alias=True)
            for user in users
        ]
        return _page(records, total, params)

    @staticmethod
    async def get_cfg_details(oci_cfg_id: str, db: AsyncSession) -> dict:
        user = await OciService._get_user(oci_cfg_id, db)
        config = OciService._build_oci_config(user)

        def fetch_details() -> list[dict]:
            instances: list[dict] = []
            with OracleInstanceFetcher(config, user.username or "") as fetcher:
                for instance in fetcher.list_instances():
                    vnics: list[dict] = []
                    for attachment in fetcher.list_vnic_attachments(instance.id):
                        if attachment.lifecycle_state != "ATTACHED":
                            continue
                        try:
                            vnic = fetcher.get_vnic(attachment.vnic_id)
                            vnics.append(
                                InstanceVnicInfo(
                                    vnic_id=attachment.vnic_id, name=vnic.display_name
                                ).model_dump(by_alias=True)
                            )
                        except Exception as exc:
                            logger.warning(
                                "读取 VNIC 失败: instance={}, error={}", instance.id, exc
                            )
                    shape_config = instance.shape_config
                    instances.append(
                        InstanceInfo(
                            oc_id=instance.id,
                            region=getattr(instance, "region", None) or user.oci_region,
                            name=instance.display_name,
                            public_ip=fetcher.list_public_ips(instance.id),
                            shape=instance.shape,
                            enable_change_ip=int(
                                scheduler.is_change_ip_running(user.id, instance.id)
                            ),
                            ocpus=_number_string(getattr(shape_config, "ocpus", None)),
                            memory=_number_string(getattr(shape_config, "memory_in_gbs", None)),
                            create_time=str(instance.time_created)
                            if instance.time_created
                            else None,
                            state=instance.lifecycle_state,
                            availability_domain=instance.availability_domain,
                            vnic_list=vnics,
                        ).model_dump(by_alias=True)
                    )
            return instances

        instances = await run_oci(fetch_details, "获取 OCI 资源失败")

        return OciCfgDetailsRsp(
            user_id=user.oci_user_id,
            tenant_id=user.oci_tenant_id,
            fingerprint=user.oci_fingerprint,
            private_key_path=None,
            region=user.oci_region,
            instance_list=instances,
            nlb_list=[],
        ).model_dump(by_alias=True)

    @staticmethod
    async def get_image_options(
        oci_cfg_id: str,
        architecture: str,
        db: AsyncSession,
    ) -> list[dict[str, str]]:
        user = await OciService._get_user(oci_cfg_id, db)
        shape = Architecture.get_shape_by_type(architecture)

        def operation() -> list[dict[str, str]]:
            with OracleInstanceFetcher(
                OciService._build_oci_config(user),
                user.username or "",
            ) as fetcher:
                return fetcher.list_image_options(shape)

        options = await run_oci(operation, "获取可用操作系统镜像失败")
        return [
            ImageOptionRsp.model_validate(option).model_dump(by_alias=True)
            for option in options
        ]

    @staticmethod
    async def get_availability_domain_options(
        oci_cfg_id: str,
        architecture: str,
        db: AsyncSession,
    ) -> list[dict[str, str]]:
        user = await OciService._get_user(oci_cfg_id, db)
        shape = Architecture.get_shape_by_type(architecture)

        def operation() -> list:
            with OracleInstanceFetcher(
                OciService._build_oci_config(user),
                user.username or "",
            ) as fetcher:
                return fetcher.list_supported_availability_domains(shape)

        domains = await run_oci(operation, "获取可用域失败")
        return [
            AvailabilityDomainOptionRsp(
                name=domain.name,
                label=domain.name,
            ).model_dump(by_alias=True)
            for domain in domains
        ]

    @staticmethod
    async def create_task(params: CreateInstanceParams, db: AsyncSession) -> str:
        user = await OciService._get_user(params.user_id, db)
        existing = await db.execute(
            select(OciCreateTask.id).where(
                OciCreateTask.user_id == user.id,
                OciCreateTask.status.in_(ACTIVE_TASK_STATUSES),
            )
        )
        if existing.first():
            raise OciException(-1, "该 OCI 配置已有创建任务，请先停止原任务")

        task_id = generate_id()
        shape = Architecture.get_shape_by_type(params.architecture)
        operating_system, operating_system_version = OperationSystem.resolve(
            params.operation_system,
            params.operation_system_version,
        )

        def validate_selection() -> None:
            with OracleInstanceFetcher(
                OciService._build_oci_config(user),
                user.username or "",
            ) as fetcher:
                fetcher.validate_image_selection(
                    shape,
                    operating_system,
                    operating_system_version,
                )
                if params.availability_domain:
                    fetcher.validate_availability_domain_selection(
                        shape,
                        params.availability_domain,
                    )

        await run_oci(validate_selection, "所选实例配置不可用")
        fixed_shape = not shape.endswith(".Flex")
        task = OciCreateTask(
            id=task_id,
            user_id=user.id,
            oci_region=user.oci_region,
            instance_name=params.instance_name,
            ocpus=1.0 if fixed_shape else float(params.ocpus),
            memory=1.0 if fixed_shape else float(params.memory),
            disk=params.disk,
            boot_volume_vpus_per_gb=params.boot_volume_vpus_per_gb,
            architecture=params.architecture,
            interval=params.interval,
            interval_max=params.interval_max or params.interval,
            availability_domain=params.availability_domain,
            availability_domain_index=0,
            create_numbers=params.create_numbers,
            initial_create_numbers=params.create_numbers,
            operation_system=operating_system,
            operation_system_version=operating_system_version,
            ssh_public_key=params.ssh_public_key,
            paused=0,
            status="pending",
            attempts=0,
            max_attempts=(
                settings.create_task_max_attempts
                if params.max_attempts is None
                else params.max_attempts
            ),
            create_time=datetime.now(),
            updated_at=datetime.now(),
        )
        db.add(task)
        try:
            await enqueue_notification(
                db,
                event_key=f"create-task-created:{task_id}",
                category="create-task-created",
                message=_build_create_task_created_message(task, user, shape),
            )
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise OciException(-1, "该 OCI 配置已有创建任务，请先停止原任务") from exc
        try:
            OciService.schedule_create_task(task_id, params.interval)
        except Exception as exc:
            task.status = "failed"
            task.last_error = "task scheduler is unavailable"
            task.updated_at = datetime.now()
            await db.commit()
            raise OciException(-1, "任务调度器不可用，请重启服务") from exc
        logger.info(
            "开机任务已创建: task={}, user={}, region={}, name={}, ad={}, architecture={}, boot_vpu={}, count={}, interval={}-{}s, max_attempts={}",
            task_id,
            user.username,
            user.oci_region,
            params.instance_name or "auto",
            params.availability_domain or "auto",
            params.architecture,
            params.boot_volume_vpus_per_gb,
            params.create_numbers,
            params.interval,
            params.interval_max or params.interval,
            task.max_attempts or "unlimited",
        )
        return task_id

    @staticmethod
    def schedule_create_task(
        task_id: str,
        interval: int,
        paused: bool = False,
        initial_delay: float = 0,
    ) -> None:
        from services.instance_service import InstanceService

        scheduler.submit_create_task(
            task_id,
            lambda: asyncio.run(InstanceService.execute_create_task(task_id)),
            interval,
            paused=paused,
            initial_delay=initial_delay,
        )

    @staticmethod
    async def get_create_task_list(params: BasicPageParams, db: AsyncSession) -> dict[str, Any]:
        query = select(OciCreateTask, OciUser.username).outerjoin(
            OciUser, OciCreateTask.user_id == OciUser.id
        )
        count_query = (
            select(func.count())
            .select_from(OciCreateTask)
            .outerjoin(OciUser, OciCreateTask.user_id == OciUser.id)
        )
        if params.keyword:
            condition = OciUser.username.contains(params.keyword.strip())
            query = query.where(condition)
            count_query = count_query.where(condition)
        total = (await db.execute(count_query)).scalar() or 0
        rows = (
            await db.execute(
                query.order_by(OciCreateTask.create_time.desc(), OciCreateTask.id)
                .offset(params.offset)
                .limit(params.page_size)
            )
        ).all()
        records = [
            CreateTaskRsp(
                id=task.id,
                username=username or task.user_id,
                region=task.oci_region,
                instance_name=task.instance_name,
                ocpus=_number_string(task.ocpus),
                memory=_number_string(task.memory),
                disk=task.disk,
                boot_volume_vpus_per_gb=task.boot_volume_vpus_per_gb,
                architecture=task.architecture,
                interval=task.interval,
                interval_max=task.interval_max,
                max_attempts=task.max_attempts,
                availability_domain=task.availability_domain,
                create_numbers=task.create_numbers,
                operation_system=task.operation_system,
                operation_system_version=task.operation_system_version,
                create_time=task.create_time.strftime(DATETIME_FMT_NORM),
                counts=str(task.attempts),
                paused=task.paused,
                status=task.status,
                last_error=task.last_error,
            ).model_dump(by_alias=True)
            for task, username in rows
        ]
        return _page(records, total, params)

    @staticmethod
    async def stop_create_tasks(
        db: AsyncSession,
        *,
        task_ids: list[str] | None = None,
        user_id: str | None = None,
    ) -> int:
        query = select(OciCreateTask).where(OciCreateTask.status.in_(ACTIVE_TASK_STATUSES))
        if task_ids:
            query = query.where(OciCreateTask.id.in_(task_ids))
        elif user_id:
            query = query.where(OciCreateTask.user_id == user_id)
        else:
            raise OciException(-1, "缺少任务标识")
        tasks = list((await db.execute(query)).scalars())
        for task in tasks:
            scheduler.stop_create_task(task.id)
            task.status = "cancelled"
            task.paused = 0
            task.updated_at = datetime.now()
        await db.commit()
        return len(tasks)

    @staticmethod
    async def pause_create_tasks(task_ids: list[str], db: AsyncSession) -> int:
        tasks = list(
            (
                await db.execute(
                    select(OciCreateTask).where(
                        OciCreateTask.id.in_(task_ids),
                        OciCreateTask.status.in_({"pending", "running"}),
                    )
                )
            ).scalars()
        )
        for task in tasks:
            scheduler.pause_create_task(task.id)
            task.paused = 1
            task.status = "paused"
            task.next_run_at = None
            task.updated_at = datetime.now()
        await db.commit()
        return len(tasks)

    @staticmethod
    async def resume_create_tasks(task_ids: list[str], db: AsyncSession) -> int:
        tasks = list(
            (
                await db.execute(
                    select(OciCreateTask).where(
                        OciCreateTask.id.in_(task_ids), OciCreateTask.status == "paused"
                    )
                )
            ).scalars()
        )
        for task in tasks:
            task.paused = 0
            task.status = "pending"
            task.next_run_at = datetime.now()
            task.updated_at = datetime.now()
            if not scheduler.resume_create_task(task.id):
                OciService.schedule_create_task(task.id, task.interval)
        await db.commit()
        return len(tasks)

    @staticmethod
    async def update_instance_state(params: UpdateInstanceStateParams, db: AsyncSession) -> None:
        user = await OciService._get_user(params.oci_cfg_id, db)

        def operation() -> None:
            with OracleInstanceFetcher(
                OciService._build_oci_config(user), user.username or ""
            ) as fetcher:
                fetcher.instance_action(params.instance_id, params.action)

        await run_oci(operation, "更新实例状态失败")

    @staticmethod
    async def update_instance_name(params: UpdateInstanceNameParams, db: AsyncSession) -> None:
        user = await OciService._get_user(params.oci_cfg_id, db)

        def operation() -> None:
            with OracleInstanceFetcher(
                OciService._build_oci_config(user), user.username or ""
            ) as fetcher:
                fetcher.update_instance_name(params.instance_id, params.name)

        await run_oci(operation, "更新实例名称失败")

    @staticmethod
    async def change_ip(params: ChangeIpParams, db: AsyncSession) -> str:
        user = await OciService._get_user(params.oci_cfg_id, db)
        existing = await db.execute(
            select(OciChangeIpTask.id).where(
                OciChangeIpTask.user_id == user.id,
                OciChangeIpTask.instance_id == params.instance_id,
                OciChangeIpTask.status.in_(ACTIVE_TASK_STATUSES),
            )
        )
        if existing.first() or scheduler.is_change_ip_running(user.id, params.instance_id):
            raise OciException(-1, "该实例已有换 IP 任务在运行")

        task_id = generate_id()
        task = OciChangeIpTask(
            id=task_id,
            user_id=user.id,
            instance_id=params.instance_id,
            vnic_id=params.vnic_id,
            cidr_list=json.dumps(params.cidr_list, ensure_ascii=False)
            if params.cidr_list
            else None,
            interval=settings.change_ip_interval_seconds,
            status="pending",
            attempts=0,
            max_attempts=1 if not params.cidr_list else settings.change_ip_max_attempts,
            create_time=datetime.now(),
            updated_at=datetime.now(),
        )
        db.add(task)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise OciException(-1, "该实例已有换 IP 任务在运行") from exc
        try:
            OciService.schedule_change_ip_task(task)
        except Exception as exc:
            task.status = "failed"
            task.last_error = "task scheduler is unavailable"
            task.updated_at = datetime.now()
            await db.commit()
            raise OciException(-1, "任务调度器不可用，请重启服务") from exc
        return task_id

    @staticmethod
    def schedule_change_ip_task(task: OciChangeIpTask) -> None:
        from services.instance_service import InstanceService

        scheduler.submit_change_ip_task(
            task.user_id,
            task.instance_id,
            lambda: asyncio.run(InstanceService.execute_change_ip_task(task.id)),
            task.interval,
        )

    @staticmethod
    async def stop_change_ip(
        instance_id: str, db: AsyncSession, oci_cfg_id: str | None = None
    ) -> int:
        query = select(OciChangeIpTask).where(
            OciChangeIpTask.instance_id == instance_id,
            OciChangeIpTask.status.in_(ACTIVE_TASK_STATUSES),
        )
        if oci_cfg_id:
            query = query.where(OciChangeIpTask.user_id == oci_cfg_id)
        tasks = list((await db.execute(query)).scalars())
        for task in tasks:
            scheduler.stop_change_ip_task(task.user_id, task.instance_id)
            task.status = "cancelled"
            task.updated_at = datetime.now()
        await db.commit()
        return len(tasks)

    @staticmethod
    async def send_captcha(
        oci_cfg_id: str,
        resource_id: str | None,
        db: AsyncSession,
    ) -> None:
        user = await OciService._get_user(oci_cfg_id, db)
        from telegram_bot import get_bot

        bot = get_bot()
        if bot is None:
            raise OciException(-1, "请先配置 Telegram Bot 后再执行破坏性操作")
        captcha = f"{secrets.randbelow(1_000_000):06d}"
        captcha_cache.put(_captcha_key(oci_cfg_id, resource_id), captcha, ttl=300)
        sent = await bot.send_message(
            "【验证码】\n\n"
            f"用户：[{user.username or user.id}]\n区域：{user.oci_region}\n"
            f"验证码：{captcha}\n有效期：5 分钟"
        )
        if not sent:
            captcha_cache.remove(_captcha_key(oci_cfg_id, resource_id))
            raise OciException(-1, "Telegram 验证码发送失败")

    @staticmethod
    def verify_captcha(oci_cfg_id: str, resource_id: str | None, captcha: str) -> bool:
        keys = [_captcha_key(oci_cfg_id, resource_id)]
        if resource_id:
            keys.append(_captcha_key(oci_cfg_id, None))
        for key in keys:
            cached = captcha_cache.get(key)
            if cached and hmac.compare_digest(str(cached), captcha):
                captcha_cache.remove(key)
                return True
        return False

    @staticmethod
    async def terminate_instance(params: TerminateInstanceParams, db: AsyncSession) -> None:
        if not OciService.verify_captcha(params.oci_cfg_id, params.instance_id, params.captcha):
            raise OciException(-1, "验证码错误或已过期")
        user = await OciService._get_user(params.oci_cfg_id, db)

        def operation() -> None:
            with OracleInstanceFetcher(
                OciService._build_oci_config(user), user.username or ""
            ) as fetcher:
                fetcher.terminate_instance(params.instance_id, params.preserve_boot_volume == 1)

        await run_oci(operation, "终止实例失败")

    @staticmethod
    async def _get_user(oci_cfg_id: str, db: AsyncSession) -> OciUser:
        user = await db.get(OciUser, oci_cfg_id)
        if user is None:
            raise OciException(-1, "用户配置不存在")
        return user

    @staticmethod
    def _build_oci_config(user: OciUser) -> OciConfig:
        return OciConfig(
            user_id=user.oci_user_id,
            tenant_id=user.oci_tenant_id,
            region=user.oci_region,
            fingerprint=user.oci_fingerprint,
            private_key_path=user.oci_key_path,
        )


def _captcha_key(oci_cfg_id: str, resource_id: str | None) -> str:
    return f"captcha:{oci_cfg_id}:{resource_id or '*'}"


def _number_string(value: object) -> str | None:
    if value is None:
        return None
    try:
        number = float(value)
        return str(int(number)) if number.is_integer() else str(number)
    except (TypeError, ValueError):
        return str(value)


def _build_create_task_created_message(
    task: OciCreateTask,
    user: OciUser,
    shape: str,
) -> str:
    interval = f"{task.interval} 秒"
    if task.interval_max != task.interval:
        interval = f"{task.interval}–{task.interval_max} 秒"
    max_attempts = "不限次数" if task.max_attempts == 0 else f"{task.max_attempts} 次"
    operation_system = " ".join(
        item for item in (task.operation_system, task.operation_system_version) if item
    )
    return (
        "【开机任务创建成功】\n\n"
        f"任务 ID：{task.id}\n"
        f"配置：[{user.username or user.id}]\n"
        f"区域：{task.oci_region or user.oci_region}\n"
        f"实例名称：{task.instance_name or '自动生成'}\n"
        f"可用域：{task.availability_domain or '自动轮换'}\n"
        f"规格：{task.architecture} · {shape} · "
        f"{_number_string(task.ocpus)} OCPU / {_number_string(task.memory)} GB / "
        f"{task.disk} GB / {task.boot_volume_vpus_per_gb} VPU/GB\n"
        f"系统：{operation_system}\n"
        f"创建数量：{task.create_numbers}\n"
        f"重试间隔：{interval}，最大尝试：{max_attempts}\n"
        "状态：等待执行"
    )


def _page(records: list, total: int, params: BasicPageParams) -> dict:
    return {
        "records": records,
        "total": total,
        "current": params.current_page,
        "size": params.page_size,
    }
