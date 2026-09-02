"""Import every active ORM model so SQLAlchemy can discover its table."""

from models.notification_outbox import NotificationOutbox
from models.oci_change_ip_task import OciChangeIpTask
from models.oci_create_task import OciCreateTask
from models.oci_kv import OciKv
from models.oci_user import OciUser
from models.ssh_public_key import SshPublicKey

__all__ = [
    "NotificationOutbox",
    "OciChangeIpTask",
    "OciCreateTask",
    "OciKv",
    "OciUser",
    "SshPublicKey",
]
