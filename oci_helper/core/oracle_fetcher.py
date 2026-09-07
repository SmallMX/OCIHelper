"""
OCI SDK 封装模块

对应 Java 原项目：com.yohann.ocihelper.config.OracleInstanceFetcher
功能：
    - 封装 OCI Python SDK 的客户端初始化和连接管理
    - 提供实例创建、网络管理、身份认证等核心 API 调用
    - 使用上下文管理器 (with 语句) 自动清理资源

该类是整个项目与 OCI 云 API 交互的唯一入口。
"""

import hashlib
import ipaddress
import math
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import oci
from loguru import logger

from config import settings
from enums.architecture import Architecture
from enums.operation_system import OperationSystem

# 默认 VCN CIDR 块
DEFAULT_CIDR_BLOCK = "10.0.0.0/16"
MANAGED_TAG_KEY = "oci-helper-managed"
MANAGED_TAG_VALUE = "true"
LAUNCH_ID_TAG_KEY = "oci-helper-launch-id"


def is_oci_helper_managed(resource: Any) -> bool:
    """Return whether a resource carries this application's ownership tag."""
    tags = getattr(resource, "freeform_tags", None) or {}
    return tags.get(MANAGED_TAG_KEY) == MANAGED_TAG_VALUE


def is_capacity_oci_error(exc: Exception) -> bool:
    if not isinstance(exc, oci.exceptions.ServiceError):
        return False
    description = f"{exc.code or ''} {exc.message or str(exc)}".lower()
    return "capacity" in description


def is_retryable_oci_error(exc: Exception) -> bool:
    """Classify transient OCI/network failures without retrying credential errors."""
    if isinstance(exc, oci.exceptions.ServiceError):
        message = (exc.message or str(exc)).lower()
        code = (exc.code or "").lower()
        return (
            exc.status in {408, 429, 500, 502, 503, 504}
            or (exc.status == 409 and code == "incorrectstate")
            or is_capacity_oci_error(exc)
            or any(
                marker in message
                for marker in ("too many requests", "temporarily unavailable")
            )
        )
    return isinstance(
        exc,
        (
            oci.exceptions.RequestException,
            oci.exceptions.MaximumWaitTimeExceeded,
            TimeoutError,
            ConnectionError,
        ),
    )


@dataclass
class OciConfig:
    """
    OCI 用户配置数据类

    对应 Java: SysUserDTO.OciCfg
    存储单个 OCI 用户的认证信息。
    """

    user_id: str
    tenant_id: str
    region: str
    fingerprint: str
    private_key_path: str
    compartment_id: str | None = None


class OracleInstanceFetcher:
    """
    OCI 实例操作封装器

    对应 Java: OracleInstanceFetcher
    封装了所有与 OCI API 的交互逻辑。

    使用方式：
        with OracleInstanceFetcher(oci_cfg) as fetcher:
            instances = fetcher.list_instances()
    """

    def __init__(self, oci_cfg: OciConfig, username: str = ""):
        self.oci_cfg = oci_cfg
        self.username = username

        # 读取私钥内容
        key_file_path = oci_cfg.private_key_path
        if not os.path.isabs(key_file_path):
            key_file_path = os.path.join(settings.key_dir_path, key_file_path)

        # 构建 OCI SDK 配置字典
        self._config = {
            "user": oci_cfg.user_id,
            "tenancy": oci_cfg.tenant_id,
            "region": oci_cfg.region,
            "fingerprint": oci_cfg.fingerprint,
            "key_file": key_file_path,
        }

        # 验证配置
        oci.config.validate_config(self._config)

        # 初始化各 SDK 客户端
        timeout = (10, settings.oci_request_timeout_seconds)
        self.compute_client = oci.core.ComputeClient(self._config, timeout=timeout)
        self.identity_client = oci.identity.IdentityClient(self._config, timeout=timeout)
        self.vn_client = oci.core.VirtualNetworkClient(self._config, timeout=timeout)
        self.virtual_network_client = self.vn_client
        self.block_storage_client = oci.core.BlockstorageClient(self._config, timeout=timeout)
        self.limits_client = oci.limits.LimitsClient(self._config, timeout=timeout)

        # 确定 compartment_id（默认使用根隔间）
        if oci_cfg.compartment_id:
            self.compartment_id = oci_cfg.compartment_id
        else:
            self.compartment_id = self._find_root_compartment()

        logger.debug(
            f"OCI 客户端初始化完成: 用户={username}, 区域={oci_cfg.region}, "
            f"compartment={self.compartment_id}"
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def close(self):
        for client in (
            self.compute_client,
            self.identity_client,
            self.vn_client,
            self.block_storage_client,
            self.limits_client,
        ):
            try:
                client.base_client.session.close()
            except Exception as exc:
                logger.debug("关闭 OCI HTTP 会话失败: {}", exc)
        logger.debug(f"OCI 客户端已关闭: 用户={self.username}")

    def configure_instance_creation(
        self,
        *,
        architecture: str,
        ocpus: float,
        memory: float,
        disk: int,
        operation_system: str,
        create_numbers: int,
        ssh_public_key: str,
        boot_volume_vpus_per_gb: int = 20,
        instance_name: str | None = None,
        operation_system_version: str | None = None,
        availability_domain: str | None = None,
        availability_domain_index: int = 0,
        retry_token_seed: str | None = None,
    ) -> None:
        self._architecture = architecture
        self._shape = Architecture.get_shape_by_type(architecture)
        self._ocpus = ocpus
        self._memory = memory
        self._disk = disk
        if boot_volume_vpus_per_gb not in {10, 20} and not (
            30 <= boot_volume_vpus_per_gb <= 120
        ):
            raise ValueError("引导卷 VPU/GB 只支持 10、20 或 30 至 120")
        self._boot_volume_vpus_per_gb = boot_volume_vpus_per_gb
        self._operation_system = operation_system
        self._operation_system_version = operation_system_version
        self._instance_name = (instance_name or "").strip() or None
        self._ssh_key = ssh_public_key.strip()
        if not self._ssh_key:
            raise ValueError("SSH public key is required")
        self._availability_domain = availability_domain
        self._availability_domain_index = max(0, availability_domain_index)
        self._retry_token_seed = retry_token_seed
        self.create_numbers = create_numbers

    # ============================
    #  身份与认证
    # ============================

    def _find_root_compartment(self) -> str:
        return self.oci_cfg.tenant_id

    def get_availability_domains(self) -> list:
        """获取可用域列表"""
        response = oci.pagination.list_call_get_all_results(
            self.identity_client.list_availability_domains, compartment_id=self.compartment_id
        )
        return response.data or []

    def list_region_subscriptions(self) -> list:
        """获取已订阅区域列表"""
        response = oci.pagination.list_call_get_all_results(
            self.identity_client.list_region_subscriptions, tenancy_id=self.oci_cfg.tenant_id
        )
        return response.data or []

    def get_user_info(self) -> Any:
        """获取当前用户信息"""
        response = self.identity_client.get_user(user_id=self.oci_cfg.user_id)
        return response.data

    def get_tenant_info(self) -> dict:
        """
        获取租户信息

        对应 Java: OracleInstanceFetcher.getTenantInfo()

        返回:
            包含租户名称、家庭区域和已订阅区域的字典
        """
        tenancy = self.identity_client.get_tenancy(tenancy_id=self.oci_cfg.tenant_id).data

        subscriptions = self.list_region_subscriptions()
        subscribed_regions = [s.region_name for s in subscriptions]

        home_region = tenancy.home_region_key if tenancy.home_region_key else ""

        return {
            "tenantName": tenancy.name,
            "homeRegion": home_region,
            "subscribedRegions": subscribed_regions,
        }

    # ============================
    #  实例管理
    # ============================

    def list_instances(self) -> list:
        """列出所有实例"""
        response = oci.pagination.list_call_get_all_results(
            self.compute_client.list_instances, compartment_id=self.compartment_id
        )
        return [i for i in (response.data or []) if i.lifecycle_state != "TERMINATED"]

    def get_instance(self, instance_id: str) -> Any:
        """获取实例详情"""
        response = self.compute_client.get_instance(instance_id=instance_id)
        return response.data

    def get_instance_by_id(self, instance_id: str) -> Any:
        """
        按 ID 获取实例（get_instance 别名）

        对应 Java: OracleInstanceFetcher.getInstanceById()
        """
        return self.get_instance(instance_id)

    def get_instance_cfg(self, instance_id: str) -> dict:
        """
        获取实例配置信息（Shape 详情）

        对应 Java: OracleInstanceFetcher.getInstanceCfg()

        返回:
            包含 shape, ocpus, memoryInGBs 等的字典
        """
        instance = self.get_instance(instance_id)
        shape_config = instance.shape_config
        return {
            "shape": instance.shape,
            "ocpus": shape_config.ocpus if shape_config else None,
            "memoryInGBs": shape_config.memory_in_gbs if shape_config else None,
            "networkingBandwidthInGbps": (
                shape_config.networking_bandwidth_in_gbps if shape_config else None
            ),
            "displayName": instance.display_name,
            "lifecycleState": instance.lifecycle_state,
        }

    def update_instance_cfg(self, instance_id: str, ocpus: float, memory: float) -> Any:
        """
        更新实例配置（CPU 和内存）

        对应 Java: OracleInstanceFetcher.updateInstanceCfg()
        """
        instance = self.get_instance(instance_id)
        shape = self._get_shape(instance.shape, instance.availability_domain)
        self._validate_flex_shape_config(shape, ocpus, memory)
        response = self.compute_client.update_instance(
            instance_id=instance_id,
            update_instance_details=oci.core.models.UpdateInstanceDetails(
                shape_config=oci.core.models.UpdateInstanceShapeConfigDetails(
                    ocpus=ocpus,
                    memory_in_gbs=memory,
                )
            ),
        )
        logger.info(f"已更新实例配置: {instance_id}, OCPU={ocpus}, 内存={memory}GB")
        return response.data

    def _get_shape(self, shape_name: str, availability_domain: str | None = None):
        kwargs = {"compartment_id": self.compartment_id, "shape": shape_name}
        if availability_domain:
            kwargs["availability_domain"] = availability_domain
        response = oci.pagination.list_call_get_all_results(
            self.compute_client.list_shapes,
            **kwargs,
        )
        shape = next((item for item in (response.data or []) if item.shape == shape_name), None)
        if shape is None:
            raise ValueError(f"当前区域或可用域不支持 Shape {shape_name}")
        return shape

    @staticmethod
    def _validate_flex_shape_config(shape, ocpus: float, memory: float) -> None:
        if not getattr(shape, "is_flexible", False):
            raise ValueError(f"固定规格 Shape {shape.shape} 不支持调整 OCPU 或内存")
        if not math.isfinite(ocpus) or not math.isfinite(memory) or ocpus <= 0 or memory <= 0:
            raise ValueError("OCPU 和内存必须是大于 0 的有限数值")
        ocpu_options = getattr(shape, "ocpu_options", None)
        memory_options = getattr(shape, "memory_options", None)
        if ocpu_options:
            minimum = getattr(ocpu_options, "min", None)
            maximum = getattr(ocpu_options, "max", None)
            if minimum is not None and ocpus < minimum:
                raise ValueError(f"{shape.shape} 至少需要 {minimum:g} OCPU")
            if maximum is not None and ocpus > maximum:
                raise ValueError(f"{shape.shape} 最多支持 {maximum:g} OCPU")
        if memory_options:
            minimum = getattr(memory_options, "min_in_g_bs", None)
            maximum = getattr(memory_options, "max_in_g_bs", None)
            min_per_ocpu = getattr(memory_options, "min_per_ocpu_in_gbs", None)
            max_per_ocpu = getattr(memory_options, "max_per_ocpu_in_gbs", None)
            if minimum is not None and memory < minimum:
                raise ValueError(f"{shape.shape} 至少需要 {minimum:g} GB 内存")
            if maximum is not None and memory > maximum:
                raise ValueError(f"{shape.shape} 最多支持 {maximum:g} GB 内存")
            if min_per_ocpu is not None and memory < ocpus * min_per_ocpu:
                raise ValueError(f"{shape.shape} 每 OCPU 至少需要 {min_per_ocpu:g} GB 内存")
            if max_per_ocpu is not None and memory > ocpus * max_per_ocpu:
                raise ValueError(f"{shape.shape} 每 OCPU 最多支持 {max_per_ocpu:g} GB 内存")

    def validate_shape_update(
        self,
        instance_id: str,
        shape_name: str,
        ocpus: float | None,
        memory: float | None,
    ) -> None:
        instance = self.get_instance(instance_id)
        if shape_name != instance.shape:
            current_shape = self._get_shape(instance.shape, instance.availability_domain)
            resize_compatible_shapes = getattr(
                current_shape, "resize_compatible_shapes", None
            )
            compatible_shapes = {
                getattr(item, "shape_name", item)
                for item in (resize_compatible_shapes or [])
            }
            if resize_compatible_shapes is not None and shape_name not in compatible_shapes:
                raise ValueError(f"当前实例不能直接调整为 Shape {shape_name}")
        target_shape = self._get_shape(shape_name, instance.availability_domain)
        if getattr(target_shape, "is_flexible", False):
            if ocpus is None or memory is None:
                raise ValueError("Flex Shape 必须提供 OCPU 和内存")
            self._validate_flex_shape_config(target_shape, ocpus, memory)

    def create_instance_data(self) -> dict:
        """
        创建实例（核心方法）

        对应 Java: OracleInstanceFetcher.createInstanceData()

        返回:
            包含 success、private_ip、public_ip、region 等非敏感信息的字典
        """
        shape = getattr(self, "_shape", Architecture.ARM.shape_detail)
        result = {
            "success": False,
            "retryable": True,
            "error": None,
            "private_ip": None,
            "public_ip": None,
            "region": self.oci_cfg.region,
            "availability_domain": None,
            "lifecycle_state": None,
            "rotate_availability_domain": False,
            "architecture": getattr(self, "_architecture", "ARM"),
            "shape": shape,
            "ocpus": getattr(self, "_ocpus", 1),
            "memory": getattr(self, "_memory", 6),
            "disk": getattr(self, "_disk", 50),
            "boot_volume_vpus_per_gb": getattr(self, "_boot_volume_vpus_per_gb", 20),
        }

        ad_name = None
        try:
            ad_name = self._select_availability_domain(
                shape,
                getattr(self, "_availability_domain", None),
                getattr(self, "_availability_domain_index", 0),
            )
            result["availability_domain"] = ad_name
            launch_id, display_name = self._build_launch_identity(
                getattr(self, "_retry_token_seed", None),
                ad_name,
                getattr(self, "_instance_name", None),
            )
            instance = None
            if launch_id:
                instance = self._find_existing_managed_instance(launch_id, display_name)
            image = None
            if instance is None:
                image = self._select_image(
                    shape,
                    getattr(self, "_operation_system", "Canonical Ubuntu"),
                    getattr(self, "_operation_system_version", None),
                )
                _, subnet = self._ensure_network(ad_name)

                launch_tags = {MANAGED_TAG_KEY: MANAGED_TAG_VALUE}
                if launch_id:
                    launch_tags[LAUNCH_ID_TAG_KEY] = launch_id
                launch_kwargs = {
                    "compartment_id": self.compartment_id,
                    "availability_domain": ad_name,
                    "shape": shape,
                    "source_details": oci.core.models.InstanceSourceViaImageDetails(
                        image_id=image.id,
                        boot_volume_size_in_gbs=getattr(self, "_disk", 50),
                        boot_volume_vpus_per_gb=getattr(
                            self, "_boot_volume_vpus_per_gb", 20
                        ),
                    ),
                    # OCI 自动分配主专用 IPv4，并为其关联临时公共 IPv4。
                    "create_vnic_details": oci.core.models.CreateVnicDetails(
                        subnet_id=subnet.id,
                        assign_public_ip=True,
                    ),
                    "display_name": display_name,
                    "metadata": {"ssh_authorized_keys": self._ssh_key},
                    "freeform_tags": launch_tags,
                }
                if shape.endswith(".Flex"):
                    launch_kwargs["shape_config"] = (
                        oci.core.models.LaunchInstanceShapeConfigDetails(
                            ocpus=float(getattr(self, "_ocpus", 1)),
                            memory_in_gbs=float(getattr(self, "_memory", 6)),
                        )
                    )

                instance = self.compute_client.launch_instance(
                    launch_instance_details=oci.core.models.LaunchInstanceDetails(**launch_kwargs),
                    opc_retry_token=launch_id,
                ).data
            else:
                logger.info(
                    "发现同一创建任务已提交的实例，跳过重复 LaunchInstance: task={}, instance={}",
                    launch_id,
                    instance.id,
                )

            instance = self._wait_for_created_instance(instance)
            lifecycle_state = (getattr(instance, "lifecycle_state", None) or "UNKNOWN").upper()
            private_ip = None
            public_ip = None
            vnic_error = None
            for attempt in range(12):
                try:
                    vnic = self.get_vnic_by_instance_id(instance.id)
                    private_ip = getattr(vnic, "private_ip", None)
                    public_ip = getattr(vnic, "public_ip", None)
                    if public_ip:
                        break
                except Exception as exc:
                    vnic_error = exc
                if attempt < 11:
                    time.sleep(5)
            if not public_ip:
                logger.warning(
                    "实例已创建，但暂未获取到主 VNIC 的公共 IPv4: instance={}, error={}",
                    instance.id,
                    vnic_error or "public IP is not visible yet",
                )

            result.update(
                success=True,
                retryable=False,
                availability_domain=ad_name,
                lifecycle_state=lifecycle_state,
                display_name=getattr(instance, "display_name", None) or display_name,
                private_ip=private_ip,
                public_ip=public_ip,
                instance_id=instance.id,
                image_id=getattr(instance, "image_id", None)
                or (getattr(image, "id", None) if image else None),
            )
            logger.info(
                "实例创建成功: user={}, region={}, ad={}, instance={}, state={}, "
                "private_ip={}, public_ip={}",
                self.username,
                self.oci_cfg.region,
                ad_name,
                instance.id,
                lifecycle_state,
                private_ip or "pending",
                public_ip or "pending",
            )
        except oci.exceptions.ServiceError as exc:
            message = exc.message or str(exc)
            retryable = is_retryable_oci_error(exc)
            result.update(
                error=message,
                retryable=retryable,
                rotate_availability_domain=retryable and is_capacity_oci_error(exc),
            )
            log = logger.warning if retryable else logger.error
            log(
                "创建实例失败: user={}, region={}, ad={}, status={}, code={}, message={}",
                self.username,
                self.oci_cfg.region,
                ad_name or "unresolved",
                exc.status,
                exc.code,
                message,
            )
        except Exception as exc:
            result.update(error=str(exc), retryable=is_retryable_oci_error(exc))
            logger.exception(
                "创建实例异常: user={}, region={}, ad={}, shape={}",
                self.username,
                self.oci_cfg.region,
                ad_name or "unresolved",
                shape,
            )
        return result

    @staticmethod
    def _build_launch_identity(
        retry_token_seed: str | None,
        ad_name: str,
        requested_display_name: str | None = None,
    ) -> tuple[str | None, str]:
        if retry_token_seed:
            launch_id = hashlib.sha256(f"{retry_token_seed}:{ad_name}".encode()).hexdigest()
            return launch_id, requested_display_name or f"oci-helper-{launch_id[:20]}"
        timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
        return None, requested_display_name or f"instance-{timestamp}"

    def _find_existing_managed_instance(self, launch_id: str, display_name: str):
        response = oci.pagination.list_call_get_all_results(
            self.compute_client.list_instances,
            compartment_id=self.compartment_id,
            display_name=display_name,
        )
        matches = [
            instance
            for instance in (response.data or [])
            if getattr(instance, "display_name", None) == display_name
            and is_oci_helper_managed(instance)
            and (getattr(instance, "freeform_tags", None) or {}).get(LAUNCH_ID_TAG_KEY) == launch_id
        ]
        if len(matches) > 1:
            raise ValueError(f"检测到多个相同启动标识的实例，已停止创建: {launch_id}")
        return matches[0] if matches else None

    def _wait_for_created_instance(self, instance):
        lifecycle_state = (getattr(instance, "lifecycle_state", None) or "").upper()
        if lifecycle_state in {"TERMINATING", "TERMINATED"}:
            raise ValueError(f"同一创建任务的实例已处于 {lifecycle_state} 状态，已停止重复创建")
        if lifecycle_state in {"RUNNING", "STOPPING", "STOPPED"}:
            return instance
        return oci.wait_until(
            self.compute_client,
            self.compute_client.get_instance(instance.id),
            "lifecycle_state",
            "RUNNING",
            max_wait_seconds=600,
        ).data

    def _select_availability_domain(
        self,
        shape: str,
        preferred_domain: str | None = None,
        start_index: int = 0,
    ) -> str:
        accessible_ads = self.get_availability_domains()
        supported_ads = self._filter_supported_availability_domains(
            shape,
            accessible_ads,
        )
        if preferred_domain:
            selected = next(
                (ad for ad in supported_ads if ad.name == preferred_domain),
                None,
            )
            if selected is not None:
                return selected.name
            accessible_domains = {ad.name for ad in accessible_ads}
            if preferred_domain not in accessible_domains:
                raise ValueError(f"可用域不存在或当前账号不可访问: {preferred_domain}")
            raise ValueError(f"可用域 {preferred_domain} 不支持 Shape {shape}")
        if supported_ads:
            return supported_ads[start_index % len(supported_ads)].name
        raise ValueError(f"区域 {self.oci_cfg.region} 不支持 Shape {shape}")

    def list_supported_availability_domains(self, shape: str) -> list:
        return self._filter_supported_availability_domains(
            shape,
            self.get_availability_domains(),
        )

    def _filter_supported_availability_domains(self, shape: str, domains: list) -> list:
        supported_ads = []
        for ad in domains:
            response = oci.pagination.list_call_get_all_results(
                self.compute_client.list_shapes,
                compartment_id=self.compartment_id,
                availability_domain=ad.name,
            )
            if any(item.shape == shape for item in (response.data or [])):
                supported_ads.append(ad)
        return sorted(supported_ads, key=lambda item: item.name)

    def validate_availability_domain_selection(self, shape: str, domain_name: str) -> None:
        self._select_availability_domain(shape, domain_name)

    def list_image_options(self, shape: str) -> list[dict[str, str]]:
        response = oci.pagination.list_call_get_all_results(
            self.compute_client.list_images,
            compartment_id=self.compartment_id,
            shape=shape,
            lifecycle_state="AVAILABLE",
            sort_by="TIMECREATED",
            sort_order="DESC",
        )
        options: dict[tuple[str, str], dict[str, str]] = {}
        for image in response.data or []:
            operating_system = (getattr(image, "operating_system", None) or "").strip()
            version = (getattr(image, "operating_system_version", None) or "").strip()
            if not operating_system or not version or "windows" in operating_system.lower():
                continue
            key = (operating_system.casefold(), version.casefold())
            options.setdefault(
                key,
                {
                    "operating_system": operating_system,
                    "operating_system_version": version,
                    "label": f"{operating_system} {version}",
                },
            )
        return sorted(options.values(), key=lambda item: item["label"].casefold())

    def _select_image(
        self,
        shape: str,
        operation_system: str,
        operation_system_version: str | None = None,
    ):
        os_type, version = OperationSystem.resolve(
            operation_system,
            operation_system_version,
        )
        response = oci.pagination.list_call_get_all_results(
            self.compute_client.list_images,
            compartment_id=self.compartment_id,
            shape=shape,
            operating_system=os_type,
            operating_system_version=version,
            lifecycle_state="AVAILABLE",
            sort_by="TIMECREATED",
            sort_order="DESC",
        )
        images = response.data or []
        if not images:
            raise ValueError(f"没有找到适用于 {shape} 的 {os_type} {version} 镜像")
        return images[0]

    def validate_image_selection(
        self,
        shape: str,
        operation_system: str,
        operation_system_version: str | None = None,
    ) -> None:
        self._select_image(shape, operation_system, operation_system_version)

    def _ensure_network(self, ad_name: str):
        """优先复用现有 VCN/公共子网，必要时再创建公共子网。"""
        vcns = self.list_vcns()
        subnets_by_vcn: dict[str, list] = {}
        candidates: list[tuple[tuple[bool, int, int], Any, Any]] = []
        for vcn_index, vcn in enumerate(vcns):
            subnets = self.list_subnets(vcn.id)
            subnets_by_vcn[vcn.id] = subnets
            for subnet_index, subnet in enumerate(subnets):
                availability_domain = getattr(subnet, "availability_domain", None)
                if availability_domain not in {None, ad_name}:
                    continue
                if bool(getattr(subnet, "prohibit_public_ip_on_vnic", False)) or bool(
                    getattr(subnet, "prohibit_internet_ingress", False)
                ):
                    continue
                priority = (
                    availability_domain is not None,
                    vcn_index,
                    subnet_index,
                )
                candidates.append((priority, vcn, subnet))

        if candidates:
            _, vcn, subnet = min(candidates, key=lambda item: item[0])
            logger.info(
                "复用现有公共网络: VCN={}, subnet={}",
                vcn.id,
                subnet.id,
            )
            return vcn, subnet

        vcn = None
        subnet_cidr = None
        for existing_vcn in vcns:
            existing_subnets = subnets_by_vcn[existing_vcn.id]
            try:
                available_cidr = self._find_available_subnet_cidr(
                    existing_vcn,
                    existing_subnets,
                )
                self._ensure_internet_gateway(existing_vcn)
            except ValueError as exc:
                logger.info(
                    "跳过不适合创建公共子网的现有 VCN: vcn={}, reason={}",
                    existing_vcn.id,
                    exc,
                )
                continue
            vcn = existing_vcn
            subnet_cidr = available_cidr
            break

        if vcn is None:
            vcn = self.vn_client.create_vcn(
                create_vcn_details=oci.core.models.CreateVcnDetails(
                    compartment_id=self.compartment_id,
                    cidr_block=DEFAULT_CIDR_BLOCK,
                    display_name="oci-helper-vcn",
                    freeform_tags={MANAGED_TAG_KEY: MANAGED_TAG_VALUE},
                )
            ).data
            vcn = oci.wait_until(
                self.vn_client,
                self.vn_client.get_vcn(vcn.id),
                "lifecycle_state",
                "AVAILABLE",
                max_wait_seconds=300,
            ).data
            logger.info(f"已创建 VCN: {vcn.id}")

            subnet_cidr = self._find_available_subnet_cidr(vcn, [])
            self._ensure_internet_gateway(vcn)

        subnet = self.vn_client.create_subnet(
            create_subnet_details=oci.core.models.CreateSubnetDetails(
                compartment_id=self.compartment_id,
                vcn_id=vcn.id,
                cidr_block=subnet_cidr,
                prohibit_internet_ingress=False,
                prohibit_public_ip_on_vnic=False,
                display_name="oci-helper-subnet",
                freeform_tags={MANAGED_TAG_KEY: MANAGED_TAG_VALUE},
            )
        ).data
        subnet = oci.wait_until(
            self.vn_client,
            self.vn_client.get_subnet(subnet.id),
            "lifecycle_state",
            "AVAILABLE",
            max_wait_seconds=300,
        ).data
        logger.info(f"已创建公共子网: {subnet.id}")

        return vcn, subnet

    def _ensure_internet_gateway(self, vcn) -> None:
        gateways = [
            gateway
            for gateway in (
                oci.pagination.list_call_get_all_results(
                    self.vn_client.list_internet_gateways,
                    compartment_id=self.compartment_id,
                    vcn_id=vcn.id,
                ).data
                or []
            )
            if getattr(gateway, "lifecycle_state", None)
            not in {"TERMINATING", "TERMINATED"}
        ]
        route_table = self.vn_client.get_route_table(rt_id=vcn.default_route_table_id).data
        route_rules = list(route_table.route_rules or [])
        default_routes = [
            rule
            for rule in route_rules
            if getattr(rule, "destination", None) == "0.0.0.0/0"
        ]
        routed_gateway = next(
            (
                gateway
                for gateway in gateways
                if any(
                    getattr(rule, "network_entity_id", None) == gateway.id
                    for rule in default_routes
                )
            ),
            None,
        )
        if default_routes and routed_gateway is None:
            raise ValueError("默认路由 0.0.0.0/0 已指向其他网络实体")

        managed = [
            gateway
            for gateway in gateways
            if gateway.display_name == "oci-helper-ig" and is_oci_helper_managed(gateway)
        ]
        gateway = (
            routed_gateway
            or next((item for item in gateways if item.is_enabled), None)
            or (managed[0] if managed else None)
            or (gateways[0] if gateways else None)
        )
        if gateway is not None:
            if not gateway.is_enabled:
                gateway = self.vn_client.update_internet_gateway(
                    ig_id=gateway.id,
                    update_internet_gateway_details=(
                        oci.core.models.UpdateInternetGatewayDetails(is_enabled=True)
                    ),
                ).data
        else:
            gateway = self.vn_client.create_internet_gateway(
                create_internet_gateway_details=oci.core.models.CreateInternetGatewayDetails(
                    compartment_id=self.compartment_id,
                    vcn_id=vcn.id,
                    is_enabled=True,
                    display_name="oci-helper-ig",
                    freeform_tags={MANAGED_TAG_KEY: MANAGED_TAG_VALUE},
                )
            ).data

        if not any(
            rule.destination == "0.0.0.0/0" and rule.network_entity_id == gateway.id
            for rule in route_rules
        ):
            route_rules.append(
                oci.core.models.RouteRule(
                    destination="0.0.0.0/0",
                    destination_type="CIDR_BLOCK",
                    network_entity_id=gateway.id,
                )
            )
            self.vn_client.update_route_table(
                rt_id=route_table.id,
                update_route_table_details=oci.core.models.UpdateRouteTableDetails(
                    route_rules=route_rules
                ),
            )

    @staticmethod
    def _find_available_subnet_cidr(vcn, subnets: list) -> str:
        cidr_blocks = getattr(vcn, "cidr_blocks", None) or [
            getattr(vcn, "cidr_block", None) or DEFAULT_CIDR_BLOCK
        ]
        vcn_networks = [
            ipaddress.ip_network(cidr)
            for cidr in cidr_blocks
            if cidr and ipaddress.ip_network(cidr).version == 4
        ]
        occupied = [
            ipaddress.ip_network(item.cidr_block)
            for item in subnets
            if getattr(item, "cidr_block", None)
        ]
        for vcn_network in vcn_networks:
            prefix_length = max(24, vcn_network.prefixlen)
            candidates = (
                [vcn_network]
                if prefix_length == vcn_network.prefixlen
                else vcn_network.subnets(new_prefix=prefix_length)
            )
            for candidate in candidates:
                if not any(candidate.overlaps(current) for current in occupied):
                    return str(candidate)
        raise ValueError(f"VCN {vcn.id} 没有可用的 IPv4 子网地址段")

    def instance_action(self, instance_id: str, action: str) -> Any:
        """执行实例操作（启动/停止/重启）"""
        response = self.compute_client.instance_action(instance_id=instance_id, action=action)
        logger.info(f"实例操作执行: 用户={self.username}, 实例={instance_id}, 操作={action}")
        return response.data

    def terminate_instance(self, instance_id: str, preserve_boot_volume: bool = False) -> None:
        """终止（删除）实例"""
        self.compute_client.terminate_instance(
            instance_id=instance_id, preserve_boot_volume=preserve_boot_volume
        )
        logger.info(
            f"实例终止: 用户={self.username}, 实例={instance_id}, 保留引导卷={preserve_boot_volume}"
        )

    def update_instance_name(self, instance_id: str, name: str) -> Any:
        """修改实例名称"""
        response = self.compute_client.update_instance(
            instance_id=instance_id,
            update_instance_details=oci.core.models.UpdateInstanceDetails(display_name=name),
        )
        return response.data

    def create_console_connection(self, instance_id: str, public_key: str) -> str:
        """
        创建实例控制台连接 (VNC)

        对应 Java: OracleInstanceFetcher.startVnc()

        返回:
            VNC 连接 URL
        """
        response = self.compute_client.create_instance_console_connection(
            create_instance_console_connection_details=(
                oci.core.models.CreateInstanceConsoleConnectionDetails(
                    instance_id=instance_id,
                    public_key=public_key,
                )
            ),
            opc_retry_token=str(uuid.uuid4()),
        )
        connection = response.data
        logger.info(f"已创建控制台连接: 实例={instance_id}")
        return connection.vnc_connection_string or ""

    def list_vnic_attachments(self, instance_id: str) -> list:
        """列出实例的 VNIC 附件"""
        response = oci.pagination.list_call_get_all_results(
            self.compute_client.list_vnic_attachments,
            compartment_id=self.compartment_id,
            instance_id=instance_id,
        )
        return response.data or []

    def get_vnic(self, vnic_id: str) -> Any:
        """获取 VNIC 详情"""
        response = self.vn_client.get_vnic(vnic_id=vnic_id)
        return response.data

    def get_vnic_by_instance_id(self, instance_id: str) -> Any:
        """
        按实例 ID 获取主 VNIC

        对应 Java: OracleInstanceFetcher.getVnicByInstanceId()
        """
        attachments = [
            attachment
            for attachment in self.list_vnic_attachments(instance_id)
            if attachment.lifecycle_state == "ATTACHED"
        ]
        for attachment in attachments:
            vnic = self.get_vnic(attachment.vnic_id)
            if getattr(vnic, "is_primary", False):
                return vnic
        raise Exception(f"未找到实例 {instance_id} 的 VNIC")

    # ============================
    #  网络管理 (VCN/Subnet)
    # ============================

    def list_vcns(self) -> list:
        """列出所有 VCN"""
        response = oci.pagination.list_call_get_all_results(
            self.vn_client.list_vcns, compartment_id=self.compartment_id
        )
        return [v for v in (response.data or []) if v.lifecycle_state == "AVAILABLE"]

    def list_vcn(self) -> list:
        """list_vcns 的别名，兼容 services 层调用"""
        return self.list_vcns()

    def get_vcn_by_id(self, vcn_id: str) -> Any:
        """
        按 ID 获取 VCN

        对应 Java: OracleInstanceFetcher.getVcnById()
        """
        response = self.vn_client.get_vcn(vcn_id=vcn_id)
        return response.data

    def get_vcn_by_instance_id(self, instance_id: str) -> Any:
        """
        按实例 ID 获取关联的 VCN

        对应 Java: OracleInstanceFetcher.getVcnByInstanceId()
        通过 实例 -> VNIC -> 子网 -> VCN 链路查找
        """
        vnic = self.get_vnic_by_instance_id(instance_id)
        subnet = self.vn_client.get_subnet(subnet_id=vnic.subnet_id).data
        return self.get_vcn_by_id(subnet.vcn_id)

    def check_vcn_is_public(self, vcn) -> str:
        """
        检查 VCN 是否有公网可见性

        对应 Java: OracleInstanceFetcher.checkVcnIsPublic()

        返回:
            "public" 或 "private"
        """
        try:
            subnets = self.list_subnets(vcn.id)
            for subnet in subnets:
                if not subnet.prohibit_public_ip_on_vnic:
                    return "public"
            return "private"
        except Exception:
            return "unknown"

    def delete_vcn_by_id(self, vcn_id: str) -> None:
        """Delete an empty VCN, or safely dismantle a VCN created by this app."""
        vcn = self.get_vcn_by_id(vcn_id)
        subnets = self._list_vcn_resources(self.vn_client.list_subnets, vcn_id)
        gateways = self._list_vcn_resources(
            self.vn_client.list_internet_gateways, vcn_id
        )
        unsupported_dependencies = [
            *self._list_vcn_resources(self.vn_client.list_nat_gateways, vcn_id),
            *self._list_vcn_resources(self.vn_client.list_service_gateways, vcn_id),
            *self._list_vcn_resources(self.vn_client.list_local_peering_gateways, vcn_id),
            *self._list_vcn_resources(self.vn_client.list_drg_attachments, vcn_id),
            *self._list_vcn_resources(self.vn_client.list_network_security_groups, vcn_id),
            *self._list_vcn_resources(self.vn_client.list_vlans, vcn_id),
        ]
        route_tables = self._list_vcn_resources(self.vn_client.list_route_tables, vcn_id)
        security_lists = self._list_vcn_resources(self.vn_client.list_security_lists, vcn_id)
        dhcp_options = self._list_vcn_resources(self.vn_client.list_dhcp_options, vcn_id)
        unsupported_dependencies.extend(
            table for table in route_tables if table.id != vcn.default_route_table_id
        )
        unsupported_dependencies.extend(
            item for item in security_lists if item.id != vcn.default_security_list_id
        )
        unsupported_dependencies.extend(
            item for item in dhcp_options if item.id != vcn.default_dhcp_options_id
        )

        helper_owned = is_oci_helper_managed(vcn)
        if (subnets or gateways or unsupported_dependencies) and not helper_owned:
            raise ValueError(
                "该 VCN 含有子网或网关；为避免误删用户网络，本工具只级联删除带受管标签的 VCN"
            )
        if helper_owned:
            unmanaged = [
                self._resource_label(item)
                for item in [*subnets, *gateways]
                if not is_oci_helper_managed(item)
            ]
            if unmanaged:
                raise ValueError(
                    f"VCN 中包含非本工具管理的网络资源，已拒绝级联删除: {', '.join(unmanaged)}"
                )
            if unsupported_dependencies:
                labels = ", ".join(
                    self._resource_label(item) for item in unsupported_dependencies
                )
                raise ValueError(f"VCN 中包含本工具不会级联删除的依赖资源: {labels}")

            gateway_ids = {gateway.id for gateway in gateways}
            default_route_table = next(
                (table for table in route_tables if table.id == vcn.default_route_table_id),
                None,
            )
            if default_route_table is None:
                default_route_table = self.vn_client.get_route_table(
                    rt_id=vcn.default_route_table_id
                ).data
            unrelated_rules = [
                rule
                for rule in (default_route_table.route_rules or [])
                if rule.network_entity_id not in gateway_ids
            ]
            if unrelated_rules:
                raise ValueError("VCN 默认路由表包含非 Internet Gateway 路由，已拒绝级联删除")

            for subnet in subnets:
                private_ips = oci.pagination.list_call_get_all_results(
                    self.vn_client.list_private_ips,
                    subnet_id=subnet.id,
                ).data or []
                ipv6s = oci.pagination.list_call_get_all_results(
                    self.vn_client.list_ipv6s,
                    subnet_id=subnet.id,
                ).data or []
                if private_ips or ipv6s:
                    raise ValueError(
                        f"子网 {self._resource_label(subnet)} 中仍有 IP 资源，已拒绝级联删除"
                    )

            for subnet in subnets:
                response = self.vn_client.get_subnet(subnet_id=subnet.id)
                self.vn_client.delete_subnet(
                    subnet_id=subnet.id,
                    if_match=response.headers.get("etag"),
                )
                self._wait_until_deleted(self.vn_client.get_subnet, {"subnet_id": subnet.id})

            if gateways:
                route_response = self.vn_client.get_route_table(rt_id=vcn.default_route_table_id)
                remaining_rules = [
                    rule
                    for rule in (route_response.data.route_rules or [])
                    if rule.network_entity_id not in gateway_ids
                ]
                updated_route_table = self.vn_client.update_route_table(
                    rt_id=vcn.default_route_table_id,
                    update_route_table_details=oci.core.models.UpdateRouteTableDetails(
                        route_rules=remaining_rules
                    ),
                    if_match=route_response.headers.get("etag"),
                )
                oci.wait_until(
                    self.vn_client,
                    updated_route_table,
                    "lifecycle_state",
                    "AVAILABLE",
                    max_wait_seconds=300,
                )
                for gateway in gateways:
                    response = self.vn_client.get_internet_gateway(ig_id=gateway.id)
                    self.vn_client.delete_internet_gateway(
                        ig_id=gateway.id,
                        if_match=response.headers.get("etag"),
                    )
                    self._wait_until_deleted(
                        self.vn_client.get_internet_gateway,
                        {"ig_id": gateway.id},
                    )

        response = self.vn_client.get_vcn(vcn_id=vcn_id)
        self.vn_client.delete_vcn(vcn_id=vcn_id, if_match=response.headers.get("etag"))
        self._wait_until_deleted(self.vn_client.get_vcn, {"vcn_id": vcn_id})
        logger.info("已删除 VCN: {}", vcn_id)

    def _list_vcn_resources(self, list_method, vcn_id: str) -> list:
        response = oci.pagination.list_call_get_all_results(
            list_method,
            compartment_id=self.compartment_id,
            vcn_id=vcn_id,
        )
        return [
            item
            for item in (response.data or [])
            if getattr(item, "lifecycle_state", None) not in {"TERMINATED", "DELETED"}
        ]

    @staticmethod
    def _resource_label(resource) -> str:
        return getattr(resource, "display_name", None) or getattr(resource, "id", "unknown")

    @staticmethod
    def _wait_until_deleted(getter, kwargs: dict, timeout: int = 300) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                response = getter(**kwargs)
                if getattr(response.data, "lifecycle_state", None) == "TERMINATED":
                    return
            except oci.exceptions.ServiceError as exc:
                if exc.status == 404:
                    return
                raise
            time.sleep(2)
        raise TimeoutError("等待 OCI 网络资源删除超时")

    def list_subnets(self, vcn_id: str) -> list:
        """列出指定 VCN 的子网"""
        response = oci.pagination.list_call_get_all_results(
            self.vn_client.list_subnets, compartment_id=self.compartment_id, vcn_id=vcn_id
        )
        return [s for s in (response.data or []) if s.lifecycle_state == "AVAILABLE"]

    def get_security_list(self, security_list_id: str) -> Any:
        """获取安全列表详情"""
        response = self.vn_client.get_security_list(security_list_id=security_list_id)
        return response.data

    def list_security_rule(self, vcn) -> Any:
        """
        获取 VCN 默认安全列表

        对应 Java: OracleInstanceFetcher.listSecurityRule()

        参数:
            vcn: VCN 对象

        返回:
            SecurityList 对象（含 ingress/egress rules）
        """
        return self.get_security_list(vcn.default_security_list_id)

    def list_security_rule_with_etag(self, vcn) -> tuple[Any, str | None]:
        response = self.vn_client.get_security_list(
            security_list_id=vcn.default_security_list_id
        )
        return response.data, response.headers.get("etag")

    def release_security_rule(
        self, vcn, source_cidr: str = "0.0.0.0/0", source_cidr_v6: str = "::/0"
    ) -> None:
        """
        一键放行所有端口和协议

        对应 Java: OracleInstanceFetcher.releaseSecurityRule()
        """
        security_list, etag = self.list_security_rule_with_etag(vcn)

        # 全协议入站规则 (IPv4 + IPv6)
        allow_ingress = [
            oci.core.models.IngressSecurityRule(
                protocol="all",
                source=source_cidr,
                source_type="CIDR_BLOCK",
                is_stateless=False,
                description="Allow all ingress IPv4",
            ),
            oci.core.models.IngressSecurityRule(
                protocol="all",
                source=source_cidr_v6,
                source_type="CIDR_BLOCK",
                is_stateless=False,
                description="Allow all ingress IPv6",
            ),
        ]

        # 全协议出站规则 (IPv4 + IPv6)
        allow_egress = [
            oci.core.models.EgressSecurityRule(
                protocol="all",
                destination=source_cidr,
                destination_type="CIDR_BLOCK",
                is_stateless=False,
                description="Allow all egress IPv4",
            ),
            oci.core.models.EgressSecurityRule(
                protocol="all",
                destination=source_cidr_v6,
                destination_type="CIDR_BLOCK",
                is_stateless=False,
                description="Allow all egress IPv6",
            ),
        ]

        ingress_rules = list(security_list.ingress_security_rules or [])
        for rule in allow_ingress:
            if not any(
                current.protocol == rule.protocol
                and current.source == rule.source
                and current.source_type == rule.source_type
                for current in ingress_rules
            ):
                ingress_rules.append(rule)

        egress_rules = list(security_list.egress_security_rules or [])
        for rule in allow_egress:
            if not any(
                current.protocol == rule.protocol
                and current.destination == rule.destination
                and current.destination_type == rule.destination_type
                for current in egress_rules
            ):
                egress_rules.append(rule)

        self.update_security_list(
            security_list_id=security_list.id,
            ingress_rules=ingress_rules,
            egress_rules=egress_rules,
            if_match=etag,
        )
        logger.info(f"已放行 VCN [{vcn.display_name}] 所有端口和协议")

    def update_security_list(
        self,
        security_list_id: str | None = None,
        ingress_rules: list | None = None,
        egress_rules: list | None = None,
        vcn=None,
        if_match: str | None = None,
    ) -> Any:
        """
        更新安全列表规则

        支持两种调用方式:
            1. update_security_list(security_list_id=..., ingress_rules=..., egress_rules=...)
            2. update_security_list(vcn, ingress_rules=..., egress_rules=...)
        """
        # 兼容 service 层传 vcn 对象的调用方式
        if vcn is not None and security_list_id is None:
            security_list_id = vcn.default_security_list_id

        update_details = oci.core.models.UpdateSecurityListDetails()
        if ingress_rules is not None:
            update_details.ingress_security_rules = ingress_rules
        if egress_rules is not None:
            update_details.egress_security_rules = egress_rules

        response = self.vn_client.update_security_list(
            security_list_id=security_list_id,
            update_security_list_details=update_details,
            if_match=if_match,
        )
        return response.data

    # ============================
    #  IP 地址管理
    # ============================

    def list_public_ips(self, instance_id: str) -> list[str]:
        """获取实例的所有公网 IP"""
        ips = []
        vnic_attachments = self.list_vnic_attachments(instance_id)
        for attachment in vnic_attachments:
            if attachment.lifecycle_state == "ATTACHED":
                vnic = self.get_vnic(attachment.vnic_id)
                if vnic.public_ip:
                    ips.append(vnic.public_ip)
        return ips

    def get_private_ip_id(self, vnic_id: str) -> str | None:
        """获取 VNIC 的私有 IP OCID"""
        response = oci.pagination.list_call_get_all_results(
            self.vn_client.list_private_ips, vnic_id=vnic_id
        )
        addresses = response.data or []
        primary = next((item for item in addresses if item.is_primary), None)
        if primary or addresses:
            return (primary or addresses[0]).id
        return None

    def delete_public_ip_by_private_ip(self, private_ip_id: str) -> None:
        """通过私有 IP 删除关联的临时公网 IP，保留固定地址。"""
        try:
            response = self.vn_client.get_public_ip_by_private_ip_id(
                get_public_ip_by_private_ip_id_details=(
                    oci.core.models.GetPublicIpByPrivateIpIdDetails(
                        private_ip_id=private_ip_id
                    )
                )
            )
        except oci.exceptions.ServiceError as exc:
            if exc.status == 404:
                return
            raise
        if response.data:
            if getattr(response.data, "lifetime", None) != "EPHEMERAL":
                raise ValueError("当前公网 IP 不是临时地址，已停止换 IP 以保护保留地址")
            self.vn_client.delete_public_ip(
                public_ip_id=response.data.id,
                if_match=response.headers.get("etag"),
            )
            self._wait_until_deleted(
                self.vn_client.get_public_ip,
                {"public_ip_id": response.data.id},
            )
            logger.info(f"已删除公网 IP: {response.data.ip_address}")

    def create_public_ip(self, private_ip_id: str, opc_retry_token: str) -> str:
        """为私有 IP 创建新的临时公网 IP"""
        response = self.vn_client.create_public_ip(
            create_public_ip_details=oci.core.models.CreatePublicIpDetails(
                compartment_id=self.compartment_id,
                lifetime="EPHEMERAL",
                private_ip_id=private_ip_id,
            ),
            opc_retry_token=opc_retry_token,
        )
        new_ip = response.data.ip_address
        logger.info(f"已创建新公网 IP: {new_ip}")
        return new_ip

    def reassign_ephemeral_public_ip(self, vnic) -> str:
        """
        重新分配临时公网 IP（换 IP）

        对应 Java: OracleInstanceFetcher.reassignEphemeralPublicIp()

        流程：
            1. 获取 VNIC 的私有 IP
            2. 删除旧的公网 IP
            3. 创建新的公网 IP

        参数:
            vnic: VNIC 对象

        返回:
            新的公网 IP 地址
        """
        private_ip_id = self.get_private_ip_id(vnic.id)
        if not private_ip_id:
            raise Exception(f"未找到 VNIC {vnic.id} 的私有 IP")

        self.delete_public_ip_by_private_ip(private_ip_id)

        # OCI may need a short propagation window before a new ephemeral IP can
        # be associated with the same private IP.
        last_error = None
        new_ip = None
        retry_token = str(uuid.uuid4())
        for delay in (2, 4, 8):
            time.sleep(delay)
            try:
                new_ip = self.create_public_ip(private_ip_id, retry_token)
                break
            except Exception as exc:
                last_error = exc
                retryable = is_retryable_oci_error(exc) or (
                    isinstance(exc, oci.exceptions.ServiceError) and exc.status == 409
                )
                if not retryable:
                    raise
                try:
                    reconciled = self._get_public_ip_address(private_ip_id)
                except Exception as reconcile_exc:
                    if not is_retryable_oci_error(reconcile_exc):
                        raise
                    logger.warning(
                        "确认公网 IP 创建结果时遇到临时错误，将继续重试: {}",
                        reconcile_exc,
                    )
                    reconciled = None
                if reconciled:
                    new_ip = reconciled
                    break
        if new_ip is None:
            if last_error is not None:
                raise last_error
            raise RuntimeError("创建新公网 IP 失败") from last_error
        logger.info(f"换 IP 成功: VNIC={vnic.id}, 新IP={new_ip}")
        return new_ip

    def _get_public_ip_address(self, private_ip_id: str) -> str | None:
        try:
            response = self.vn_client.get_public_ip_by_private_ip_id(
                get_public_ip_by_private_ip_id_details=(
                    oci.core.models.GetPublicIpByPrivateIpIdDetails(
                        private_ip_id=private_ip_id
                    )
                )
            )
            return getattr(response.data, "ip_address", None)
        except oci.exceptions.ServiceError as exc:
            if exc.status == 404:
                return None
            raise

    def create_ipv6(self, vnic, vcn) -> Any:
        """
        为 VNIC 创建 IPv6 地址

        对应 Java: OracleInstanceFetcher.createIpv6()

        参数:
            vnic: VNIC 对象
            vcn: VCN 对象

        返回:
            IPv6 对象
        """
        existing = [
            item
            for item in (
                oci.pagination.list_call_get_all_results(
                    self.vn_client.list_ipv6s,
                    vnic_id=vnic.id,
                ).data
                or []
            )
            if getattr(item, "lifecycle_state", None) not in {"TERMINATED", "DELETED"}
        ]
        if existing:
            subnet = self.vn_client.get_subnet(subnet_id=vnic.subnet_id).data
            self._ensure_ipv6_internet_route(vcn, subnet)
            return existing[0]

        # 确保 VCN 有 IPv6 CIDR
        if not vcn.ipv6_cidr_blocks:
            operations = oci.core.VirtualNetworkClientCompositeOperations(self.vn_client)
            try:
                work_request = operations.add_ipv6_vcn_cidr_and_wait_for_work_request(
                    vcn.id,
                    operation_kwargs={
                        "add_vcn_ipv6_cidr_details": oci.core.models.AddVcnIpv6CidrDetails(
                            is_oracle_gua_allocation_enabled=True
                        ),
                        "opc_retry_token": str(uuid.uuid4()),
                    },
                    waiter_kwargs={"max_wait_seconds": 600},
                )
                status = (getattr(work_request.data, "status", None) or "").upper()
                if status and status != "SUCCEEDED":
                    raise RuntimeError(f"OCI 添加 VCN IPv6 前缀失败: {status}")
            finally:
                try:
                    operations._work_request_client.base_client.session.close()
                except Exception as exc:
                    logger.debug("关闭 OCI Work Request HTTP 会话失败: {}", exc)
            vcn = self.vn_client.get_vcn(vcn_id=vcn.id).data
            if not vcn.ipv6_cidr_blocks:
                raise RuntimeError("OCI 未返回已分配的 VCN IPv6 前缀")
            logger.info(f"已为 VCN {vcn.id} 添加 IPv6 CIDR")

        # 确保子网有 IPv6 CIDR
        subnet_response = self.vn_client.get_subnet(subnet_id=vnic.subnet_id)
        subnet = subnet_response.data
        if not subnet.ipv6_cidr_blocks:
            subnet_cidr = self._find_available_ipv6_subnet_cidr(vcn)
            response = self.vn_client.update_subnet(
                subnet_id=subnet.id,
                update_subnet_details=oci.core.models.UpdateSubnetDetails(
                    ipv6_cidr_blocks=[subnet_cidr],
                ),
                if_match=subnet_response.headers.get("etag"),
            )
            subnet = oci.wait_until(
                self.vn_client,
                response,
                "lifecycle_state",
                "AVAILABLE",
                max_wait_seconds=300,
            ).data

        self._ensure_ipv6_internet_route(vcn, subnet)

        # 创建 IPv6
        response = self.vn_client.create_ipv6(
            create_ipv6_details=oci.core.models.CreateIpv6Details(
                vnic_id=vnic.id,
            ),
            opc_retry_token=str(uuid.uuid4()),
        )
        logger.info(f"已创建 IPv6: {response.data.ip_address}")
        return response.data

    def _find_available_ipv6_subnet_cidr(self, vcn) -> str:
        occupied = {
            str(ipaddress.ip_network(cidr))
            for subnet in self._list_vcn_resources(self.vn_client.list_subnets, vcn.id)
            for cidr in (getattr(subnet, "ipv6_cidr_blocks", None) or [])
        }
        for cidr in vcn.ipv6_cidr_blocks or []:
            network = ipaddress.ip_network(cidr)
            if network.version != 6 or network.prefixlen > 64:
                continue
            candidates = [network] if network.prefixlen == 64 else network.subnets(new_prefix=64)
            for candidate in candidates:
                if str(candidate) not in occupied:
                    return str(candidate)
        raise ValueError(f"VCN {vcn.id} 没有可用的 /64 IPv6 子网地址段")

    def _ensure_ipv6_internet_route(self, vcn, subnet) -> None:
        if getattr(subnet, "prohibit_internet_ingress", None) is True:
            return
        route_response = self.vn_client.get_route_table(rt_id=subnet.route_table_id)
        route_rules = list(route_response.data.route_rules or [])
        if any(rule.destination == "::/0" for rule in route_rules):
            return
        gateways = self._list_vcn_resources(self.vn_client.list_internet_gateways, vcn.id)
        gateway = next(
            (item for item in gateways if getattr(item, "is_enabled", False)),
            None,
        )
        if gateway is None:
            logger.warning("VCN {} 没有启用的 Internet Gateway，IPv6 将不具备公网路由", vcn.id)
            return
        route_rules.append(
            oci.core.models.RouteRule(
                destination="::/0",
                destination_type="CIDR_BLOCK",
                network_entity_id=gateway.id,
            )
        )
        response = self.vn_client.update_route_table(
            rt_id=route_response.data.id,
            update_route_table_details=oci.core.models.UpdateRouteTableDetails(
                route_rules=route_rules
            ),
            if_match=route_response.headers.get("etag"),
        )
        oci.wait_until(
            self.vn_client,
            response,
            "lifecycle_state",
            "AVAILABLE",
            max_wait_seconds=300,
        )

    # ============================
    #  引导卷管理
    # ============================

    def list_boot_volumes(self, availability_domain: str | None = None) -> list:
        """
        列出引导卷

        支持两种调用方式:
            1. list_boot_volumes("ad_name") — 指定可用域
            2. list_boot_volumes() — 遍历所有可用域

        对应 Java: OracleInstanceFetcher.listBootVolumes()
        """
        if availability_domain:
            response = oci.pagination.list_call_get_all_results(
                self.block_storage_client.list_boot_volumes,
                availability_domain=availability_domain,
                compartment_id=self.compartment_id,
            )
            return response.data or []
        else:
            # 遍历所有可用域
            all_volumes = []
            ads = self.get_availability_domains()
            for ad in ads:
                response = oci.pagination.list_call_get_all_results(
                    self.block_storage_client.list_boot_volumes,
                    availability_domain=ad.name,
                    compartment_id=self.compartment_id,
                )
                all_volumes.extend(response.data or [])
            return all_volumes

    def list_boot_volume_attachments(self, instance_id: str | None = None) -> list:
        """List boot-volume attachments across every availability domain."""
        attachments = []
        for ad in self.get_availability_domains():
            kwargs = {
                "availability_domain": ad.name,
                "compartment_id": self.compartment_id,
            }
            if instance_id:
                kwargs["instance_id"] = instance_id
            response = oci.pagination.list_call_get_all_results(
                self.compute_client.list_boot_volume_attachments, **kwargs
            )
            attachments.extend(response.data or [])
        return attachments

    def get_boot_volume_by_instance_id(self, instance_id: str) -> Any:
        """
        按实例 ID 获取引导卷

        对应 Java: OracleInstanceFetcher.getBootVolumeByInstanceId()
        """
        attachments = self.list_boot_volume_attachments(instance_id)

        for att in attachments:
            if att.lifecycle_state == "ATTACHED":
                return self.block_storage_client.get_boot_volume(
                    boot_volume_id=att.boot_volume_id
                ).data

        raise Exception(f"未找到实例 {instance_id} 的引导卷")

    def update_boot_volume_cfg(
        self,
        boot_volume_id: str,
        size_in_gbs: int | None,
        vpus_per_gb: int | None,
    ) -> Any:
        """
        更新引导卷配置（大小和性能）

        对应 Java: OracleInstanceFetcher.updateBootVolumeCfg()
        """
        current = self.get_boot_volume(boot_volume_id)
        if size_in_gbs is not None and size_in_gbs < int(current.size_in_gbs):
            raise ValueError("引导卷只支持扩容，不能缩小")
        if vpus_per_gb is not None and (
            vpus_per_gb not in {10, 20} and not 30 <= vpus_per_gb <= 120
        ):
            raise ValueError("引导卷 VPU/GB 只支持 10、20 或 30 至 120")
        details: dict[str, int] = {}
        if size_in_gbs is not None and size_in_gbs > int(current.size_in_gbs):
            details["size_in_gbs"] = size_in_gbs
        current_vpus = getattr(current, "vpus_per_gb", None)
        if vpus_per_gb is not None and (
            current_vpus is None or vpus_per_gb != int(current_vpus)
        ):
            details["vpus_per_gb"] = vpus_per_gb
        if not details:
            return current
        response = self.block_storage_client.update_boot_volume(
            boot_volume_id=boot_volume_id,
            update_boot_volume_details=oci.core.models.UpdateBootVolumeDetails(**details),
        )
        logger.info(
            "已更新引导卷配置: {}, 大小={}, VPU={}",
            boot_volume_id,
            size_in_gbs,
            vpus_per_gb,
        )
        return response.data

    def get_boot_volume(self, boot_volume_id: str) -> Any:
        return self.block_storage_client.get_boot_volume(boot_volume_id=boot_volume_id).data

    def terminate_boot_volume(self, boot_volume_id: str) -> None:
        """终止引导卷"""
        self.block_storage_client.delete_boot_volume(boot_volume_id=boot_volume_id)
        logger.info(f"已终止引导卷: {boot_volume_id}")

    # ============================
    #  限额查询
    # ============================

    def list_services(self) -> list:
        response = oci.pagination.list_call_get_all_results(
            self.limits_client.list_services, compartment_id=self.compartment_id
        )
        return response.data or []

    def list_limit_definitions(self, service_name: str | None = None) -> list:
        kwargs = {"compartment_id": self.compartment_id}
        if service_name:
            kwargs["service_name"] = service_name
        response = oci.pagination.list_call_get_all_results(
            self.limits_client.list_limit_definitions, **kwargs
        )
        return response.data or []

    def list_limit_values(self, service_name: str, limit_name: str) -> list:
        response = oci.pagination.list_call_get_all_results(
            self.limits_client.list_limit_values,
            compartment_id=self.compartment_id,
            service_name=service_name,
            name=limit_name,
        )
        return response.data or []

    def get_resource_availability(
        self, service_name: str, limit_name: str, availability_domain: str | None
    ) -> dict:
        """获取资源可用性"""
        kwargs = {
            "service_name": service_name,
            "limit_name": limit_name,
            "compartment_id": self.compartment_id,
        }
        if availability_domain:
            kwargs["availability_domain"] = availability_domain
        data = self.limits_client.get_resource_availability(**kwargs).data
        return {"available": data.available, "used": data.used}

    # ============================
    #  流量统计
    # ============================

    def get_traffic_data(
        self,
        instance_id: str,
        *,
        begin_time: datetime | None = None,
        end_time: datetime | None = None,
        namespace: str = "oci_computeagent",
        in_query: str | None = None,
        out_query: str | None = None,
    ) -> dict:
        """
        获取实例流量统计数据

        对应 Java: OracleInstanceFetcher.getTrafficData()
        通过 OCI Monitoring API 获取网络流量指标

        返回:
            {"labels": [...], "ingress": [...], "egress": [...]}
        """
        monitoring_client = oci.monitoring.MonitoringClient(
            self._config,
            timeout=(10, settings.oci_request_timeout_seconds),
        )
        try:
            resolved_end_time = end_time or datetime.now(UTC)
            resolved_start_time = begin_time or (resolved_end_time - timedelta(hours=1))
            queries = {
                "ingress": in_query
                or f'NetworksBytesIn[5m]{{resourceId = "{instance_id}"}}.increment()',
                "egress": out_query
                or f'NetworksBytesOut[5m]{{resourceId = "{instance_id}"}}.increment()',
            }
            series: dict[str, dict[str, float]] = {"ingress": {}, "egress": {}}

            for direction, query in queries.items():
                response = monitoring_client.summarize_metrics_data(
                    compartment_id=self.compartment_id,
                    summarize_metrics_data_details=oci.monitoring.models.SummarizeMetricsDataDetails(
                        namespace=namespace,
                        query=query,
                        start_time=resolved_start_time,
                        end_time=resolved_end_time,
                    ),
                )

                for item in response.data or []:
                    for dp in item.aggregated_datapoints or []:
                        if dp.timestamp:
                            timestamp = dp.timestamp.isoformat()
                            series[direction][timestamp] = series[direction].get(
                                timestamp, 0
                            ) + (dp.value or 0)

            timestamps = sorted(set().union(*(values.keys() for values in series.values())))
            return {
                "labels": [datetime.fromisoformat(value).strftime("%H:%M") for value in timestamps],
                "ingress": [
                    round(series["ingress"].get(value, 0) / (1024 * 1024), 2)
                    for value in timestamps
                ],
                "egress": [
                    round(series["egress"].get(value, 0) / (1024 * 1024), 2)
                    for value in timestamps
                ],
            }
        finally:
            try:
                monitoring_client.base_client.session.close()
            except Exception as exc:
                logger.debug("关闭 OCI Monitoring HTTP 会话失败: {}", exc)
