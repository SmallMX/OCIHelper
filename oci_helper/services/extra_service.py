"""Retained OCI networking, storage, tenant, traffic and limits use cases."""

from __future__ import annotations

import hashlib

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from core.cache import cache
from core.oracle_fetcher import OracleInstanceFetcher
from enums.security_rule import SecurityRuleProtocol
from exceptions import OciException
from schemas.other_schemas import (
    AddEgressSecurityRuleParams,
    AddIngressSecurityRuleParams,
    GetTrafficDataParams,
)
from services.common import run_oci
from services.oci_service import OciService
from utils.common import paginate_list


class SecurityRuleService:
    @staticmethod
    async def get_page(
        oci_cfg_id: str,
        vcn_id: str,
        rule_type: int,
        keyword: str | None,
        current_page: int,
        page_size: int,
        clean_relaunch: bool,
        db: AsyncSession,
    ) -> dict:
        direction = "ingress" if rule_type == 0 else "egress"
        list_key = f"security_rule:{oci_cfg_id}:{vcn_id}:{direction}:list"
        map_key = f"security_rule:{oci_cfg_id}:{vcn_id}:{direction}:map"
        if clean_relaunch:
            cache.remove(list_key)
            cache.remove(map_key)

        rules = cache.get(list_key)
        if rules is None:
            user = await OciService._get_user(oci_cfg_id, db)

            def operation() -> tuple[list[dict], dict]:
                with OracleInstanceFetcher(
                    OciService._build_oci_config(user), user.username or ""
                ) as fetcher:
                    security_list = fetcher.list_security_rule(fetcher.get_vcn_by_id(vcn_id))
                    source_rules = (
                        security_list.ingress_security_rules
                        if rule_type == 0
                        else security_list.egress_security_rules
                    ) or []
                    parsed: list[dict] = []
                    mapping: dict = {}
                    for index, rule in enumerate(source_rules):
                        rule_id = _rule_id(direction, index, rule)
                        parsed.append(
                            _parse_ingress_rule(rule, rule_id)
                            if rule_type == 0
                            else _parse_egress_rule(rule, rule_id)
                        )
                        mapping[rule_id] = rule
                    return parsed, mapping

            rules, mapping = await run_oci(operation, "获取安全规则失败")
            cache.put(list_key, rules, ttl=600)
            cache.put(map_key, mapping, ttl=600)

        filtered = _filter_rules(rules, keyword)
        records, total = paginate_list(filtered, current_page, page_size)
        return _page(records, total, current_page, page_size)

    @staticmethod
    async def add_ingress(params: AddIngressSecurityRuleParams, db: AsyncSession) -> None:
        user = await OciService._get_user(params.oci_cfg_id, db)

        def operation() -> None:
            import oci

            with OracleInstanceFetcher(
                OciService._build_oci_config(user), user.username or ""
            ) as fetcher:
                vcn = fetcher.get_vcn_by_id(params.vcn_id)
                security_list, etag = fetcher.list_security_rule_with_etag(vcn)
                rule = params.inbound_rule
                options = _protocol_options(
                    oci, rule.protocol, rule.source_port, rule.destination_port, rule.icmp_options
                )
                ingress = oci.core.models.IngressSecurityRule(
                    protocol=rule.protocol,
                    source=rule.source,
                    source_type=rule.source_type,
                    is_stateless=rule.is_stateless,
                    description=rule.description,
                    **options,
                )
                fetcher.update_security_list(
                    security_list_id=security_list.id,
                    ingress_rules=[*(security_list.ingress_security_rules or []), ingress],
                    egress_rules=security_list.egress_security_rules,
                    if_match=etag,
                )

        await run_oci(operation, "添加入站安全规则失败")
        SecurityRuleService.clear_cache(params.oci_cfg_id, params.vcn_id)

    @staticmethod
    async def add_egress(params: AddEgressSecurityRuleParams, db: AsyncSession) -> None:
        user = await OciService._get_user(params.oci_cfg_id, db)

        def operation() -> None:
            import oci

            with OracleInstanceFetcher(
                OciService._build_oci_config(user), user.username or ""
            ) as fetcher:
                vcn = fetcher.get_vcn_by_id(params.vcn_id)
                security_list, etag = fetcher.list_security_rule_with_etag(vcn)
                rule = params.outbound_rule
                options = _protocol_options(
                    oci, rule.protocol, rule.source_port, rule.destination_port, rule.icmp_options
                )
                egress = oci.core.models.EgressSecurityRule(
                    protocol=rule.protocol,
                    destination=rule.destination,
                    destination_type=rule.destination_type,
                    is_stateless=rule.is_stateless,
                    description=rule.description,
                    **options,
                )
                fetcher.update_security_list(
                    security_list_id=security_list.id,
                    ingress_rules=security_list.ingress_security_rules,
                    egress_rules=[*(security_list.egress_security_rules or []), egress],
                    if_match=etag,
                )

        await run_oci(operation, "添加出站安全规则失败")
        SecurityRuleService.clear_cache(params.oci_cfg_id, params.vcn_id)

    @staticmethod
    async def remove_rules(
        oci_cfg_id: str,
        vcn_id: str,
        rule_type: int,
        rule_ids: list[str],
        db: AsyncSession,
    ) -> None:
        direction = "ingress" if rule_type == 0 else "egress"
        map_key = f"security_rule:{oci_cfg_id}:{vcn_id}:{direction}:map"
        mapping = cache.get(map_key)
        if mapping is None:
            raise OciException(-1, "规则缓存已过期，请刷新列表后重试")
        unknown = [rule_id for rule_id in rule_ids if rule_id not in mapping]
        if unknown:
            raise OciException(-1, "待删除规则不存在或已变化，请刷新列表")
        user = await OciService._get_user(oci_cfg_id, db)

        def operation() -> None:
            with OracleInstanceFetcher(
                OciService._build_oci_config(user), user.username or ""
            ) as fetcher:
                vcn = fetcher.get_vcn_by_id(vcn_id)
                security_list, etag = fetcher.list_security_rule_with_etag(vcn)
                current_rules = (
                    security_list.ingress_security_rules
                    if rule_type == 0
                    else security_list.egress_security_rules
                ) or []
                current_mapping = {
                    _rule_id(direction, index, rule): rule
                    for index, rule in enumerate(current_rules)
                }
                if set(current_mapping) != set(mapping):
                    raise OciException(
                        -1,
                        "安全规则已被其他操作修改，请刷新列表后重试",
                    )
                current_remaining = [
                    rule for rule_id, rule in current_mapping.items() if rule_id not in rule_ids
                ]
                fetcher.update_security_list(
                    security_list_id=security_list.id,
                    ingress_rules=current_remaining
                    if rule_type == 0
                    else security_list.ingress_security_rules,
                    egress_rules=current_remaining
                    if rule_type == 1
                    else security_list.egress_security_rules,
                    if_match=etag,
                )

        await run_oci(operation, "删除安全规则失败")
        SecurityRuleService.clear_cache(oci_cfg_id, vcn_id)

    @staticmethod
    def clear_cache(oci_cfg_id: str, vcn_id: str) -> None:
        for direction in ("ingress", "egress"):
            cache.remove(f"security_rule:{oci_cfg_id}:{vcn_id}:{direction}:list")
            cache.remove(f"security_rule:{oci_cfg_id}:{vcn_id}:{direction}:map")


class VcnService:
    @staticmethod
    async def get_page(
        oci_cfg_id: str,
        keyword: str | None,
        current_page: int,
        page_size: int,
        clean_relaunch: bool,
        db: AsyncSession,
    ) -> dict:
        cache_key = f"vcn:{oci_cfg_id}:list"
        if clean_relaunch:
            cache.remove(cache_key)
        items = cache.get(cache_key)
        if items is None:
            user = await OciService._get_user(oci_cfg_id, db)

            def operation() -> list[dict]:
                with OracleInstanceFetcher(
                    OciService._build_oci_config(user), user.username or ""
                ) as fetcher:
                    return [
                        {
                            "id": vcn.id,
                            "displayName": vcn.display_name,
                            "status": vcn.lifecycle_state,
                            "visibility": fetcher.check_vcn_is_public(vcn),
                            "createTime": vcn.time_created.strftime("%Y-%m-%d %H:%M:%S")
                            if vcn.time_created
                            else None,
                        }
                        for vcn in fetcher.list_vcn()
                    ]

            items = await run_oci(operation, "获取 VCN 列表失败")
            cache.put(cache_key, items, ttl=600)
        filtered = _filter_by_name(items, keyword)
        records, total = paginate_list(filtered, current_page, page_size)
        return _page(records, total, current_page, page_size)

    @staticmethod
    async def remove_vcn(oci_cfg_id: str, vcn_ids: list[str], db: AsyncSession) -> None:
        user = await OciService._get_user(oci_cfg_id, db)

        def operation() -> None:
            with OracleInstanceFetcher(
                OciService._build_oci_config(user), user.username or ""
            ) as fetcher:
                for vcn_id in vcn_ids:
                    fetcher.delete_vcn_by_id(vcn_id)

        await run_oci(operation, "删除 VCN 失败")
        cache.remove(f"vcn:{oci_cfg_id}:list")


class BootVolumeService:
    @staticmethod
    async def get_page(
        oci_cfg_id: str,
        keyword: str | None,
        current_page: int,
        page_size: int,
        clean_relaunch: bool,
        db: AsyncSession,
    ) -> dict:
        cache_key = f"boot_volume:{oci_cfg_id}:list"
        if clean_relaunch:
            cache.remove(cache_key)
        items = cache.get(cache_key)
        if items is None:
            user = await OciService._get_user(oci_cfg_id, db)

            def operation() -> list[dict]:
                with OracleInstanceFetcher(
                    OciService._build_oci_config(user), user.username or ""
                ) as fetcher:
                    attached_ids = {
                        item.boot_volume_id
                        for item in fetcher.list_boot_volume_attachments()
                        if item.lifecycle_state in {"ATTACHED", "ATTACHING"}
                    }
                    return [
                        {
                            "id": volume.id,
                            "displayName": volume.display_name,
                            "status": volume.lifecycle_state,
                            "sizeInGBs": volume.size_in_gbs,
                            "vpusPerGB": getattr(volume, "vpus_per_gb", None),
                            "attached": volume.id in attached_ids,
                            "createTime": volume.time_created.strftime("%Y-%m-%d %H:%M:%S")
                            if volume.time_created
                            else None,
                        }
                        for volume in fetcher.list_boot_volumes()
                    ]

            items = await run_oci(operation, "获取引导卷列表失败")
            cache.put(cache_key, items, ttl=600)
        filtered = _filter_by_name(items, keyword)
        records, total = paginate_list(filtered, current_page, page_size)
        return _page(records, total, current_page, page_size)

    @staticmethod
    async def update(
        oci_cfg_id: str,
        boot_volume_id: str,
        size: int,
        vpus_per_gb: int,
        db: AsyncSession,
    ) -> None:
        user = await OciService._get_user(oci_cfg_id, db)

        def operation() -> None:
            with OracleInstanceFetcher(
                OciService._build_oci_config(user), user.username or ""
            ) as fetcher:
                fetcher.update_boot_volume_cfg(boot_volume_id, size, vpus_per_gb)

        await run_oci(operation, "更新引导卷失败")
        cache.remove(f"boot_volume:{oci_cfg_id}:list")

    @staticmethod
    async def terminate(oci_cfg_id: str, boot_volume_ids: list[str], db: AsyncSession) -> None:
        user = await OciService._get_user(oci_cfg_id, db)

        def operation() -> None:
            with OracleInstanceFetcher(
                OciService._build_oci_config(user), user.username or ""
            ) as fetcher:
                attached_ids = {
                    item.boot_volume_id
                    for item in fetcher.list_boot_volume_attachments()
                    if item.lifecycle_state in {"ATTACHED", "ATTACHING"}
                }
                conflicts = attached_ids.intersection(boot_volume_ids)
                if conflicts:
                    raise OciException(
                        -1,
                        "不能终止仍挂载到实例的引导卷，请先终止实例或分离卷",
                    )
                for volume_id in boot_volume_ids:
                    fetcher.terminate_boot_volume(volume_id)

        await run_oci(operation, "终止引导卷失败")
        cache.remove(f"boot_volume:{oci_cfg_id}:list")


class TrafficService:
    @staticmethod
    async def get_traffic_data(params: GetTrafficDataParams, db: AsyncSession) -> dict:
        user = await OciService._get_user(params.oci_cfg_id, db)
        config = OciService._build_oci_config(user)
        if params.region:
            config.region = params.region

        def operation() -> dict:
            with OracleInstanceFetcher(
                config, user.username or ""
            ) as fetcher:
                return fetcher.get_traffic_data(
                    params.instance_id,
                    begin_time=params.begin_time,
                    end_time=params.end_time,
                    namespace=params.namespace,
                    in_query=params.in_query,
                    out_query=params.out_query,
                )

        return await run_oci(operation, "获取流量数据失败")


class TenantService:
    @staticmethod
    async def get_tenant_info(oci_cfg_id: str, db: AsyncSession, region: str | None = None) -> dict:
        user = await OciService._get_user(oci_cfg_id, db)
        config = OciService._build_oci_config(user)
        if region:
            config.region = region

        def operation() -> dict:
            with OracleInstanceFetcher(config, user.username or "") as fetcher:
                return fetcher.get_tenant_info()

        return await run_oci(operation, "获取租户信息失败")


class LimitsService:
    @staticmethod
    async def get_service_names(oci_cfg_id: str, region: str | None, db: AsyncSession) -> list[str]:
        user = await OciService._get_user(oci_cfg_id, db)
        config = OciService._build_oci_config(user)
        if region:
            config.region = region

        def operation() -> list[str]:
            with OracleInstanceFetcher(config, user.username or "") as fetcher:
                return sorted(
                    item.name for item in fetcher.list_services() if getattr(item, "name", None)
                )

        return await run_oci(operation, "获取限额服务列表失败")

    @staticmethod
    async def query(
        oci_cfg_id: str,
        region: str | None,
        service_name: str | None,
        db: AsyncSession,
    ) -> dict:
        user = await OciService._get_user(oci_cfg_id, db)
        config = OciService._build_oci_config(user)
        if region:
            config.region = region

        def operation() -> dict:
            items: list[dict] = []
            with OracleInstanceFetcher(config, user.username or "") as fetcher:
                for definition in fetcher.list_limit_definitions(service_name):
                    values = fetcher.list_limit_values(definition.service_name, definition.name)
                    if not values:
                        items.append(_limit_item(definition, None, None))
                        continue
                    for value in values:
                        availability = None
                        try:
                            availability = fetcher.get_resource_availability(
                                definition.service_name,
                                definition.name,
                                value.availability_domain,
                            )
                        except Exception as exc:
                            logger.debug("限额可用量查询失败: {}", exc)
                        items.append(_limit_item(definition, value, availability))
            return {"total": len(items), "items": items}

        return await run_oci(operation, "查询限额失败")


def _protocol_options(oci, protocol: str, source: str | None, destination: str | None, icmp):
    options: dict = {}
    if protocol in {"6", "17"}:
        model = oci.core.models.TcpOptions if protocol == "6" else oci.core.models.UdpOptions
        options["tcp_options" if protocol == "6" else "udp_options"] = model(
            source_port_range=_port_range(oci, source),
            destination_port_range=_port_range(oci, destination),
        )
    elif protocol in {"1", "58"} and icmp and icmp.type is not None:
        options["icmp_options"] = oci.core.models.IcmpOptions(type=icmp.type, code=icmp.code)
    return options


def _port_range(oci, value: str | None):
    if not value:
        return None
    parts = [int(item) for item in value.split("-", 1)]
    return oci.core.models.PortRange(min=parts[0], max=parts[-1])


def _rule_id(direction: str, index: int, rule) -> str:
    digest = hashlib.sha256(f"{direction}:{index}:{rule}".encode()).hexdigest()
    return digest[:18]


def _parse_ingress_rule(rule, rule_id: str) -> dict:
    return _parse_rule(rule, rule_id, getattr(rule, "source", None))


def _parse_egress_rule(rule, rule_id: str) -> dict:
    return _parse_rule(rule, rule_id, getattr(rule, "destination", None))


def _parse_rule(rule, rule_id: str, endpoint: str | None) -> dict:
    try:
        protocol = SecurityRuleProtocol.from_code(rule.protocol).desc
    except ValueError:
        protocol = rule.protocol
    result = {
        "id": rule_id,
        "isStateless": rule.is_stateless,
        "protocol": protocol,
        "sourceOrDestination": endpoint,
        "typeAndCode": None,
        "description": rule.description,
        "sourcePort": None,
        "destinationPort": None,
    }
    if rule.protocol in {"1", "58"} and rule.icmp_options:
        result["typeAndCode"] = str(rule.icmp_options.type)
        if rule.icmp_options.code is not None:
            result["typeAndCode"] += f", {rule.icmp_options.code}"
    if rule.protocol in {"6", "17"}:
        options = rule.tcp_options if rule.protocol == "6" else rule.udp_options
        result["sourcePort"] = _format_port_range(getattr(options, "source_port_range", None))
        result["destinationPort"] = _format_port_range(
            getattr(options, "destination_port_range", None)
        )
    return result


def _format_port_range(port_range) -> str:
    if port_range is None:
        return "全部"
    return (
        str(port_range.min)
        if port_range.min == port_range.max
        else f"{port_range.min}-{port_range.max}"
    )


def _filter_rules(rules: list[dict], keyword: str | None) -> list[dict]:
    if not keyword:
        return rules
    needle = keyword.lower()
    return [
        rule for rule in rules if any(needle in str(rule.get(key) or "").lower() for key in rule)
    ]


def _filter_by_name(items: list[dict], keyword: str | None) -> list[dict]:
    if not keyword:
        return items
    needle = keyword.lower()
    return [item for item in items if needle in str(item.get("displayName") or "").lower()]


def _page(records: list, total: int, current: int, size: int) -> dict:
    return {"records": records, "total": total, "current": current, "size": size}


def _limit_item(definition, value, availability) -> dict:
    return {
        "serviceName": definition.service_name,
        "limitName": definition.name,
        "description": definition.description,
        "scopeType": getattr(definition.scope_type, "value", definition.scope_type) or "REGION",
        "availabilityDomain": getattr(value, "availability_domain", None),
        "serviceLimit": getattr(value, "value", None),
        "used": availability.get("used") if availability else None,
        "available": availability.get("available") if availability else None,
    }
