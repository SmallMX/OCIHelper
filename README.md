# OCI Helper

English | [简体中文](README.zh-CN.md)

OCI Helper is a self-hosted Oracle Cloud Infrastructure (OCI) web management panel built with FastAPI, the OCI Python SDK, and SQLite. It manages OCI API profiles, instance lifecycles, instance creation tasks, networking resources, boot volumes, and public IP addresses.

## Features

- Manage multiple OCI API profiles and hosted PEM private keys
- Change the administrator username and password from the web interface
- Create, start, stop, restart, rename, and terminate instances
- Configure OCPUs, memory, boot volume size, and performance for ARM, AMD, and Flex shapes
- Initialize Linux instances with SSH public keys without enabling root password login
- Run persistent instance creation tasks with randomized intervals, attempt limits, and availability domain rotation
- Resume instance creation and public IP rotation tasks after service restarts
- Inspect recent task execution logs from the management panel
- Manage VCN default security lists, boot volumes, service limits, and instance network traffic
- Send task, instance creation, and public IP notifications through Telegram
- Protect destructive operations with short-lived Telegram verification codes

## Quick Start

Docker Compose with the published GHCR image is the recommended deployment method. Install Git, Docker Engine, and the Docker Compose plugin first.

```bash
git clone https://github.com/SmallMX/OCIHelper.git
cd OCIHelper/oci_helper
cp .env.example .env
```

Edit `.env` and set at least the management panel password:

```dotenv
OCI_HELPER_IMAGE_TAG=latest
OCI_HELPER_WEB_ACCOUNT=admin
OCI_HELPER_WEB_PASSWORD=replace-with-at-least-12-characters
```

`OCI_HELPER_IMAGE_TAG` defaults to `latest`. Set it to a release version such as `0.0.2` when you want a reproducible deployment. The account variables initialize the administrator credentials. After the credentials are changed from the web interface, the values stored in the database take precedence over the environment variables.

Start the service:

```bash
docker compose pull
docker compose up -d
docker compose ps
```

Default endpoints:

- Management panel: `http://127.0.0.1:8888`
- OpenAPI documentation: `http://127.0.0.1:8888/docs`
- Health check: `http://127.0.0.1:8888/api/health`

Docker Compose binds the management port to the host loopback interface by default. For production deployments, keep that default and place OCI Helper behind Caddy, Nginx, or another trusted reverse proxy with HTTPS. Set `OCI_HELPER_BIND_ADDRESS` only when another binding is intentional; do not expose the management port directly to the public internet.

## Configure OCI API Access

OCI Helper calls OCI services with an API signing key. This is a PEM-formatted RSA key and is different from the SSH key used to log in to compute instances. The following workflow is based on the [Oracle API signing key documentation](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/apisigningkey.htm).

### Generate an API Key

1. Sign in to the [OCI Console](https://cloud.oracle.com/).
2. Open the target user's details page:
   - For the current account, open the profile menu in the upper-right corner and select **User settings**.
   - To manage another user as an administrator, open **Identity & Security**, select **Users**, and choose the target user.
3. Select **API Keys** under **Resources**, then click **Add API Key**.
4. Keep **Generate API Key Pair** selected, click **Download Private Key**, and immediately save the downloaded `.pem` private key.
5. Click **Add**. After the key is added, copy the complete **Configuration File Preview**.

> Keep the private key under the control of the OCI Helper administrator. Never send it through chat, commit it to Git, or use it as an SSH public key. A lost private key cannot be recovered from its public key; delete the matching fingerprint in the OCI Console and generate a new key pair instead.

The Configuration File Preview looks like this:

```ini
[DEFAULT]
user=<your-user-ocid>
fingerprint=<your-api-key-fingerprint>
tenancy=<your-tenancy-ocid>
region=<your-region>
key_file=/path/to/oci_api_key.pem
```

| Field | Description |
|---|---|
| `user` | OCID of the user associated with the API key |
| `fingerprint` | Fingerprint of the public key registered in OCI |
| `tenancy` | OCI tenancy OCID |
| `region` | Default region, for example `ap-tokyo-1` |
| `key_file` | Local path used by Oracle tools; OCI Helper does not use this path directly |

### Add the Profile to OCI Helper

1. Sign in to the OCI Helper management panel and open **OCI 配置 (OCI Configurations)**.
2. Click **添加配置 (Add Configuration)** and enter a recognizable name.
3. Paste the complete Configuration File Preview into **OCI config 内容 (OCI config content)**.
4. Upload the downloaded `.pem` file under **PEM 私钥 (PEM private key)**.
5. Click **验证并保存 (Validate and Save)**. OCI Helper queries the OCI API for availability domains and saves the profile only after validation succeeds.

The configuration must contain `user`, `tenancy`, `region`, and `fingerprint`. You may leave `key_file` in the pasted content, but OCI Helper ignores that path and only uses the uploaded PEM file, which it stores with `0600` permissions. The form does not accept a private-key passphrase, so use the key generated by the OCI Console instead of a custom encrypted PEM file.

### Configure IAM Permissions

An API key authenticates a user but does not grant additional permissions. The user must belong to a group covered by an appropriate [IAM policy](https://docs.oracle.com/en-us/iaas/Content/Identity/policysyntax/policy-syntax.htm) before it can create instances, networks, or boot volumes.

Create a dedicated IAM user for OCI Helper and grant only the permissions required for the features and compartments you use. Avoid granting tenancy-wide administrative access as a permanent troubleshooting workaround.

If validation fails, check the error and the following common causes:

- `401` or `NotAuthenticated`: verify that the PEM private key, `fingerprint`, and OCI Console API key belong to the same key pair.
- `403` or `NotAuthorizedOrNotFound`: verify the user's group membership and confirm that the IAM policy covers the target tenancy or compartment.
- Connection timeout: verify that the server can reach the regional OCI API endpoint over HTTPS.

## Create Instances

After adding an OCI profile, select **创建任务 (Create Task)** from the configuration list. The main parameters correspond to the reference project as follows:

| Reference option | Web form | Description |
|---|---|---|
| `shape` | Architecture / Shape | Instance shape to create |
| `cpus` | OCPU | OCPU count for Flex shapes |
| `memoryInGBs` | Memory | Memory size for Flex shapes |
| `bootVolumeSizeInGBs` | Boot volume | Boot volume size in GB |
| `bootVolumeVpusPerGB` | Boot volume performance | Defaults to `20` (higher performance); supports `10`, `20`, or `30` through `120` VPU/GB |
| `sum` | Instance count | Number of instances the task must create successfully |
| `availabilityDomain` | Availability domain | When empty, rotate through availability domains that support the selected shape |
| `minTime` | Minimum interval | Minimum delay in seconds after each OCI create request |
| `maxTime` | Maximum interval | Each delay is randomized between the minimum and maximum values |
| `retry=-1` | Maximum attempts `0` | `0` means unlimited; any other value is the task-wide attempt limit |
| `ssh_authorized_key` | SSH public key | Required; accepts one or more OpenSSH public keys |

Generate an SSH key pair before creating a task. Linux, macOS, and recent Windows versions can use OpenSSH:

```bash
ssh-keygen -t ed25519 -C "oci-helper"
cat ~/.ssh/id_ed25519.pub
```

Paste the `.pub` file content into **SSH 公钥 (SSH public key)**. Keep the private key only on the device used to connect to the instance and never upload it to OCI Helper. Ubuntu images use the `ubuntu` account; Oracle Linux and CentOS images use `opc`. Run `sudo -i` when root privileges are required.

Each scheduled run performs at most one `LaunchInstance` call. Explicit capacity errors rotate to the next availability domain. Errors with uncertain outcomes, such as network timeouts, keep the same availability domain and reuse the OCI idempotency token to avoid creating duplicate instances through blind retries.

The fixed `VM.Standard.E2.1.Micro` shape always uses the configuration defined by OCI. OCPU and memory values from the form apply only to Flex shapes.

## Administrator Account

Open **系统设置 (System Settings)** to change the administrator username or password. The current password is required before saving. You may leave the new password empty when changing only the username; a new password must contain at least 12 characters.

Passwords are stored in SQLite as strong PBKDF2-SHA256 hashes and are never stored or returned in plaintext. After a successful change, all existing login tokens are immediately invalidated and the management panel requires the new credentials. `OCI_HELPER_WEB_ACCOUNT` and `OCI_HELPER_WEB_PASSWORD` then remain fallback values used only when the database does not contain administrator credentials. Once credentials have been saved through the management panel, you can remove the bootstrap password from `.env`; a fresh database still requires a strong bootstrap password to start.

## Telegram

Enter a Telegram Bot Token and Chat ID under **系统设置 (System Settings)**. OCI Helper validates the configuration before saving it. Create a bot with [BotFather](https://t.me/BotFather) and obtain a Chat ID with [IDBot](https://t.me/myidbot).

Telegram sends task notifications and short-lived verification codes for destructive operations such as terminating instances or boot volumes and deleting VCNs. Saved Bot Tokens are never returned by the API.

Instance creation and public IP results are written to a SQLite notification outbox in the same transaction as the task state, then delivered by the main event loop. Temporary network failures use exponential backoff, and pending notifications resume after a service restart. Retries are not consumed while Telegram is unconfigured.

## Releases and Container Images

Each `vX.Y.Z` Git tag runs the [release workflow](.github/workflows/release.yml). GitHub Actions builds the Docker image for `linux/amd64` and `linux/arm64`, publishes the versioned and `latest` tags to GHCR, records a provenance attestation, and creates the matching [GitHub Release](https://github.com/SmallMX/OCIHelper/releases).

```bash
docker pull ghcr.io/smallmx/ocihelper:0.0.2
```

Published images are listed in the [OCI Helper container package](https://github.com/SmallMX/OCIHelper/pkgs/container/ocihelper).

## Upgrade

Back up the Docker data volumes before upgrading, then run the following commands from the repository directory:

```bash
git pull --ff-only
cd oci_helper
docker compose pull
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:8888/api/health
```

When `OCI_HELPER_IMAGE_TAG` is pinned, update it to the target release before pulling. Database migrations run automatically during application startup. Do not delete or replace `.oci-helper-data-key` in the data volume, or saved Telegram Bot Tokens can no longer be decrypted.

## Persistent Data

Docker Compose uses named volumes for persistent data:

| Data | Container path | Purpose |
|---|---|---|
| SQLite | `/app/data` | Profiles, task state, and encryption keys |
| OCI private keys | `/app/keys` | PEM files managed by the application |
| Logs | `/app/logs` | Application log files |

Rebuilding the container does not delete named volumes. Back up the volumes before migrating servers or cleaning Docker resources.

## Common Commands

```bash
# Show service status
docker compose ps

# Pull the configured image tag
docker compose pull

# Follow logs
docker compose logs -f

# Restart the service
docker compose restart

# Stop the service and preserve data volumes
docker compose down
```

For the complete environment variable list, architecture, security boundaries, and development guide, see the [application documentation (Chinese)](oci_helper/README.md).

## License

Licensed under the [Apache License 2.0](LICENSE).
