# =============================================================================
# OCI Helper - Python 版 Docker 镜像
#
# 对应 Java 原项目的 Dockerfile，将 Spring Boot JAR 替换为 Uvicorn + FastAPI
#
# 构建：
#   docker build -t oci-helper-python .
#
# 运行：
#   OCI_HELPER_WEB_PASSWORD 须预先设为至少 12 位的唯一强密码。
#   docker run -d \
#     -p 127.0.0.1:8888:8888 \
#     -v /opt/oci-helper/data:/app/data \
#     -v /opt/oci-helper/keys:/app/keys \
#     -e OCI_HELPER_WEB_ACCOUNT=admin \
#     -e OCI_HELPER_WEB_PASSWORD \
#     --name oci-helper \
#     oci-helper-python
# =============================================================================

# ---- 第一阶段：依赖安装 ----
FROM python:3.11-slim AS builder

# 设置工作目录
WORKDIR /app

# 安装系统依赖（用于编译 Python 扩展）
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libffi-dev && \
    rm -rf /var/lib/apt/lists/*

# 复制依赖清单并安装
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- 第二阶段：运行镜像 ----
FROM python:3.11-slim

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

# 设置工作目录
WORKDIR /app

# 从构建阶段拷贝已安装的依赖
COPY --from=builder /install /usr/local

# 复制应用代码
COPY --chown=10001:10001 . .

# 创建数据和密钥目录
RUN mkdir -p /app/data /app/keys /app/logs && \
    chown -R 10001:10001 /app

USER 10001:10001

# 暴露端口（默认 8888，与 Java 版一致）
EXPOSE 8888

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import httpx; r = httpx.get('http://localhost:8888/api/health'); exit(0 if r.status_code==200 else 1)" || exit 1

# 启动命令
CMD ["python", "main.py"]
