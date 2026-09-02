"""Offline smoke tests for application startup and stable API contracts."""

from __future__ import annotations

import io
import os
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import oci
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_ssh_private_key,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_runtime = tempfile.TemporaryDirectory(prefix="oci-helper-tests-")
_runtime_path = Path(_runtime.name)
os.environ.update(
    {
        "OCI_HELPER_WEB_ACCOUNT": "admin",
        "OCI_HELPER_WEB_PASSWORD": "test-password-12345",
        "OCI_HELPER_DB_PATH": str(_runtime_path / "oci.db"),
        "OCI_HELPER_KEY_DIR_PATH": str(_runtime_path / "keys"),
        "OCI_HELPER_LOG_FILE_PATH": str(_runtime_path / "oci-helper.log"),
    }
)

from fastapi.testclient import TestClient

from config import settings
from core.admin_credentials import verify_password_hash
from core.cache import ExpireCache
from core.oracle_fetcher import (
    LAUNCH_ID_TAG_KEY,
    MANAGED_TAG_KEY,
    MANAGED_TAG_VALUE,
    OracleInstanceFetcher,
    is_capacity_oci_error,
    is_retryable_oci_error,
)
from core.secrets import decrypt_secret, is_encrypted
from core.task_scheduler import TaskInfo
from database import _apply_migrations
from main import app
from models.base import Base
from models.notification_outbox import NotificationOutbox
from models.oci_create_task import OciCreateTask
from models.oci_user import OciUser
from schemas.instance_schemas import (
    AvailabilityDomainOptionRsp,
    CreateInstanceParams,
    GetImageOptionsParams,
    GetShapeOptionsParams,
    ImageOptionRsp,
    StartConsoleConnectionParams,
)
from schemas.other_schemas import (
    GetTrafficDataParams,
    IcmpOptions,
    IngressRuleInput,
    UpdateBootVolumeParams,
    VcnPageParams,
)
from schemas.response import ResponseData
from services.extra_service import _protocol_options
from services.instance_service import (
    InstanceService,
    _build_create_success_message,
    _build_instance_display_name,
    _random_retry_delay,
)
from services.notification_service import (
    dispatch_due_notifications,
    enqueue_notification,
    notification_retry_delay,
)
from services.oci_service import OciService
from telegram_bot import TelegramNotifier

TEST_SSH_PUBLIC_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIAOhB7/zzhC+HXDdGOdLwJln5NYwm6UNXx3chmQSVTG4 "
    "oci-helper-test"
)
TEST_CONSOLE_PUBLIC_KEY = (
    rsa.generate_private_key(public_exponent=65537, key_size=2048)
    .public_key()
    .public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH)
    .decode()
)


class ApplicationSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client_context.__exit__(None, None, None)
        _runtime.cleanup()

    def test_admin_credentials_update(self) -> None:
        login = self.client.post(
            "/api/sys/login",
            json={"account": "admin", "password": "test-password-12345"},
        )
        token = login.json()["data"]["token"]

        image_options = [
            {
                "operatingSystem": "Canonical Ubuntu",
                "operatingSystemVersion": "24.04",
                "label": "Canonical Ubuntu 24.04",
            }
        ]
        with patch(
            "routers.oci_router.OciService.get_image_options",
            new=AsyncMock(return_value=image_options),
        ) as get_image_options:
            response = self.client.post(
                "/api/oci/imageOptions",
                json={"ociCfgId": "cfg", "architecture": "ARM"},
                headers={"Authorization": f"Bearer {token}"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], image_options)
        self.assertEqual(get_image_options.await_args.args[:2], ("cfg", "ARM"))

        availability_domains = [
            {"name": "example:AD-1", "label": "example:AD-1"},
        ]
        with patch(
            "routers.oci_router.OciService.get_availability_domain_options",
            new=AsyncMock(return_value=availability_domains),
        ) as get_availability_domain_options:
            response = self.client.post(
                "/api/oci/availabilityDomainOptions",
                json={"ociCfgId": "cfg", "architecture": "ARM"},
                headers={"Authorization": f"Bearer {token}"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], availability_domains)
        self.assertEqual(
            get_availability_domain_options.await_args.args[:2],
            ("cfg", "ARM"),
        )
        headers = {"Authorization": f"Bearer {token}"}

        config = self.client.post("/api/sys/getSysCfg", json={}, headers=headers).json()["data"]
        self.assertEqual(config["adminAccount"], "admin")

        wrong_password = self.client.post(
            "/api/sys/updateAdminCredentials",
            json={
                "account": "renamed-admin",
                "currentPassword": "wrong-password",
                "newPassword": "new-test-password-12345",
            },
            headers=headers,
        )
        self.assertFalse(wrong_password.json()["success"])

        updated = self.client.post(
            "/api/sys/updateAdminCredentials",
            json={
                "account": "renamed-admin",
                "currentPassword": "test-password-12345",
            },
            headers=headers,
        )
        self.assertTrue(updated.json()["success"])

        with sqlite3.connect(_runtime_path / "oci.db") as connection:
            stored_hash = connection.execute(
                "SELECT value FROM oci_kv WHERE code = 'SYS_ADMIN_PASSWORD_HASH'"
            ).fetchone()[0]
        self.assertNotEqual(stored_hash, "test-password-12345")
        self.assertTrue(verify_password_hash("test-password-12345", stored_hash))
        self.assertFalse(verify_password_hash("wrong-password", stored_hash))

        expired_session = self.client.post("/api/sys/getSysCfg", json={}, headers=headers)
        self.assertEqual(expired_session.status_code, 401)

        old_login = self.client.post(
            "/api/sys/login",
            json={"account": "admin", "password": "test-password-12345"},
        )
        self.assertFalse(old_login.json()["success"])
        renamed_login = self.client.post(
            "/api/sys/login",
            json={
                "account": "renamed-admin",
                "password": "test-password-12345",
            },
        )
        self.assertTrue(renamed_login.json()["success"])
        renamed_token = renamed_login.json()["data"]["token"]

        password_updated = self.client.post(
            "/api/sys/updateAdminCredentials",
            json={
                "account": "renamed-admin",
                "currentPassword": "test-password-12345",
                "newPassword": "new-test-password-12345",
            },
            headers={"Authorization": f"Bearer {renamed_token}"},
        )
        self.assertTrue(password_updated.json()["success"])
        self.assertEqual(
            self.client.post(
                "/api/sys/getSysCfg",
                json={},
                headers={"Authorization": f"Bearer {renamed_token}"},
            ).status_code,
            401,
        )

        new_login = self.client.post(
            "/api/sys/login",
            json={
                "account": "renamed-admin",
                "password": "new-test-password-12345",
            },
        )
        self.assertTrue(new_login.json()["success"])
        new_token = new_login.json()["data"]["token"]

        restored = self.client.post(
            "/api/sys/updateAdminCredentials",
            json={
                "account": "admin",
                "currentPassword": "new-test-password-12345",
                "newPassword": "test-password-12345",
            },
            headers={"Authorization": f"Bearer {new_token}"},
        )
        self.assertTrue(restored.json()["success"])

    def test_health_login_auth_and_validation(self) -> None:
        self.assertEqual(self.client.get("/api/health").json(), {"status": "ok"})

        invalid_unicode_login = self.client.post(
            "/api/sys/login",
            json={"account": "admin", "password": "错误密码"},
        )
        self.assertEqual(invalid_unicode_login.status_code, 200)
        self.assertFalse(invalid_unicode_login.json()["success"])

        login = self.client.post(
            "/api/sys/login",
            json={"account": "admin", "password": "test-password-12345"},
        )
        self.assertEqual(login.status_code, 200)
        login_data = login.json()["data"]
        self.assertEqual(login_data["currentVersion"], settings.app_version)
        token = login_data["token"]

        unauthorized = self.client.post("/api/oci/userPage", json={})
        self.assertEqual(unauthorized.status_code, 401)
        self.assertFalse(unauthorized.json()["success"])

        validation = self.client.post(
            "/api/oci/userPage",
            json={"pageSize": 0},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(validation.status_code, 422)
        self.assertEqual(validation.json()["code"], 422)

        system_config = self.client.post(
            "/api/sys/getSysCfg",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        ).json()["data"]
        self.assertNotIn("tgBotToken", system_config)
        self.assertEqual(system_config["adminAccount"], "admin")
        self.assertFalse(system_config["tgBotConfigured"])

        log_path = _runtime_path / "oci-helper.log"
        log_path.write_text("task line 1\ntask line 2\ntask line 3\n", encoding="utf-8")
        unauthorized_logs = self.client.post("/api/sys/taskLogs", json={"lines": 2})
        self.assertEqual(unauthorized_logs.status_code, 401)
        task_logs = self.client.post(
            "/api/sys/taskLogs",
            json={"lines": 2},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(task_logs.status_code, 200)
        self.assertEqual(task_logs.json()["data"]["lines"], ["task line 2", "task line 3"])
        self.assertEqual(task_logs.json()["data"]["lineCount"], 2)
        self.assertTrue(task_logs.json()["data"]["truncated"])

    def test_frontend_and_response_factory(self) -> None:
        frontend = self.client.get("/")
        self.assertEqual(frontend.status_code, 200)
        self.assertIn("/app.js?v=", frontend.text)
        self.assertIn("/app.css?v=", frontend.text)
        self.assertIn("/i18n.js?v=", frontend.text)
        self.assertEqual(frontend.headers["cache-control"], "no-cache")
        stylesheet = self.client.get("/app.css")
        self.assertEqual(stylesheet.status_code, 200)
        script = self.client.get("/app.js")
        self.assertEqual(script.status_code, 200)
        localization = self.client.get("/i18n.js")
        self.assertEqual(localization.status_code, 200)
        self.assertIn('const DEFAULT_LANGUAGE = "zh-CN"', localization.text)
        self.assertIn('"系统设置": "System settings"', localization.text)
        self.assertIn("/sys/updateAdminCredentials", script.text)
        self.assertIn("/oci/imageOptions", script.text)
        self.assertIn("/oci/availabilityDomainOptions", script.text)
        self.assertIn("instanceName", script.text)
        self.assertIn("/sys/taskLogs", script.text)
        self.assertIn("/sshKey/list", script.text)
        self.assertIn("/sshKey/add", script.text)
        self.assertIn('name: "sshKeySelection"', script.text)
        self.assertIn("新公钥会自动保存到公钥列表", script.text)
        self.assertIn('id="app-version"', frontend.text)
        self.assertIn("setAppVersion(data.currentVersion)", script.text)
        self.assertIn("任务列表", frontend.text)
        self.assertIn("执行日志", frontend.text)
        self.assertIn("SSH 公钥", frontend.text)
        self.assertNotIn(
            'options: ["UBUNTU_22_04", "UBUNTU_24_04"',
            script.text,
        )
        self.assertNotIn("planType", script.text)
        self.assertNotIn("套餐", script.text)
        self.assertRegex(
            stylesheet.text,
            r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important;",
        )
        self.assertTrue(ResponseData.success()["success"])
        self.assertFalse(ResponseData.fail()["success"])

    def test_ssh_public_key_management(self) -> None:
        login = self.client.post(
            "/api/sys/login",
            json={"account": "admin", "password": "test-password-12345"},
        )
        headers = {"Authorization": f"Bearer {login.json()['data']['token']}"}

        unauthorized = self.client.get("/api/sshKey/list")
        self.assertEqual(unauthorized.status_code, 401)

        added = self.client.post(
            "/api/sshKey/add",
            json={"name": "Imported key", "publicKey": TEST_SSH_PUBLIC_KEY},
            headers=headers,
        )
        self.assertTrue(added.json()["success"])
        imported = added.json()["data"]
        self.assertEqual(imported["name"], "Imported key")
        self.assertEqual(imported["publicKey"], TEST_SSH_PUBLIC_KEY)
        self.assertTrue(imported["fingerprint"].startswith("SHA256:"))
        self.assertNotIn("privateKey", imported)

        duplicate = self.client.post(
            "/api/sshKey/add",
            json={"name": "Duplicate key", "publicKey": TEST_SSH_PUBLIC_KEY},
            headers=headers,
        )
        self.assertFalse(duplicate.json()["success"])

        renamed = self.client.post(
            "/api/sshKey/rename",
            json={"id": imported["id"], "name": "Renamed key"},
            headers=headers,
        )
        self.assertTrue(renamed.json()["success"])
        self.assertEqual(renamed.json()["data"]["name"], "Renamed key")

        generated = self.client.post(
            "/api/sshKey/generate",
            json={"name": "Generated key"},
            headers=headers,
        )
        self.assertEqual(generated.status_code, 200)
        self.assertEqual(generated.headers["content-type"], "application/zip")
        self.assertIn("oci-helper-ssh-key.zip", generated.headers["content-disposition"])
        with zipfile.ZipFile(io.BytesIO(generated.content)) as archive:
            self.assertEqual(set(archive.namelist()), {"id_ed25519", "id_ed25519.pub"})
            private_bytes = archive.read("id_ed25519")
            downloaded_public_key = archive.read("id_ed25519.pub").decode().strip()
            private_mode = archive.getinfo("id_ed25519").external_attr >> 16
        self.assertEqual(private_mode & 0o777, 0o600)
        private_key = load_ssh_private_key(private_bytes, password=None)
        derived_public_key = private_key.public_key().public_bytes(
            Encoding.OpenSSH,
            PublicFormat.OpenSSH,
        ).decode()
        self.assertEqual(downloaded_public_key.split()[:2], derived_public_key.split()[:2])

        listed = self.client.get("/api/sshKey/list", headers=headers)
        self.assertTrue(listed.json()["success"])
        records = listed.json()["data"]
        self.assertEqual({record["name"] for record in records}, {"Renamed key", "Generated key"})
        generated_record = next(record for record in records if record["name"] == "Generated key")
        self.assertEqual(generated_record["publicKey"], downloaded_public_key)
        self.assertTrue(all("privateKey" not in record for record in records))

    def test_request_aliases_and_validation(self) -> None:
        page = VcnPageParams.model_validate({"ociCfgId": "cfg"})
        self.assertEqual(page.current_page, 1)
        self.assertEqual(page.page_size, 10)

        task = CreateInstanceParams.model_validate(
            {
                "userId": "cfg",
                "ocpus": "1",
                "memory": "6",
                "disk": 50,
                "architecture": "ARM",
                "interval": 60,
                "createNumbers": 1,
                "operationSystem": "UBUNTU_22_04",
                "sshPublicKey": TEST_SSH_PUBLIC_KEY,
            }
        )
        self.assertEqual(task.user_id, "cfg")
        self.assertEqual(task.ocpus, 1)
        self.assertEqual(task.interval_max, 60)
        self.assertEqual(task.boot_volume_vpus_per_gb, 20)
        self.assertIsNone(task.availability_domain)
        self.assertIsNone(task.instance_name)
        self.assertEqual(task.ssh_public_key, TEST_SSH_PUBLIC_KEY)

        dynamic_image = CreateInstanceParams.model_validate(
            {
                **task.model_dump(by_alias=True),
                "operationSystem": "Canonical Ubuntu",
                "operationSystemVersion": "24.04",
            }
        )
        self.assertEqual(dynamic_image.operation_system, "Canonical Ubuntu")
        self.assertEqual(dynamic_image.operation_system_version, "24.04")
        with self.assertRaises(ValueError):
            CreateInstanceParams.model_validate(
                {
                    **task.model_dump(by_alias=True),
                    "operationSystem": "Unlisted Linux",
                }
            )

        image_options = GetImageOptionsParams.model_validate(
            {"ociCfgId": "cfg", "architecture": "ARM"}
        )
        self.assertEqual(image_options.architecture, "ARM")
        self.assertEqual(
            ImageOptionRsp.model_validate(
                {
                    "operating_system": "Canonical Ubuntu",
                    "operating_system_version": "24.04",
                    "label": "Canonical Ubuntu 24.04",
                }
            ).model_dump(by_alias=True),
            {
                "operatingSystem": "Canonical Ubuntu",
                "operatingSystemVersion": "24.04",
                "label": "Canonical Ubuntu 24.04",
            },
        )
        with self.assertRaises(ValueError):
            GetImageOptionsParams.model_validate(
                {"ociCfgId": "cfg", "architecture": "unknown-shape"}
            )
        self.assertEqual(
            GetShapeOptionsParams.model_validate(
                {"ociCfgId": "cfg", "architecture": "AMD"}
            ).architecture,
            "AMD",
        )
        self.assertEqual(
            AvailabilityDomainOptionRsp(
                name="example:AD-1",
                label="example:AD-1",
            ).model_dump(by_alias=True),
            {"name": "example:AD-1", "label": "example:AD-1"},
        )

        retry_policy = CreateInstanceParams.model_validate(
            {
                "userId": "cfg",
                "ocpus": "1",
                "memory": "6",
                "disk": 50,
                "architecture": "ARM",
                "interval": 5,
                "intervalMax": 30,
                "maxAttempts": 0,
                "availabilityDomain": "  example:AD-1  ",
                "instanceName": "  web-server  ",
                "createNumbers": 1,
                "operationSystem": "UBUNTU_22_04",
                "sshPublicKey": f"  {TEST_SSH_PUBLIC_KEY}  ",
            }
        )
        self.assertEqual(retry_policy.interval_max, 30)
        self.assertEqual(retry_policy.max_attempts, 0)
        self.assertEqual(retry_policy.availability_domain, "example:AD-1")
        self.assertEqual(retry_policy.instance_name, "web-server")
        self.assertEqual(retry_policy.ssh_public_key, TEST_SSH_PUBLIC_KEY)

        with self.assertRaises(ValueError):
            CreateInstanceParams.model_validate(
                {**retry_policy.model_dump(by_alias=True), "instanceName": "web\nserver"}
            )
        with self.assertRaises(ValueError):
            CreateInstanceParams.model_validate(
                {**retry_policy.model_dump(by_alias=True), "instanceName": "x" * 252}
            )

        missing_key = retry_policy.model_dump(by_alias=True)
        missing_key.pop("sshPublicKey")
        with self.assertRaises(ValueError):
            CreateInstanceParams.model_validate(missing_key)
        with self.assertRaises(ValueError):
            CreateInstanceParams.model_validate(
                {**retry_policy.model_dump(by_alias=True), "sshPublicKey": "not-a-key"}
            )
        with self.assertRaises(ValueError):
            CreateInstanceParams.model_validate(
                {
                    **retry_policy.model_dump(by_alias=True),
                    "sshPublicKey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5",
                }
            )
        with self.assertRaises(ValueError):
            CreateInstanceParams.model_validate(
                {**retry_policy.model_dump(by_alias=True), "rootPassword": "legacy-password"}
            )
        with self.assertRaises(ValueError):
            CreateInstanceParams.model_validate(
                {**retry_policy.model_dump(by_alias=True), "ocpus": "1.5"}
            )
        with self.assertRaises(ValueError):
            CreateInstanceParams.model_validate(
                {**retry_policy.model_dump(by_alias=True), "ocpus": 5}
            )
        with self.assertRaises(ValueError):
            CreateInstanceParams.model_validate(
                {**retry_policy.model_dump(by_alias=True), "memory": "25"}
            )
        with self.assertRaises(ValueError):
            CreateInstanceParams.model_validate(
                {
                    **retry_policy.model_dump(by_alias=True),
                    "ocpus": 2,
                    "memory": "1",
                }
            )
        with self.assertRaises(ValueError):
            CreateInstanceParams.model_validate(
                {**retry_policy.model_dump(by_alias=True), "disk": 201}
            )
        for invalid_vpu in (11, 29, 121):
            with self.assertRaises(ValueError):
                CreateInstanceParams.model_validate(
                    {
                        **retry_policy.model_dump(by_alias=True),
                        "bootVolumeVpusPerGB": invalid_vpu,
                    }
                )

        console = StartConsoleConnectionParams.model_validate(
            {
                "ociCfgId": "cfg",
                "instanceId": "instance",
                "publicKey": TEST_CONSOLE_PUBLIC_KEY,
            }
        )
        self.assertEqual(console.public_key, TEST_CONSOLE_PUBLIC_KEY)
        with self.assertRaises(ValueError):
            StartConsoleConnectionParams.model_validate(
                {
                    "ociCfgId": "cfg",
                    "instanceId": "instance",
                    "publicKey": TEST_SSH_PUBLIC_KEY,
                }
            )

        self.assertEqual(
            UpdateBootVolumeParams.model_validate(
                {
                    "ociCfgId": "cfg",
                    "bootVolumeId": "volume",
                    "bootVolumeSize": 50,
                    "bootVolumeVpu": 30,
                }
            ).boot_volume_vpu,
            30,
        )
        for invalid_vpu in (0, 9, 11, 21, 29):
            with self.assertRaises(ValueError):
                UpdateBootVolumeParams.model_validate(
                    {
                        "ociCfgId": "cfg",
                        "bootVolumeId": "volume",
                        "bootVolumeSize": 50,
                        "bootVolumeVpu": invalid_vpu,
                    }
                )

        traffic = GetTrafficDataParams.model_validate(
            {"ociCfgId": "cfg", "instanceId": "instance"}
        )
        self.assertEqual(traffic.namespace, "oci_computeagent")
        with self.assertRaises(ValueError):
            GetTrafficDataParams.model_validate(
                {
                    "ociCfgId": "cfg",
                    "instanceId": "instance",
                    "beginTime": datetime.now(UTC),
                }
            )
        with self.assertRaises(ValueError):
            GetTrafficDataParams.model_validate(
                {
                    "ociCfgId": "cfg",
                    "instanceId": "instance",
                    "namespace": "oci_vcn",
                }
            )

        icmpv6 = IngressRuleInput.model_validate(
            {
                "protocol": "58",
                "sourceType": "CIDR_BLOCK",
                "source": "::/0",
                "icmpOptions": {"type": 2, "code": 0},
            }
        )
        options = _protocol_options(
            oci,
            icmpv6.protocol,
            icmpv6.source_port,
            icmpv6.destination_port,
            IcmpOptions(type=2, code=0),
        )
        self.assertEqual(options["icmp_options"].type, 2)

    def test_cache_eviction(self) -> None:
        cache = ExpireCache(maxsize=2, ttl=60)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("c", 3)
        self.assertIsNone(cache.get("a"))
        self.assertEqual(cache.get("c"), 3)

    def test_create_retry_policy_helpers(self) -> None:
        capacity_error = oci.exceptions.ServiceError(
            500, "OutOfHostCapacity", {}, "No host capacity"
        )
        throttle_error = oci.exceptions.ServiceError(
            429, "TooManyRequests", {}, "Slow down"
        )
        conflict_error = oci.exceptions.ServiceError(409, "Conflict", {}, "Conflict")
        self.assertTrue(is_capacity_oci_error(capacity_error))
        self.assertTrue(is_retryable_oci_error(capacity_error))
        self.assertTrue(is_retryable_oci_error(throttle_error))
        self.assertFalse(is_retryable_oci_error(conflict_error))

        fetcher = OracleInstanceFetcher.__new__(OracleInstanceFetcher)
        fetcher.oci_cfg = SimpleNamespace(region="test-region")
        fetcher.compartment_id = "compartment"
        fetcher.compute_client = SimpleNamespace(list_shapes=object())
        fetcher.get_availability_domains = lambda: [
            SimpleNamespace(name="AD-2"),
            SimpleNamespace(name="AD-1"),
        ]
        response = SimpleNamespace(data=[SimpleNamespace(shape="VM.Standard.A1.Flex")])
        with patch(
            "core.oracle_fetcher.oci.pagination.list_call_get_all_results",
            return_value=response,
        ):
            self.assertEqual(
                fetcher._select_availability_domain(
                    "VM.Standard.A1.Flex", start_index=1
                ),
                "AD-2",
            )
            self.assertEqual(
                fetcher._select_availability_domain(
                    "VM.Standard.A1.Flex", "AD-1", 1
                ),
                "AD-1",
            )

        def shape_results(*_args, **kwargs):
            data = (
                [SimpleNamespace(shape="VM.Standard.A1.Flex")]
                if kwargs["availability_domain"] == "AD-1"
                else []
            )
            return SimpleNamespace(data=data)

        with patch(
            "core.oracle_fetcher.oci.pagination.list_call_get_all_results",
            side_effect=shape_results,
        ):
            supported = fetcher.list_supported_availability_domains(
                "VM.Standard.A1.Flex"
            )
            self.assertEqual([domain.name for domain in supported], ["AD-1"])
            fetcher.validate_availability_domain_selection(
                "VM.Standard.A1.Flex",
                "AD-1",
            )
            with self.assertRaises(ValueError):
                fetcher.validate_availability_domain_selection(
                    "VM.Standard.A1.Flex",
                    "AD-2",
                )

        with patch("core.task_scheduler.time.monotonic", return_value=100.0):
            self.assertEqual(TaskInfo("task", 5, initial_delay=12.5).next_run, 112.5)
        with patch("services.instance_service.random.randint", return_value=17) as randint:
            self.assertEqual(_random_retry_delay(5, 30), 17)
            randint.assert_called_once_with(5, 30)
        self.assertIsNone(_build_instance_display_name(None, 3, 3))
        self.assertEqual(_build_instance_display_name("web", 1, 1), "web")
        self.assertEqual(_build_instance_display_name("web", 3, 3), "web-001")
        self.assertEqual(_build_instance_display_name("web", 3, 2), "web-002")
        self.assertEqual(_build_instance_display_name("web", 3, 1), "web-003")

    def test_launch_uses_only_ssh_key_metadata(self) -> None:
        fetcher = OracleInstanceFetcher.__new__(OracleInstanceFetcher)
        fetcher.oci_cfg = SimpleNamespace(region="test-region")
        fetcher.compartment_id = "compartment"
        fetcher.username = "test-user"
        fetcher._architecture = "ARM"
        fetcher._shape = "VM.Standard.A1.Flex"
        fetcher._ocpus = 1
        fetcher._memory = 6
        fetcher._disk = 50
        fetcher._operation_system = "UBUNTU_22_04"
        fetcher._ssh_key = TEST_SSH_PUBLIC_KEY
        fetcher._retry_token_seed = "task-1:1"
        fetcher._instance_name = "web-server-001"
        fetcher._select_availability_domain = MagicMock(return_value="AD-1")
        fetcher._select_image = MagicMock(return_value=SimpleNamespace(id="image-1"))
        fetcher._ensure_network = MagicMock(return_value=(None, SimpleNamespace(id="subnet-1")))
        fetcher.get_vnic_by_instance_id = MagicMock(
            return_value=SimpleNamespace(
                private_ip="10.0.0.10",
                public_ip="203.0.113.10",
            )
        )
        launch_instance = MagicMock(
            return_value=SimpleNamespace(data=SimpleNamespace(id="instance-1"))
        )
        fetcher.compute_client = SimpleNamespace(
            list_instances=MagicMock(),
            launch_instance=launch_instance,
            get_instance=MagicMock(return_value=SimpleNamespace()),
        )

        with (
            patch(
                "core.oracle_fetcher.oci.pagination.list_call_get_all_results",
                return_value=SimpleNamespace(data=[]),
            ),
            patch(
                "core.oracle_fetcher.oci.wait_until",
                return_value=SimpleNamespace(data=SimpleNamespace(id="instance-1")),
            ),
        ):
            result = fetcher.create_instance_data()

        self.assertTrue(result["success"])
        launch_id, display_name = fetcher._build_launch_identity(
            "task-1:1", "AD-1", "web-server-001"
        )
        self.assertEqual(launch_instance.call_args.kwargs["opc_retry_token"], launch_id)
        launch_details = launch_instance.call_args.kwargs["launch_instance_details"]
        self.assertEqual(launch_details.display_name, display_name)
        self.assertEqual(
            launch_details.freeform_tags,
            {
                MANAGED_TAG_KEY: MANAGED_TAG_VALUE,
                LAUNCH_ID_TAG_KEY: launch_id,
            },
        )
        self.assertEqual(
            launch_details.metadata,
            {"ssh_authorized_keys": TEST_SSH_PUBLIC_KEY},
        )
        self.assertEqual(launch_details.source_details.boot_volume_vpus_per_gb, 20)
        self.assertTrue(launch_details.create_vnic_details.assign_public_ip)
        self.assertIsNone(launch_details.create_vnic_details.private_ip)
        self.assertEqual(result["private_ip"], "10.0.0.10")
        self.assertEqual(result["public_ip"], "203.0.113.10")
        self.assertNotIn("user_data", launch_details.metadata)

    def test_launch_reconciles_existing_managed_instance(self) -> None:
        fetcher = OracleInstanceFetcher.__new__(OracleInstanceFetcher)
        fetcher.oci_cfg = SimpleNamespace(region="test-region")
        fetcher.compartment_id = "compartment"
        fetcher.username = "test-user"
        fetcher._architecture = "ARM"
        fetcher._shape = "VM.Standard.A1.Flex"
        fetcher._ocpus = 1
        fetcher._memory = 6
        fetcher._disk = 50
        fetcher._operation_system = "UBUNTU_22_04"
        fetcher._ssh_key = TEST_SSH_PUBLIC_KEY
        fetcher._retry_token_seed = "task-1:1"
        fetcher._select_availability_domain = MagicMock(return_value="AD-1")
        fetcher._select_image = MagicMock()
        fetcher._ensure_network = MagicMock()
        fetcher.get_vnic_by_instance_id = MagicMock(
            return_value=SimpleNamespace(
                private_ip="10.0.0.11",
                public_ip="203.0.113.11",
            )
        )
        launch_id, display_name = fetcher._build_launch_identity("task-1:1", "AD-1")
        existing = SimpleNamespace(
            id="instance-existing",
            image_id="image-existing",
            lifecycle_state="RUNNING",
            display_name=display_name,
            freeform_tags={
                MANAGED_TAG_KEY: MANAGED_TAG_VALUE,
                LAUNCH_ID_TAG_KEY: launch_id,
            },
        )
        launch_instance = MagicMock()
        fetcher.compute_client = SimpleNamespace(
            list_instances=MagicMock(),
            launch_instance=launch_instance,
        )

        with patch(
            "core.oracle_fetcher.oci.pagination.list_call_get_all_results",
            return_value=SimpleNamespace(data=[existing]),
        ):
            result = fetcher.create_instance_data()

        self.assertTrue(result["success"])
        self.assertEqual(result["instance_id"], "instance-existing")
        self.assertEqual(result["image_id"], "image-existing")
        self.assertEqual(result["lifecycle_state"], "RUNNING")
        launch_instance.assert_not_called()
        fetcher._select_image.assert_not_called()
        fetcher._ensure_network.assert_not_called()

        existing.lifecycle_state = "STOPPED"
        with patch(
            "core.oracle_fetcher.oci.pagination.list_call_get_all_results",
            return_value=SimpleNamespace(data=[existing]),
        ):
            stopped_result = fetcher.create_instance_data()
        self.assertTrue(stopped_result["success"])
        self.assertEqual(stopped_result["lifecycle_state"], "STOPPED")

    def test_create_success_message_reports_actual_state_and_task_metrics(self) -> None:
        snapshot = {
            "username": "example-user",
            "attempt": 7,
            "create_time": datetime.now() - timedelta(minutes=5),
        }
        detail = {
            "lifecycle_state": "RUNNING",
            "region": "ap-tokyo-1",
            "availability_domain": "AD-1",
            "architecture": "ARM",
            "ocpus": 1,
            "memory": 6,
            "disk": 50,
            "shape": "VM.Standard.A1.Flex",
            "private_ip": "10.0.0.10",
            "public_ip": "203.0.113.10",
        }
        running_message = _build_create_success_message(snapshot, detail)
        self.assertIn("开机成功", running_message)
        self.assertIn("状态：RUNNING", running_message)
        self.assertIn("任务尝试次数：7", running_message)
        self.assertIn("任务累计耗时：", running_message)
        self.assertIn("专用 IPv4：10.0.0.10", running_message)
        self.assertIn("公网 IPv4：203.0.113.10", running_message)
        self.assertNotIn("开机时长", running_message)

        detail["lifecycle_state"] = "STOPPED"
        stopped_message = _build_create_success_message(snapshot, detail)
        self.assertIn("实例创建成功", stopped_message)
        self.assertIn("状态：STOPPED", stopped_message)
        self.assertNotIn("用户：[example-user] 开机成功", stopped_message)

    def test_image_and_network_selection_use_available_resources(self) -> None:
        fetcher = OracleInstanceFetcher.__new__(OracleInstanceFetcher)
        fetcher.compartment_id = "compartment"
        fetcher.compute_client = SimpleNamespace(list_images=MagicMock())
        image = SimpleNamespace(id="image-available")
        with patch(
            "core.oracle_fetcher.oci.pagination.list_call_get_all_results",
            return_value=SimpleNamespace(data=[image]),
        ) as list_all:
            self.assertIs(fetcher._select_image("VM.Standard.A1.Flex", "UBUNTU_22_04"), image)
        self.assertEqual(list_all.call_args.kwargs["lifecycle_state"], "AVAILABLE")

        with patch(
            "core.oracle_fetcher.oci.pagination.list_call_get_all_results",
            return_value=SimpleNamespace(data=[image]),
        ) as list_all:
            self.assertIs(
                fetcher._select_image(
                    "VM.Standard.A1.Flex",
                    "Canonical Ubuntu",
                    "24.04",
                ),
                image,
            )
        self.assertEqual(list_all.call_args.kwargs["operating_system"], "Canonical Ubuntu")
        self.assertEqual(list_all.call_args.kwargs["operating_system_version"], "24.04")

        available_images = [
            SimpleNamespace(
                operating_system="Canonical Ubuntu",
                operating_system_version="24.04",
            ),
            SimpleNamespace(
                operating_system="canonical ubuntu",
                operating_system_version="24.04",
            ),
            SimpleNamespace(
                operating_system="Oracle Linux",
                operating_system_version="9",
            ),
            SimpleNamespace(
                operating_system="Windows",
                operating_system_version="Server 2025",
            ),
            SimpleNamespace(operating_system=None, operating_system_version=None),
        ]
        with patch(
            "core.oracle_fetcher.oci.pagination.list_call_get_all_results",
            return_value=SimpleNamespace(data=available_images),
        ) as list_all:
            options = fetcher.list_image_options("VM.Standard.A1.Flex")
        self.assertEqual(
            options,
            [
                {
                    "operating_system": "Canonical Ubuntu",
                    "operating_system_version": "24.04",
                    "label": "Canonical Ubuntu 24.04",
                },
                {
                    "operating_system": "Oracle Linux",
                    "operating_system_version": "9",
                    "label": "Oracle Linux 9",
                },
            ],
        )
        self.assertEqual(list_all.call_args.kwargs["shape"], "VM.Standard.A1.Flex")

        existing_vcn = SimpleNamespace(
            id="vcn-existing",
            display_name="existing-vcn",
            freeform_tags={},
            cidr_block="10.1.0.0/16",
        )
        managed_vcn = SimpleNamespace(
            id="vcn-managed",
            display_name="oci-helper-vcn",
            freeform_tags={MANAGED_TAG_KEY: MANAGED_TAG_VALUE},
            cidr_block="10.2.0.0/16",
        )
        existing_private_subnet = SimpleNamespace(
            id="subnet-existing-private",
            display_name="existing-private-subnet",
            freeform_tags={},
            prohibit_internet_ingress=False,
            prohibit_public_ip_on_vnic=True,
            availability_domain=None,
        )
        ingress_blocked_subnet = SimpleNamespace(
            id="subnet-ingress-blocked",
            display_name="ingress-blocked-subnet",
            freeform_tags={},
            prohibit_internet_ingress=True,
            prohibit_public_ip_on_vnic=False,
            availability_domain=None,
        )
        existing_public_subnet = SimpleNamespace(
            id="subnet-existing-public",
            display_name="existing-public-subnet",
            freeform_tags={},
            prohibit_internet_ingress=False,
            prohibit_public_ip_on_vnic=False,
            availability_domain=None,
        )
        managed_subnet = SimpleNamespace(
            id="subnet-managed",
            display_name="oci-helper-subnet",
            freeform_tags={MANAGED_TAG_KEY: MANAGED_TAG_VALUE},
            prohibit_internet_ingress=False,
            prohibit_public_ip_on_vnic=False,
            availability_domain=None,
        )
        fetcher.list_vcns = MagicMock(return_value=[existing_vcn, managed_vcn])
        fetcher.list_subnets = MagicMock(
            side_effect=lambda vcn_id: {
                "vcn-existing": [
                    existing_private_subnet,
                    ingress_blocked_subnet,
                    existing_public_subnet,
                ],
                "vcn-managed": [managed_subnet],
            }[vcn_id]
        )
        fetcher._ensure_internet_gateway = MagicMock()
        fetcher.vn_client = SimpleNamespace(
            create_vcn=MagicMock(),
            create_subnet=MagicMock(),
        )

        selected_vcn, selected_subnet = fetcher._ensure_network("AD-1")

        self.assertIs(selected_vcn, existing_vcn)
        self.assertIs(selected_subnet, existing_public_subnet)
        fetcher.vn_client.create_vcn.assert_not_called()
        fetcher.vn_client.create_subnet.assert_not_called()
        fetcher._ensure_internet_gateway.assert_not_called()

    def test_network_creates_public_subnet_in_existing_vcn_when_needed(self) -> None:
        fetcher = OracleInstanceFetcher.__new__(OracleInstanceFetcher)
        fetcher.compartment_id = "compartment"
        existing_vcn = SimpleNamespace(
            id="vcn-existing",
            display_name="existing-vcn",
            cidr_block="10.3.0.0/16",
        )
        created_subnet = SimpleNamespace(id="subnet-created")
        available_subnet = SimpleNamespace(id="subnet-created", lifecycle_state="AVAILABLE")
        fetcher.list_vcns = MagicMock(return_value=[existing_vcn])
        fetcher.list_subnets = MagicMock(return_value=[])
        fetcher._ensure_internet_gateway = MagicMock()
        fetcher.vn_client = SimpleNamespace(
            create_vcn=MagicMock(),
            create_subnet=MagicMock(return_value=SimpleNamespace(data=created_subnet)),
            get_subnet=MagicMock(return_value=SimpleNamespace(data=available_subnet)),
        )

        with patch(
            "core.oracle_fetcher.oci.wait_until",
            return_value=SimpleNamespace(data=available_subnet),
        ):
            selected_vcn, selected_subnet = fetcher._ensure_network("AD-1")

        self.assertIs(selected_vcn, existing_vcn)
        self.assertIs(selected_subnet, available_subnet)
        details = fetcher.vn_client.create_subnet.call_args.kwargs["create_subnet_details"]
        self.assertEqual(details.vcn_id, "vcn-existing")
        self.assertEqual(details.cidr_block, "10.3.0.0/24")
        self.assertFalse(details.prohibit_internet_ingress)
        self.assertFalse(details.prohibit_public_ip_on_vnic)
        fetcher.vn_client.create_vcn.assert_not_called()
        fetcher._ensure_internet_gateway.assert_called_once_with(existing_vcn)

    def test_internet_gateway_reuses_existing_resource_and_preserves_route(self) -> None:
        fetcher = OracleInstanceFetcher.__new__(OracleInstanceFetcher)
        fetcher.compartment_id = "compartment"
        vcn = SimpleNamespace(id="vcn-existing", default_route_table_id="route-default")
        gateway = SimpleNamespace(
            id="gateway-existing",
            display_name="existing-gateway",
            freeform_tags={},
            is_enabled=True,
            lifecycle_state="AVAILABLE",
        )
        route_table = SimpleNamespace(id="route-default", route_rules=[])
        fetcher.vn_client = SimpleNamespace(
            list_internet_gateways=MagicMock(),
            get_route_table=MagicMock(return_value=SimpleNamespace(data=route_table)),
            create_internet_gateway=MagicMock(),
            update_internet_gateway=MagicMock(),
            update_route_table=MagicMock(),
        )

        with patch(
            "core.oracle_fetcher.oci.pagination.list_call_get_all_results",
            return_value=SimpleNamespace(data=[gateway]),
        ):
            fetcher._ensure_internet_gateway(vcn)

        fetcher.vn_client.create_internet_gateway.assert_not_called()
        fetcher.vn_client.update_internet_gateway.assert_not_called()
        route_details = fetcher.vn_client.update_route_table.call_args.kwargs[
            "update_route_table_details"
        ]
        self.assertEqual(route_details.route_rules[0].network_entity_id, "gateway-existing")

        route_table.route_rules = [
            SimpleNamespace(
                destination="0.0.0.0/0",
                network_entity_id="nat-existing",
            )
        ]
        fetcher.vn_client.update_route_table.reset_mock()
        with (
            patch(
                "core.oracle_fetcher.oci.pagination.list_call_get_all_results",
                return_value=SimpleNamespace(data=[gateway]),
            ),
            self.assertRaisesRegex(ValueError, "默认路由"),
        ):
            fetcher._ensure_internet_gateway(vcn)
        fetcher.vn_client.update_route_table.assert_not_called()

    def test_console_primary_vnic_and_network_delete_contracts(self) -> None:
        fetcher = OracleInstanceFetcher.__new__(OracleInstanceFetcher)
        console_call = MagicMock(
            return_value=SimpleNamespace(
                data=SimpleNamespace(vnc_connection_string="ssh console")
            )
        )
        fetcher.compute_client = SimpleNamespace(
            create_instance_console_connection=console_call
        )

        self.assertEqual(
            fetcher.create_console_connection("instance-1", TEST_CONSOLE_PUBLIC_KEY),
            "ssh console",
        )
        details = console_call.call_args.kwargs["create_instance_console_connection_details"]
        self.assertEqual(details.instance_id, "instance-1")
        self.assertEqual(details.public_key, TEST_CONSOLE_PUBLIC_KEY)
        self.assertTrue(console_call.call_args.kwargs["opc_retry_token"])

        fetcher.list_vnic_attachments = MagicMock(
            return_value=[
                SimpleNamespace(vnic_id="secondary", lifecycle_state="ATTACHED"),
                SimpleNamespace(vnic_id="primary", lifecycle_state="ATTACHED"),
            ]
        )
        fetcher.get_vnic = MagicMock(
            side_effect=[
                SimpleNamespace(id="secondary", is_primary=False),
                SimpleNamespace(id="primary", is_primary=True),
            ]
        )
        self.assertEqual(fetcher.get_vnic_by_instance_id("instance-1").id, "primary")

        fetcher.compartment_id = "compartment"
        vcn = SimpleNamespace(
            id="vcn-1",
            default_route_table_id="route-1",
            default_security_list_id="security-1",
            default_dhcp_options_id="dhcp-1",
            freeform_tags={MANAGED_TAG_KEY: MANAGED_TAG_VALUE},
        )
        gateway = SimpleNamespace(
            id="gateway-1",
            display_name="oci-helper-ig",
            freeform_tags={MANAGED_TAG_KEY: MANAGED_TAG_VALUE},
            lifecycle_state="AVAILABLE",
        )
        route = SimpleNamespace(
            id="route-1",
            route_rules=[
                SimpleNamespace(
                    destination="0.0.0.0/0",
                    network_entity_id="gateway-1",
                )
            ],
            lifecycle_state="AVAILABLE",
        )
        network_client = SimpleNamespace(
            list_subnets=MagicMock(),
            list_internet_gateways=MagicMock(),
            list_nat_gateways=MagicMock(),
            list_service_gateways=MagicMock(),
            list_local_peering_gateways=MagicMock(),
            list_drg_attachments=MagicMock(),
            list_network_security_groups=MagicMock(),
            list_vlans=MagicMock(),
            list_route_tables=MagicMock(),
            list_security_lists=MagicMock(),
            list_dhcp_options=MagicMock(),
            get_route_table=MagicMock(
                return_value=SimpleNamespace(data=route, headers={"etag": "route-etag"})
            ),
            update_route_table=MagicMock(
                return_value=SimpleNamespace(data=SimpleNamespace(lifecycle_state="AVAILABLE"))
            ),
            get_internet_gateway=MagicMock(
                return_value=SimpleNamespace(data=gateway, headers={"etag": "gateway-etag"})
            ),
            delete_internet_gateway=MagicMock(),
            get_vcn=MagicMock(
                return_value=SimpleNamespace(data=vcn, headers={"etag": "vcn-etag"})
            ),
            delete_vcn=MagicMock(),
        )
        fetcher.vn_client = network_client
        fetcher.get_vcn_by_id = MagicMock(return_value=vcn)
        fetcher._wait_until_deleted = MagicMock()

        def list_resources(method, _vcn_id):
            if method is network_client.list_internet_gateways:
                return [gateway]
            if method is network_client.list_route_tables:
                return [route]
            return []

        fetcher._list_vcn_resources = MagicMock(side_effect=list_resources)
        with patch(
            "core.oracle_fetcher.oci.wait_until",
            return_value=SimpleNamespace(data=route),
        ):
            fetcher.delete_vcn_by_id("vcn-1")

        network_client.delete_internet_gateway.assert_called_once_with(
            ig_id="gateway-1",
            if_match="gateway-etag",
        )
        network_client.delete_vcn.assert_called_once_with(
            vcn_id="vcn-1",
            if_match="vcn-etag",
        )
        self.assertEqual(
            network_client.update_route_table.call_args.kwargs[
                "update_route_table_details"
            ].route_rules,
            [],
        )

    def test_ipv6_boot_volume_shape_traffic_and_ip_contracts(self) -> None:
        fetcher = OracleInstanceFetcher.__new__(OracleInstanceFetcher)
        fetcher.vn_client = SimpleNamespace(list_subnets=MagicMock())
        fetcher._list_vcn_resources = MagicMock(
            return_value=[SimpleNamespace(ipv6_cidr_blocks=["2001:db8:1234::/64"])]
        )
        self.assertEqual(
            fetcher._find_available_ipv6_subnet_cidr(
                SimpleNamespace(id="vcn-1", ipv6_cidr_blocks=["2001:db8:1234::/56"])
            ),
            "2001:db8:1234:1::/64",
        )

        existing_ipv6 = SimpleNamespace(
            ip_address="2001:db8:1234:1::10",
            lifecycle_state="AVAILABLE",
        )
        existing_subnet = SimpleNamespace(id="subnet-1")
        fetcher.vn_client = SimpleNamespace(
            list_ipv6s=MagicMock(),
            get_subnet=MagicMock(return_value=SimpleNamespace(data=existing_subnet)),
        )
        fetcher._ensure_ipv6_internet_route = MagicMock()
        with patch(
            "core.oracle_fetcher.oci.pagination.list_call_get_all_results",
            return_value=SimpleNamespace(data=[existing_ipv6]),
        ):
            self.assertIs(
                fetcher.create_ipv6(
                    SimpleNamespace(id="vnic-1", subnet_id="subnet-1"),
                    SimpleNamespace(id="vcn-1"),
                ),
                existing_ipv6,
            )
        fetcher._ensure_ipv6_internet_route.assert_called_once_with(
            unittest.mock.ANY,
            existing_subnet,
        )

        shape = SimpleNamespace(
            shape="VM.Standard.A1.Flex",
            is_flexible=True,
            ocpu_options=SimpleNamespace(min=1, max=4),
            memory_options=SimpleNamespace(
                min_in_g_bs=1,
                max_in_g_bs=24,
                min_per_ocpu_in_gbs=1,
                max_per_ocpu_in_gbs=6,
            ),
        )
        fetcher._validate_flex_shape_config(shape, 2, 12)
        with self.assertRaises(ValueError):
            fetcher._validate_flex_shape_config(shape, 0.5, 6)
        with self.assertRaises(ValueError):
            fetcher._validate_flex_shape_config(shape, 2, 13)

        current_volume = SimpleNamespace(size_in_gbs=50, vpus_per_gb=10)
        fetcher.get_boot_volume = MagicMock(return_value=current_volume)
        update_boot_volume = MagicMock(
            return_value=SimpleNamespace(data=SimpleNamespace(size_in_gbs=50, vpus_per_gb=20))
        )
        fetcher.block_storage_client = SimpleNamespace(update_boot_volume=update_boot_volume)
        fetcher.update_boot_volume_cfg("volume-1", 50, 20)
        update_details = update_boot_volume.call_args.kwargs["update_boot_volume_details"]
        self.assertIsNone(update_details.size_in_gbs)
        self.assertEqual(update_details.vpus_per_gb, 20)
        update_boot_volume.reset_mock()
        self.assertIs(fetcher.update_boot_volume_cfg("volume-1", 50, 10), current_volume)
        update_boot_volume.assert_not_called()

        fetcher.compartment_id = "compartment"
        fetcher._config = {}
        timestamp = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
        monitoring_client = MagicMock()
        monitoring_client.base_client.session.close = MagicMock()

        def summarize(*, summarize_metrics_data_details, **_kwargs):
            value = 2 * 1024 * 1024 if "BytesIn" in summarize_metrics_data_details.query else 1024 * 1024
            return SimpleNamespace(
                data=[
                    SimpleNamespace(
                        aggregated_datapoints=[
                            SimpleNamespace(timestamp=timestamp, value=value)
                        ]
                    )
                ]
            )

        monitoring_client.summarize_metrics_data.side_effect = summarize
        with patch(
            "core.oracle_fetcher.oci.monitoring.MonitoringClient",
            return_value=monitoring_client,
        ):
            traffic = fetcher.get_traffic_data(
                "instance-1",
                begin_time=timestamp - timedelta(hours=1),
                end_time=timestamp,
            )
        self.assertEqual(traffic["ingress"], [2.0])
        self.assertEqual(traffic["egress"], [1.0])
        queries = [
            call.kwargs["summarize_metrics_data_details"].query
            for call in monitoring_client.summarize_metrics_data.call_args_list
        ]
        self.assertEqual(
            queries,
            [
                'NetworksBytesIn[5m]{resourceId = "instance-1"}.increment()',
                'NetworksBytesOut[5m]{resourceId = "instance-1"}.increment()',
            ],
        )

        fetcher.get_private_ip_id = MagicMock(return_value="private-ip-1")
        fetcher.delete_public_ip_by_private_ip = MagicMock()
        fetcher.create_public_ip = MagicMock(
            side_effect=[TimeoutError("response timeout"), "203.0.113.20"]
        )
        fetcher._get_public_ip_address = MagicMock(
            side_effect=[TimeoutError("reconciliation timeout"), None]
        )
        with patch("core.oracle_fetcher.time.sleep"):
            self.assertEqual(
                fetcher.reassign_ephemeral_public_ip(SimpleNamespace(id="vnic-1")),
                "203.0.113.20",
            )
        retry_tokens = [call.args[1] for call in fetcher.create_public_ip.call_args_list]
        self.assertEqual(len(set(retry_tokens)), 1)

    def test_tenant_info_excludes_plan(self) -> None:
        fetcher = OracleInstanceFetcher.__new__(OracleInstanceFetcher)
        fetcher.oci_cfg = SimpleNamespace(tenant_id="tenancy-1")
        fetcher.identity_client = SimpleNamespace(
            get_tenancy=MagicMock(
                return_value=SimpleNamespace(
                    data=SimpleNamespace(name="Example Tenant", home_region_key="NRT")
                )
            )
        )
        fetcher.list_region_subscriptions = MagicMock(
            return_value=[SimpleNamespace(region_name="ap-tokyo-1")]
        )

        self.assertEqual(
            fetcher.get_tenant_info(),
            {
                "tenantName": "Example Tenant",
                "homeRegion": "NRT",
                "subscribedRegions": ["ap-tokyo-1"],
            },
        )

    def test_current_schema_and_migrations(self) -> None:
        current_db = _runtime_path / "current-schema.db"
        with sqlite3.connect(current_db) as connection:
            schema_path = Path(__file__).resolve().parent.parent / "schema.sql"
            connection.executescript(schema_path.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO oci_kv(id, code, value, type) VALUES (?, ?, ?, ?)",
                ("kv-1", "SYS_TG_BOT_TOKEN", "legacy-bot-token", "SYS_INIT_CFG"),
            )
            _apply_migrations(connection)

            task_columns = {
                row[1]: row
                for row in connection.execute("PRAGMA table_info('oci_create_task')")
            }
            user_columns = {
                row[1]: row for row in connection.execute("PRAGMA table_info('oci_user')")
            }
            notification_columns = {
                row[1]: row
                for row in connection.execute("PRAGMA table_info('notification_outbox')")
            }
            ssh_key_columns = {
                row[1]: row
                for row in connection.execute("PRAGMA table_info('ssh_public_key')")
            }
            bot_secret = connection.execute(
                "SELECT value FROM oci_kv WHERE id = 'kv-1'"
            ).fetchone()[0]
            versions = {
                row[0] for row in connection.execute("SELECT version FROM schema_migrations")
            }

        self.assertNotIn("root_password", task_columns)
        self.assertNotIn("plan_type", user_columns)
        self.assertIn("ssh_public_key", task_columns)
        self.assertIn("operation_system_version", task_columns)
        self.assertIn("instance_name", task_columns)
        self.assertIn("initial_create_numbers", task_columns)
        self.assertIn("boot_volume_vpus_per_gb", task_columns)
        self.assertEqual(task_columns["boot_volume_vpus_per_gb"][4], "20")
        self.assertEqual(task_columns["ssh_public_key"][3], 1)
        self.assertEqual(notification_columns["event_key"][3], 1)
        self.assertEqual(notification_columns["status"][4], "'pending'")
        self.assertEqual(ssh_key_columns["name"][3], 1)
        self.assertEqual(ssh_key_columns["public_key"][3], 1)
        self.assertEqual(ssh_key_columns["fingerprint"][3], 1)
        self.assertNotIn("private_key", ssh_key_columns)
        self.assertTrue(is_encrypted(bot_secret))
        self.assertEqual(decrypt_secret(bot_secret), "legacy-bot-token")
        self.assertEqual(versions, set(range(1, 13)))

    def test_migration_preserves_legacy_task_boot_volume_performance(self) -> None:
        legacy_db = _runtime_path / "legacy-create-task-schema.db"
        schema_path = Path(__file__).resolve().parent.parent / "schema.sql"
        schema_sql = schema_path.read_text(encoding="utf-8").replace(
            "    boot_volume_vpus_per_gb INTEGER DEFAULT 20 not null,\n",
            "",
        )
        with sqlite3.connect(legacy_db) as connection:
            connection.executescript(schema_sql)
            connection.execute(
                "INSERT INTO oci_create_task(id, ssh_public_key) VALUES (?, ?)",
                ("legacy-task", TEST_SSH_PUBLIC_KEY),
            )
            _apply_migrations(connection)
            boot_volume_vpus = connection.execute(
                "SELECT boot_volume_vpus_per_gb FROM oci_create_task WHERE id = ?",
                ("legacy-task",),
            ).fetchone()[0]

        self.assertEqual(boot_volume_vpus, 10)


class TelegramPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialization_error_does_not_log_credentials(self) -> None:
        bot_token = "bot-token-that-must-stay-private"
        chat_id = "private-chat-id"
        bot = MagicMock()
        bot.initialize = AsyncMock(
            side_effect=RuntimeError(f"The token `{bot_token}` was rejected by the server.")
        )

        with (
            patch("telegram.Bot", return_value=bot),
            patch("telegram_bot.logger.info") as info_log,
            patch("telegram_bot.logger.error") as error_log,
        ):
            notifier = TelegramNotifier(bot_token, chat_id)
            initialized = await notifier.initialize()

        logged_calls = f"{info_log.call_args_list!r} {error_log.call_args_list!r}"
        self.assertFalse(initialized)
        self.assertNotIn(bot_token, logged_calls)
        self.assertNotIn(chat_id, logged_calls)
        self.assertIn("RuntimeError", logged_calls)


class NotificationDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_outbox_deduplicates_and_retries_delivery(self) -> None:
        async with self.sessions() as db:
            for _ in range(2):
                await enqueue_notification(
                    db,
                    event_key="create-success:task-1:instance-1",
                    category="instance-create-success",
                    message="instance ready",
                )
            await db.commit()

        self.assertEqual(
            await dispatch_due_notifications(session_factory=self.sessions),
            1,
        )
        async with self.sessions() as db:
            event = (await db.execute(select(NotificationOutbox))).scalar_one()
            self.assertEqual(event.status, "pending")
            self.assertEqual(event.attempts, 0)
            self.assertIsNotNone(event.next_attempt_at)
            event.next_attempt_at = None
            await db.commit()

        sender = AsyncMock(side_effect=[False, True])
        self.assertEqual(
            await dispatch_due_notifications(session_factory=self.sessions, sender=sender),
            1,
        )
        async with self.sessions() as db:
            events = list((await db.execute(select(NotificationOutbox))).scalars())
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].status, "pending")
            self.assertEqual(events[0].attempts, 1)
            self.assertIsNotNone(events[0].next_attempt_at)
            events[0].next_attempt_at = None
            await db.commit()

        self.assertEqual(
            await dispatch_due_notifications(session_factory=self.sessions, sender=sender),
            1,
        )
        async with self.sessions() as db:
            event = (await db.execute(select(NotificationOutbox))).scalar_one()
            self.assertEqual(event.status, "sent")
            self.assertEqual(event.attempts, 2)
            self.assertIsNotNone(event.sent_at)
        self.assertEqual(sender.await_count, 2)
        self.assertEqual(notification_retry_delay(1), 5)
        self.assertEqual(notification_retry_delay(100), 3600)

    async def test_task_creation_enqueues_summary_notification(self) -> None:
        async with self.sessions() as db:
            db.add(
                OciUser(
                    id="user-1",
                    username="example-user",
                    oci_tenant_id="tenancy",
                    oci_user_id="oci-user",
                    oci_fingerprint="fingerprint",
                    oci_region="ap-tokyo-1",
                    oci_key_path="/tmp/test-key.pem",
                )
            )
            await db.commit()

            params = CreateInstanceParams(
                userId="user-1",
                ocpus=1,
                memory="6",
                disk=50,
                architecture="ARM",
                interval=5,
                intervalMax=30,
                maxAttempts=10,
                availabilityDomain=None,
                instanceName="web-server",
                createNumbers=3,
                operationSystem="Canonical Ubuntu",
                operationSystemVersion="24.04",
                sshPublicKey=TEST_SSH_PUBLIC_KEY,
            )
            with (
                patch("services.oci_service.run_oci", new=AsyncMock(return_value=None)),
                patch.object(OciService, "schedule_create_task") as schedule_task,
            ):
                task_id = await OciService.create_task(params, db)

            task = await db.get(OciCreateTask, task_id)
            notification = (await db.execute(select(NotificationOutbox))).scalar_one()

        schedule_task.assert_called_once_with(task_id, 5)
        self.assertEqual(task.status, "pending")
        self.assertEqual(task.instance_name, "web-server")
        self.assertEqual(task.initial_create_numbers, 3)
        self.assertEqual(notification.event_key, f"create-task-created:{task_id}")
        self.assertEqual(notification.category, "create-task-created")
        self.assertIn("【开机任务创建成功】", notification.message)
        self.assertIn(f"任务 ID：{task_id}", notification.message)
        self.assertIn("配置：[example-user]", notification.message)
        self.assertIn("区域：ap-tokyo-1", notification.message)
        self.assertIn("实例名称：web-server", notification.message)
        self.assertIn("可用域：自动轮换", notification.message)
        self.assertIn("VM.Standard.A1.Flex", notification.message)
        self.assertEqual(task.boot_volume_vpus_per_gb, 20)
        self.assertIn("20 VPU/GB", notification.message)
        self.assertIn("Canonical Ubuntu 24.04", notification.message)
        self.assertIn("重试间隔：5–30 秒，最大尝试：10 次", notification.message)
        self.assertNotIn(TEST_SSH_PUBLIC_KEY, notification.message)

    async def test_create_task_commits_success_and_notification_together(self) -> None:
        now = datetime.now()
        async with self.sessions() as db:
            db.add(
                OciUser(
                    id="user-1",
                    username="example-user",
                    oci_tenant_id="tenancy",
                    oci_user_id="oci-user",
                    oci_fingerprint="fingerprint",
                    oci_region="ap-tokyo-1",
                    oci_key_path="/tmp/test-key.pem",
                )
            )
            db.add(
                OciCreateTask(
                    id="task-1",
                    user_id="user-1",
                    oci_region="ap-tokyo-1",
                    ocpus=1,
                    memory=6,
                    disk=50,
                    boot_volume_vpus_per_gb=30,
                    architecture="ARM",
                    interval=5,
                    interval_max=30,
                    availability_domain=None,
                    availability_domain_index=0,
                    instance_name="web-server",
                    create_numbers=1,
                    initial_create_numbers=1,
                    ssh_public_key=TEST_SSH_PUBLIC_KEY,
                    operation_system="Canonical Ubuntu",
                    operation_system_version="24.04",
                    paused=0,
                    status="pending",
                    attempts=0,
                    max_attempts=10,
                    create_time=now,
                    updated_at=now,
                )
            )
            await db.commit()

        fetcher_context = MagicMock()
        fetcher_context.__enter__.return_value.create_instance_data.return_value = {
            "success": True,
            "retryable": False,
            "instance_id": "instance-1",
            "lifecycle_state": "RUNNING",
            "region": "ap-tokyo-1",
            "availability_domain": "AD-1",
            "architecture": "ARM",
            "ocpus": 1,
            "memory": 6,
            "disk": 50,
            "shape": "VM.Standard.A1.Flex",
            "public_ip": "203.0.113.10",
        }
        with (
            patch("services.instance_service.async_session", self.sessions),
            patch("services.instance_service.OracleInstanceFetcher", return_value=fetcher_context),
        ):
            result = await InstanceService.execute_create_task("task-1")

        self.assertTrue(result.done)
        self.assertEqual(
            fetcher_context.__enter__.return_value.configure_instance_creation.call_args.kwargs[
                "instance_name"
            ],
            "web-server",
        )
        self.assertEqual(
            fetcher_context.__enter__.return_value.configure_instance_creation.call_args.kwargs[
                "boot_volume_vpus_per_gb"
            ],
            30,
        )
        async with self.sessions() as db:
            task = await db.get(OciCreateTask, "task-1")
            notification = (await db.execute(select(NotificationOutbox))).scalar_one()
            self.assertEqual(task.status, "succeeded")
            self.assertEqual(task.create_numbers, 0)
            self.assertEqual(notification.status, "pending")
            self.assertEqual(
                notification.event_key,
                "create-success:task-1:instance-1",
            )
            self.assertIn("开机成功", notification.message)


if __name__ == "__main__":
    unittest.main()
