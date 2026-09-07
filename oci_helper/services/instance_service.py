"""Instance use cases and safe background task executions."""

from __future__ import annotations

import ipaddress
import json
import random
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from core.oracle_fetcher import (
    OracleInstanceFetcher,
    is_oci_helper_managed,
    is_retryable_oci_error,
)
from core.task_scheduler import TaskExecutionResult
from database import async_session
from enums.architecture import Architecture
from exceptions import OciException
from models.oci_change_ip_task import OciChangeIpTask
from models.oci_create_task import OciCreateTask
from models.oci_user import OciUser
from services.common import run_oci
from services.notification_service import enqueue_notification
from utils.common import get_time_difference

CREATE_SUCCESS_MESSAGE = (
    "【开机任务】\n\n用户：[{username}] {outcome}\n"
    "时间：{time}\n状态：{lifecycle_state}\n实例名称：{display_name}\nRegion：{region}\n"
    "可用域：{availability_domain}\nCPU类型：{architecture}\n"
    "CPU：{ocpus}\n内存（GB）：{memory}\n磁盘大小（GB）：{disk}\n"
    "引导卷性能：{boot_volume_vpus_per_gb} VPU/GB\n"
    "Shape：{shape}\n专用 IPv4：{private_ip}\n公网 IPv4：{public_ip}\n"
    "任务尝试次数：{count}\n任务累计耗时：{duration}"
)


class InstanceService:
    @staticmethod
    async def execute_create_task(task_id: str) -> TaskExecutionResult:
        """Execute one bounded creation attempt and persist its outcome."""
        async with async_session() as db:
            task = await db.get(OciCreateTask, task_id)
            if task is None or task.status in {"succeeded", "failed", "cancelled"}:
                return TaskExecutionResult(done=True)
            if task.paused or task.status == "paused":
                return TaskExecutionResult(next_delay=task.interval)
            if task.max_attempts > 0 and task.attempts >= task.max_attempts:
                task.status = "failed"
                task.last_error = "maximum attempt count reached"
                task.next_run_at = None
                task.updated_at = datetime.now()
                await db.commit()
                return TaskExecutionResult(done=True)

            user = await db.get(OciUser, task.user_id)
            if user is None:
                task.status = "failed"
                task.last_error = "OCI configuration no longer exists"
                task.next_run_at = None
                task.updated_at = datetime.now()
                await db.commit()
                return TaskExecutionResult(done=True)

            ssh_public_key = (task.ssh_public_key or "").strip()
            if not ssh_public_key:
                task.status = "failed"
                task.last_error = "SSH public key is required"
                task.next_run_at = None
                task.updated_at = datetime.now()
                await db.commit()
                return TaskExecutionResult(done=True)

            task.attempts += 1
            task.status = "running"
            task.last_error = None
            task.next_run_at = None
            task.updated_at = datetime.now()
            await db.commit()

            snapshot = {
                "task_id": task.id,
                "attempt": task.attempts,
                "create_time": task.create_time,
                "remaining": task.create_numbers,
                "retry_token_seed": f"{task.id}:{task.create_numbers}",
                "instance_name": _build_instance_display_name(
                    task.instance_name,
                    task.initial_create_numbers,
                    task.create_numbers,
                ),
                "architecture": task.architecture,
                "availability_domain": task.availability_domain,
                "availability_domain_index": task.availability_domain_index,
                "ocpus": task.ocpus,
                "memory": task.memory,
                "disk": task.disk,
                "boot_volume_vpus_per_gb": task.boot_volume_vpus_per_gb,
                "operation_system": task.operation_system,
                "operation_system_version": task.operation_system_version,
                "ssh_public_key": ssh_public_key,
                "username": user.username or "",
                "oci_config": _build_oci_config(user),
            }

        results: list[dict] = []
        try:
            with OracleInstanceFetcher(snapshot["oci_config"], snapshot["username"]) as fetcher:
                fetcher.configure_instance_creation(
                    architecture=snapshot["architecture"],
                    ocpus=float(snapshot["ocpus"]),
                    memory=float(snapshot["memory"]),
                    disk=int(snapshot["disk"]),
                    boot_volume_vpus_per_gb=int(snapshot["boot_volume_vpus_per_gb"]),
                    operation_system=snapshot["operation_system"],
                    operation_system_version=snapshot["operation_system_version"],
                    create_numbers=snapshot["remaining"],
                    ssh_public_key=snapshot["ssh_public_key"],
                    instance_name=snapshot["instance_name"],
                    availability_domain=snapshot["availability_domain"],
                    availability_domain_index=snapshot["availability_domain_index"],
                    retry_token_seed=snapshot["retry_token_seed"],
                )
                results.append(fetcher.create_instance_data())
        except Exception as exc:
            logger.exception("创建任务初始化失败: task_id={}", task_id)
            results.append(
                {
                    "success": False,
                    "retryable": is_retryable_oci_error(exc),
                    "error": str(exc),
                }
            )

        success_results = [item for item in results if item.get("success")]
        failure = next((item for item in results if not item.get("success")), None)
        next_delay: int | None = None
        async with async_session() as db:
            task = await db.get(OciCreateTask, task_id)
            if task is None:
                await _enqueue_create_success_notifications(db, snapshot, success_results)
                await db.commit()
                return TaskExecutionResult(done=True)
            if task.status == "cancelled":
                task.create_numbers = max(0, task.create_numbers - len(success_results))
                task.updated_at = datetime.now()
                await _enqueue_create_success_notifications(db, snapshot, success_results)
                await db.commit()
                return TaskExecutionResult(done=True)

            task.create_numbers = max(0, task.create_numbers - len(success_results))
            should_rotate_domain = bool(success_results) or bool(
                failure and failure.get("rotate_availability_domain")
            )
            if task.availability_domain is None and should_rotate_domain:
                task.availability_domain_index += 1
            task.updated_at = datetime.now()
            if task.create_numbers == 0:
                task.status = "succeeded"
                task.last_error = None
                task.next_run_at = None
                done = True
            elif failure and not failure.get("retryable", False):
                task.status = "failed"
                task.last_error = _safe_error(failure.get("error"))
                task.next_run_at = None
                done = True
            elif task.max_attempts > 0 and task.attempts >= task.max_attempts:
                task.status = "failed"
                task.last_error = "maximum attempt count reached"
                task.next_run_at = None
                done = True
            elif task.paused or task.status == "paused":
                task.paused = 1
                task.status = "paused"
                task.last_error = _safe_error(failure.get("error")) if failure else None
                task.next_run_at = None
                done = False
            else:
                task.status = "pending"
                task.last_error = _safe_error(failure.get("error")) if failure else None
                next_delay = _random_retry_delay(task.interval, task.interval_max)
                task.next_run_at = datetime.now() + timedelta(seconds=next_delay)
                done = False
            await _enqueue_create_success_notifications(db, snapshot, success_results)
            await db.commit()

        return TaskExecutionResult(done=done, next_delay=next_delay)

    @staticmethod
    async def execute_change_ip_task(task_id: str) -> TaskExecutionResult:
        async with async_session() as db:
            task = await db.get(OciChangeIpTask, task_id)
            if task is None or task.status in {"succeeded", "failed", "cancelled"}:
                return TaskExecutionResult(done=True)
            if task.attempts >= task.max_attempts:
                task.status = "failed"
                task.last_error = "maximum attempt count reached"
                task.updated_at = datetime.now()
                await db.commit()
                return TaskExecutionResult(done=True)
            user = await db.get(OciUser, task.user_id)
            if user is None:
                task.status = "failed"
                task.last_error = "OCI configuration no longer exists"
                task.updated_at = datetime.now()
                await db.commit()
                return TaskExecutionResult(done=True)

            task.attempts += 1
            task.status = "running"
            task.last_error = None
            task.updated_at = datetime.now()
            await db.commit()
            snapshot = {
                "task_id": task.id,
                "cfg_id": user.id,
                "username": user.username or "",
                "region": user.oci_region,
                "instance_id": task.instance_id,
                "vnic_id": task.vnic_id,
                "cidrs": json.loads(task.cidr_list) if task.cidr_list else [],
                "interval": task.interval,
                "attempts": task.attempts,
                "max_attempts": task.max_attempts,
                "oci_config": _build_oci_config(user),
            }

        public_ip = None
        instance_name = snapshot["instance_id"]
        error = None
        retryable = True
        try:
            with OracleInstanceFetcher(snapshot["oci_config"], snapshot["username"]) as fetcher:
                instance = fetcher.get_instance_by_id(snapshot["instance_id"])
                instance_name = instance.display_name
                if not any(
                    attachment.vnic_id == snapshot["vnic_id"]
                    and attachment.lifecycle_state == "ATTACHED"
                    for attachment in fetcher.list_vnic_attachments(snapshot["instance_id"])
                ):
                    raise ValueError("指定 VNIC 未挂载到目标实例，已停止换 IP")
                vnic = fetcher.get_vnic(snapshot["vnic_id"])
                public_ip = fetcher.reassign_ephemeral_public_ip(vnic)
        except Exception as exc:
            error = _safe_error(str(exc))
            retryable = is_retryable_oci_error(exc)
            logger.exception(
                "更换公网 IP 失败: user={}, instance={}",
                snapshot["username"],
                snapshot["instance_id"],
            )

        matched = bool(public_ip) and (
            not snapshot["cidrs"]
            or any(
                ipaddress.ip_address(public_ip) in ipaddress.ip_network(cidr)
                for cidr in snapshot["cidrs"]
            )
        )
        exhausted = snapshot["attempts"] >= snapshot["max_attempts"]

        async with async_session() as db:
            task = await db.get(OciChangeIpTask, task_id)
            if task is None or task.status == "cancelled":
                if matched:
                    await _enqueue_change_ip_success_notification(
                        db,
                        snapshot,
                        instance_name,
                        public_ip,
                    )
                    await db.commit()
                return TaskExecutionResult(done=True)
            task.updated_at = datetime.now()
            if matched:
                task.status = "succeeded"
                task.last_error = None
                done = True
                await _enqueue_change_ip_success_notification(
                    db,
                    snapshot,
                    instance_name,
                    public_ip,
                )
            elif error and not retryable:
                task.status = "failed"
                task.last_error = error
                done = True
            elif exhausted:
                task.status = "failed"
                task.last_error = error or "target CIDR was not reached before maximum attempts"
                done = True
            else:
                task.status = "pending"
                task.last_error = error
                done = False
            await db.commit()

        return TaskExecutionResult(done=done, next_delay=snapshot["interval"])

    @staticmethod
    async def release_security_rule(oci_cfg_id: str, db: AsyncSession) -> None:
        user = await _get_user(oci_cfg_id, db)

        def operation() -> None:
            with OracleInstanceFetcher(_build_oci_config(user), user.username or "") as fetcher:
                vcns = [vcn for vcn in fetcher.list_vcn() if is_oci_helper_managed(vcn)]
                if not vcns:
                    raise OciException(-1, "当前配置没有由本工具创建的 VCN")
                for vcn in vcns:
                    fetcher.release_security_rule(vcn)

        await run_oci(operation, "放行安全列表失败")

    @staticmethod
    async def create_ipv6(oci_cfg_id: str, instance_id: str, db: AsyncSession) -> str:
        user = await _get_user(oci_cfg_id, db)

        def operation() -> str:
            with OracleInstanceFetcher(_build_oci_config(user), user.username or "") as fetcher:
                vcn = fetcher.get_vcn_by_instance_id(instance_id)
                vnic = fetcher.get_vnic_by_instance_id(instance_id)
                return fetcher.create_ipv6(vnic, vcn).ip_address

        return await run_oci(operation, "创建 IPv6 地址失败")

    @staticmethod
    async def update_instance_cfg(
        oci_cfg_id: str,
        instance_id: str,
        ocpus: float,
        memory: float,
        db: AsyncSession,
    ) -> None:
        user = await _get_user(oci_cfg_id, db)

        def operation() -> None:
            with OracleInstanceFetcher(_build_oci_config(user), user.username or "") as fetcher:
                current = fetcher.get_instance_cfg(instance_id)
                if not str(current["shape"]).endswith(".Flex"):
                    raise OciException(-1, "固定规格实例不支持直接调整 OCPU 或内存")
                fetcher.update_instance_cfg(instance_id, ocpus, memory)

        await run_oci(operation, "更新实例配置失败")

    @staticmethod
    async def update_instance_shape(
        oci_cfg_id: str,
        instance_id: str,
        shape: str,
        ocpus: str | None,
        memory: str | None,
        db: AsyncSession,
    ) -> None:
        resolved_shape = Architecture.get_shape_by_type(shape)
        if resolved_shape not in {item.shape_detail for item in Architecture}:
            raise OciException(-1, "不支持的实例 Shape")
        user = await _get_user(oci_cfg_id, db)

        def operation() -> None:
            import oci

            details = oci.core.models.UpdateInstanceDetails(shape=resolved_shape)
            with OracleInstanceFetcher(_build_oci_config(user), user.username or "") as fetcher:
                if resolved_shape.endswith(".Flex"):
                    current = fetcher.get_instance_cfg(instance_id)
                    resolved_ocpus = ocpus or current.get("ocpus")
                    resolved_memory = memory or current.get("memoryInGBs")
                    if resolved_ocpus is None or resolved_memory is None:
                        raise OciException(-1, "Flex Shape 必须提供 OCPU 和内存")
                    details.shape_config = oci.core.models.UpdateInstanceShapeConfigDetails(
                        ocpus=float(resolved_ocpus),
                        memory_in_gbs=float(resolved_memory),
                    )
                    fetcher.validate_shape_update(
                        instance_id,
                        resolved_shape,
                        float(resolved_ocpus),
                        float(resolved_memory),
                    )
                else:
                    fetcher.validate_shape_update(instance_id, resolved_shape, None, None)
                fetcher.compute_client.update_instance(
                    instance_id=instance_id,
                    update_instance_details=details,
                )

        await run_oci(operation, "更新实例 Shape 失败")

    @staticmethod
    async def get_instance_cfg_info(oci_cfg_id: str, instance_id: str, db: AsyncSession) -> dict:
        user = await _get_user(oci_cfg_id, db)

        def operation() -> dict:
            with OracleInstanceFetcher(_build_oci_config(user), user.username or "") as fetcher:
                return fetcher.get_instance_cfg(instance_id)

        return await run_oci(operation, "获取实例配置失败")


def _build_create_success_message(snapshot: dict, detail: dict) -> str:
    lifecycle_state = str(detail.get("lifecycle_state") or "UNKNOWN").upper()
    outcome = "开机成功" if lifecycle_state == "RUNNING" else "实例创建成功"
    return CREATE_SUCCESS_MESSAGE.format(
        username=snapshot["username"],
        outcome=outcome,
        time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        lifecycle_state=lifecycle_state,
        display_name=detail.get("display_name") or snapshot.get("instance_name") or "自动生成",
        region=detail.get("region", ""),
        availability_domain=detail.get("availability_domain", ""),
        architecture=detail.get("architecture", ""),
        ocpus=detail.get("ocpus", ""),
        memory=detail.get("memory", ""),
        disk=detail.get("disk", ""),
        boot_volume_vpus_per_gb=detail.get("boot_volume_vpus_per_gb", ""),
        shape=detail.get("shape", ""),
        private_ip=detail.get("private_ip") or "已自动分配（暂未获取）",
        public_ip=detail.get("public_ip") or "等待分配",
        count=snapshot["attempt"],
        duration=get_time_difference(snapshot["create_time"]),
    )


async def _enqueue_create_success_notifications(
    db: AsyncSession,
    snapshot: dict,
    success_results: list[dict],
) -> None:
    for index, detail in enumerate(success_results):
        instance_identity = detail.get("instance_id") or f"remaining-{snapshot['remaining']}-{index}"
        await enqueue_notification(
            db,
            event_key=f"create-success:{snapshot['task_id']}:{instance_identity}",
            category="instance-create-success",
            message=_build_create_success_message(snapshot, detail),
        )


async def _enqueue_change_ip_success_notification(
    db: AsyncSession,
    snapshot: dict,
    instance_name: str,
    public_ip: str | None,
) -> None:
    await enqueue_notification(
        db,
        event_key=f"change-ip-success:{snapshot['task_id']}",
        category="public-ip-change-success",
        message=(
            "【更换IP任务】\n\n"
            f"用户：[{snapshot['username']}]\n区域：{snapshot['region']}\n"
            f"实例：{instance_name}\n新的公网IP：{public_ip or '未知'}"
        ),
    )


async def _get_user(oci_cfg_id: str, db: AsyncSession) -> OciUser:
    user = await db.get(OciUser, oci_cfg_id)
    if user is None:
        raise OciException(-1, "用户配置不存在")
    return user


def _build_oci_config(user: OciUser):
    from core.oracle_fetcher import OciConfig

    return OciConfig(
        user_id=user.oci_user_id,
        tenant_id=user.oci_tenant_id,
        region=user.oci_region,
        fingerprint=user.oci_fingerprint,
        private_key_path=user.oci_key_path,
    )


def _random_retry_delay(interval_min: int, interval_max: int | None) -> int:
    upper_bound = max(interval_min, interval_max or interval_min)
    return random.randint(interval_min, upper_bound)


def _build_instance_display_name(
    base_name: str | None,
    initial_create_numbers: int,
    remaining_create_numbers: int,
) -> str | None:
    if not base_name:
        return None
    total = max(1, initial_create_numbers)
    if total == 1:
        return base_name
    sequence = max(1, min(total, total - remaining_create_numbers + 1))
    return f"{base_name}-{sequence:03d}"


def _safe_error(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    return text[:1000]
