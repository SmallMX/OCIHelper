# OCI Helper

[English](README.md) | 简体中文

OCI Helper 是一个基于 FastAPI、OCI Python SDK 和 SQLite 的 Oracle Cloud Web 管理面板，用于管理 OCI API 配置、实例生命周期、开机任务、网络资源、引导卷和公网 IP。

## 核心功能

- 管理多组 OCI API 配置及托管 PEM 私钥
- 在 Web 管理页修改管理员用户名和密码
- 创建、启动、停止、重启、改名和终止实例
- 支持 ARM、AMD 与 Flex Shape 的 OCPU、内存、引导卷容量和性能配置
- 通过 SSH 公钥初始化 Linux 实例，不收集或启用 root 密码登录
- 使用随机创建间隔、最大尝试次数和可用域轮换持续创建实例
- 持久化开机任务与换 IP 任务，服务重启后自动恢复
- 在管理面板查看最新的任务执行日志
- 管理 VCN 默认安全列表、引导卷、服务限额和实例网络流量
- 通过 Telegram 发送任务创建、开机结果、换 IP 结果和破坏性操作验证码

## 快速部署

推荐通过 Docker Compose 使用已发布到 GHCR 的镜像。需要提前安装 Git、Docker Engine 和 Docker Compose 插件。

```bash
git clone https://github.com/SmallMX/OCIHelper.git
cd OCIHelper/oci_helper
cp .env.example .env
```

编辑 `.env`，至少设置管理面板密码：

```dotenv
OCI_HELPER_IMAGE_TAG=latest
OCI_HELPER_WEB_ACCOUNT=admin
OCI_HELPER_WEB_PASSWORD=replace-with-at-least-12-characters
```

`OCI_HELPER_IMAGE_TAG` 默认为 `latest`；如需可复现部署，可固定为 `0.0.2` 等具体版本。账号变量用于初始化管理员凭据。通过 Web 管理页修改管理员账号后，数据库中的凭据会优先于环境变量。

启动服务：

```bash
docker compose pull
docker compose up -d
docker compose ps
```

默认入口：

- 管理面板：`http://127.0.0.1:8888`
- OpenAPI：`http://127.0.0.1:8888/docs`
- 健康检查：`http://127.0.0.1:8888/api/health`

Docker Compose 默认只把管理端口绑定到宿主机回环地址。生产环境建议保持该默认值，并通过 Caddy、Nginx 或其他可信反向代理提供 HTTPS。只有确实需要其他监听地址时才设置 `OCI_HELPER_BIND_ADDRESS`，不要直接将管理端口暴露到公网。

## 申请 OCI API 密钥

OCI Helper 使用 OCI API Signing Key 调用云端接口。它是 PEM 格式的 RSA 密钥，与登录计算实例使用的 SSH 密钥不同。以下步骤基于 [Oracle 官方 API 密钥文档](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/apisigningkey.htm)。

### 生成密钥

1. 登录 [OCI Console](https://cloud.oracle.com/)。
2. 打开目标用户的详情页：
   - 为当前账号添加密钥：点击右上角个人资料菜单，选择 **User settings**。
   - 管理员为其他用户添加密钥：依次打开 **Identity & Security**、**Users**，再选择目标用户。
3. 在左侧 **Resources** 区域选择 **API Keys**，然后点击 **Add API Key**。
4. 保持 **Generate API Key Pair**，点击 **Download Private Key**，立即保存下载的 `.pem` 私钥。
5. 点击 **Add**。密钥添加成功后会显示 **Configuration File Preview**，复制其中的全部配置内容。

> 私钥只应由 OCI Helper 的管理员保管，不要发送到聊天工具、提交到 Git 仓库或当作 SSH 公钥使用。私钥丢失后无法从公钥还原，应在 OCI Console 中删除对应指纹并重新生成密钥。

Configuration File Preview 的内容类似：

```ini
[DEFAULT]
user=<your-user-ocid>
fingerprint=<your-api-key-fingerprint>
tenancy=<your-tenancy-ocid>
region=<your-region>
key_file=/path/to/oci_api_key.pem
```

| 字段 | 含义 |
|---|---|
| `user` | 使用该 API Key 的用户 OCID |
| `fingerprint` | 已添加公钥的指纹 |
| `tenancy` | OCI 租户 OCID |
| `region` | 默认访问区域，例如 `ap-tokyo-1` |
| `key_file` | Oracle 工具使用的本地路径；OCI Helper 不直接采用此路径 |

### 添加到本项目

1. 登录 OCI Helper 管理面板，进入“OCI 配置”。
2. 点击“添加配置”，填写便于识别的配置名称。
3. 将完整的 Configuration File Preview 粘贴到“OCI config 内容”。
4. 在“PEM 私钥”中上传刚才下载的 `.pem` 文件。
5. 点击“验证并保存”。应用会先调用 OCI API 查询可用域，验证通过后才会保存配置。

配置内容至少需要 `user`、`tenancy`、`region` 和 `fingerprint`。其中 `key_file` 可以保留，但不会被直接采用；应用只使用本次上传并以 `0600` 权限托管的 PEM 私钥。当前表单没有私钥口令字段，建议直接使用 OCI Console 生成的私钥，不要上传需要口令解密的自定义 PEM。

### 配置权限

API Key 只负责身份认证，不会额外授予云资源权限。用户还必须加入具有相应 [IAM Policy](https://docs.oracle.com/en-us/iaas/Content/Identity/policysyntax/policy-syntax.htm) 的用户组，才能创建实例、网络和引导卷。建议为 OCI Helper 创建专用 IAM 用户，并按实际使用的功能和区间授予最小权限，不要为了排查问题长期授予整个租户的完全管理权限。

如果保存失败，可根据错误信息检查：

- `401` 或 `NotAuthenticated`：确认 PEM 私钥、`fingerprint` 和 OCI Console 中的 API Key 属于同一密钥对。
- `403` 或 `NotAuthorizedOrNotFound`：确认用户已加入正确的用户组，且 IAM Policy 覆盖目标租户或区间。
- 连接超时：确认服务器可以通过 HTTPS 访问配置中 `region` 对应的 OCI API Endpoint。

## 创建实例

添加 OCI 配置后，在配置列表中选择“创建任务”。主要参数与参考项目的对应关系如下：

| 参考配置 | Web 表单 | 说明 |
|---|---|---|
| `shape` | 架构 / Shape | 选择实例规格 |
| `cpus` | OCPU | Flex Shape 的 OCPU 数量 |
| `memoryInGBs` | 内存 | Flex Shape 的内存大小 |
| `bootVolumeSizeInGBs` | 引导卷 | 引导卷容量，单位 GB |
| `bootVolumeVpusPerGB` | 引导卷性能 | 默认 `20`（高性能）；支持 `10`、`20` 或 `30` 至 `120` VPU/GB |
| `sum` | 创建数量 | 本任务需要成功创建的实例数 |
| `availabilityDomain` | 可用域 | 留空时自动轮换支持当前 Shape 的可用域 |
| `minTime` | 最短创建间隔 | 每次 OCI 创建调用后的最短等待秒数 |
| `maxTime` | 最长创建间隔 | 每次等待会在最短与最长间隔之间随机取值 |
| `retry=-1` | 最大尝试次数 `0` | `0` 表示不限次数，其他值表示任务总尝试上限 |
| `ssh_authorized_key` | SSH 公钥 | 必填；支持一行或多行 OpenSSH 公钥 |

创建任务前需要准备 SSH 密钥对。Linux、macOS 和新版 Windows 可以使用 OpenSSH 生成：

```bash
ssh-keygen -t ed25519 -C "oci-helper"
cat ~/.ssh/id_ed25519.pub
```

将 `.pub` 文件内容粘贴到“SSH 公钥”，私钥应只保存在登录设备上，不能上传到 OCI Helper。Ubuntu 镜像使用 `ubuntu` 用户登录，Oracle Linux 和 CentOS 使用 `opc`，需要 root 权限时执行 `sudo -i`。

任务每次调度只执行一次 `LaunchInstance`。明确的容量不足会切换到下一个可用域；网络超时等结果不确定的错误会保持原可用域并复用 OCI 幂等令牌，避免盲目重试产生重复实例。

固定规格 `VM.Standard.E2.1.Micro` 使用 OCI 规定的固定配置；Flex Shape 才会采用表单中的 OCPU 和内存。

## 管理员账号

进入“系统设置”可修改管理员用户名和密码。保存前必须输入当前密码；只修改用户名时可以留空新密码。新密码至少 12 个字符，保存后使用 PBKDF2-SHA256 强哈希存入 SQLite，不会保存或返回明文。

修改成功后所有现有登录令牌都会立即失效，管理页会返回登录界面并要求使用新凭据登录。此后 `OCI_HELPER_WEB_ACCOUNT` 和 `OCI_HELPER_WEB_PASSWORD` 只作为数据库中没有管理员凭据时的回退值。通过管理页保存凭据后，可以从 `.env` 中移除初始密码；使用全新数据库启动时仍须设置强密码。

## Telegram

在“系统设置”中填写 Telegram Bot Token 和 Chat ID，保存前应用会验证配置。可以通过 [BotFather](https://t.me/BotFather) 创建 Bot，并通过 [IDBot](https://t.me/myidbot) 获取 Chat ID。

Telegram 用于发送任务通知，以及终止实例、终止引导卷和删除 VCN 等破坏性操作的短时验证码。出于安全考虑，已保存的 Bot Token 不会通过 API 返回。

开机和换 IP 结果会先与任务状态一同写入 SQLite 通知发件箱，再由主事件循环发送。临时网络失败会按指数退避重试，服务重启后继续处理尚未发送的通知；未配置 Telegram 时不会消耗重试次数。

## 版本与容器镜像

每个 `vX.Y.Z` Git 标签都会触发[发布工作流](.github/workflows/release.yml)。GitHub Actions 会为 `linux/amd64` 和 `linux/arm64` 构建 Docker 镜像，将版本标签和 `latest` 推送到 GHCR，记录来源证明，并创建对应的 [GitHub Release](https://github.com/SmallMX/OCIHelper/releases)。

```bash
docker pull ghcr.io/smallmx/ocihelper:0.0.2
```

所有已发布镜像可在 [OCI Helper 容器包](https://github.com/SmallMX/OCIHelper/pkgs/container/ocihelper)中查看。

## 更新部署

升级前建议备份 Docker 数据卷，然后在仓库目录执行：

```bash
git pull --ff-only
cd oci_helper
docker compose pull
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:8888/api/health
```

如果固定了 `OCI_HELPER_IMAGE_TAG`，拉取前先将它改为目标版本。数据库迁移会在应用启动时自动执行。不要删除或更换数据卷中的 `.oci-helper-data-key`，否则已保存的 Telegram Token 将无法解密。

## 数据目录

Docker Compose 使用命名卷持久化数据：

| 数据 | 容器路径 | 用途 |
|---|---|---|
| SQLite | `/app/data` | 配置、任务状态和加密密钥 |
| OCI 私钥 | `/app/keys` | 应用托管的 PEM 文件 |
| 日志 | `/app/logs` | 文件日志 |

容器重建不会删除命名卷。迁移服务器或清理 Docker 资源前，应先完成卷级备份。

## 常用命令

```bash
# 查看运行状态
docker compose ps

# 拉取当前配置的镜像标签
docker compose pull

# 跟踪日志
docker compose logs -f

# 重启服务
docker compose restart

# 停止服务但保留数据卷
docker compose down
```

更完整的环境变量、安全边界、架构和开发说明见 [oci_helper/README.md](oci_helper/README.md)。

## 许可证

本项目使用 [Apache License 2.0](LICENSE)。
