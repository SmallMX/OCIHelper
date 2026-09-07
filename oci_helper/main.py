"""
OCI Helper - Python 版主入口

对应 Java 原项目：com.yohann.ocihelper.OciHelperApplication
功能：
    - FastAPI 应用初始化
    - 路由注册
    - 全局异常处理
    - 启动/关闭生命周期挂钩
    - 数据库初始化
    - 任务调度器启动
    - Telegram Bot 初始化

启动方式：
    python main.py
    # 或
    uvicorn main:app --host 0.0.0.0 --port 8888 --reload
"""

import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

# 将项目根目录加入 sys.path，确保模块可被正确导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

from config import settings
from database import init_db
from exceptions import OciException
from schemas.response import ResponseData

# ============================
#  生命周期管理
# ============================


def _configure_logging() -> None:
    Path(settings.log_file_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level,
        backtrace=False,
        diagnose=False,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>",
    )
    logger.add(
        settings.log_file_path,
        rotation="10 MB",
        retention="7 days",
        level=settings.log_level,
        backtrace=False,
        diagnose=False,
        encoding="utf-8",
    )


async def _restore_tasks():
    """
    恢复中断的创建任务

    对应 Java: OciTask.restartCreateTask()
    应用重启后，从数据库读取所有未完成的创建任务，
    重新提交到调度器继续执行。
    """
    from sqlalchemy import select

    from database import async_session
    from models.oci_change_ip_task import OciChangeIpTask
    from models.oci_create_task import OciCreateTask
    from services.oci_service import OciService

    try:
        async with async_session() as db:
            create_result = await db.execute(
                select(OciCreateTask).where(OciCreateTask.status.in_({"pending", "running"}))
            )
            create_tasks = list(create_result.scalars())
            for task in create_tasks:
                task.status = "pending"
                initial_delay = 0.0
                if task.next_run_at is not None:
                    initial_delay = max(
                        0.0, (task.next_run_at - datetime.now()).total_seconds()
                    )
                OciService.schedule_create_task(
                    task.id,
                    task.interval,
                    initial_delay=initial_delay,
                )

            change_result = await db.execute(
                select(OciChangeIpTask).where(OciChangeIpTask.status.in_({"pending", "running"}))
            )
            change_tasks = list(change_result.scalars())
            for task in change_tasks:
                task.status = "pending"
                OciService.schedule_change_ip_task(task)
            await db.commit()
            logger.info(
                "已恢复创建任务 {} 个、换 IP 任务 {} 个",
                len(create_tasks),
                len(change_tasks),
            )
    except Exception as e:
        logger.error(f"恢复任务失败: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI 应用生命周期管理

    对应 Java 原项目：
        - OciHelperApplication 中的 @Bean 初始化
        - OciTask 中的 @PostConstruct 启动任务

    启动阶段：
        1. 初始化数据库（创建表结构）
        2. 启动 Telegram Bot
        3. 启动持久化通知投递器
        4. 恢复中断的创建任务

    关闭阶段：
        1. 关闭任务调度器
        2. 关闭通知投递器
        3. 关闭 Telegram Bot
    """
    # ---- 启动阶段 ----
    _configure_logging()
    logger.info("=" * 60)
    logger.info("  OCI Helper (Python) 正在启动...")
    logger.info(f"  监听端口: {settings.server_port}")
    logger.info(f"  数据库路径: {settings.db_path}")
    logger.info(f"  密钥目录: {settings.key_dir_path}")
    logger.info("=" * 60)

    settings.validate_runtime(require_bootstrap_credentials=False)

    # 初始化数据库
    await init_db()
    logger.info("✅ 数据库初始化完成")

    from core.admin_credentials import load_admin_credentials
    from database import async_session

    async with async_session() as db:
        credentials = await load_admin_credentials(db)
    if not credentials.is_persisted:
        settings.validate_runtime()

    from core.task_scheduler import scheduler

    scheduler.start()

    # 初始化 Telegram Bot
    try:
        from sqlalchemy import select

        from core.secrets import decrypt_secret
        from models.oci_kv import OciKv
        from telegram_bot import init_telegram_bot

        async with async_session() as db:
            # 从数据库获取 Bot 配置
            bot_token_result = await db.execute(
                select(OciKv.value).where(OciKv.code == "SYS_TG_BOT_TOKEN")
            )
            chat_id_result = await db.execute(
                select(OciKv.value).where(OciKv.code == "SYS_TG_CHAT_ID")
            )
            stored_bot_token = bot_token_result.scalar_one_or_none()
            bot_token = decrypt_secret(stored_bot_token) if stored_bot_token else None
            chat_id = chat_id_result.scalar_one_or_none()

            if bot_token and chat_id:
                await init_telegram_bot(bot_token, chat_id)
                logger.info("✅ Telegram Bot 初始化完成")
            else:
                logger.info("⏭️ Telegram Bot 未配置，跳过初始化")
    except Exception as exc:
        logger.warning("Telegram Bot 初始化失败（非致命）: error_type={}", type(exc).__name__)

    from services.notification_service import notification_dispatcher

    notification_dispatcher.start()

    # 恢复中断的创建任务
    await _restore_tasks()

    logger.info("🚀 OCI Helper 启动完成！")

    yield

    # ---- 关闭阶段 ----
    logger.info("OCI Helper 正在关闭...")

    # 关闭任务调度器
    scheduler.shutdown()
    logger.info("✅ 任务调度器已关闭")

    await notification_dispatcher.stop()

    # 关闭 Telegram Bot
    try:
        from telegram_bot import shutdown_telegram_bot

        await shutdown_telegram_bot()
        logger.info("✅ Telegram Bot 已关闭")
    except Exception as exc:
        logger.warning("Telegram Bot 关闭失败: error_type={}", type(exc).__name__)

    logger.info("👋 OCI Helper 已停止")


# ============================
#  FastAPI 应用初始化
# ============================

app = FastAPI(
    title="OCI Helper",
    description="Oracle Cloud Infrastructure 辅助管理工具 (Python 版)",
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    is_api_path = request.url.path.startswith("/api/")
    if is_api_path:
        response.headers["Cache-Control"] = "no-store"
    elif response.headers.get("Content-Type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache"
    if (
        not is_api_path
        and not request.url.path.startswith("/docs")
        and request.url.path
        not in {
            "/redoc",
            "/openapi.json",
        }
    ):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; object-src 'none'; base-uri 'self'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


if settings.allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )


# ============================
#  全局异常处理
# ============================


@app.exception_handler(OciException)
async def oci_exception_handler(request: Request, exc: OciException):
    """
    自定义业务异常处理器

    对应 Java: @ControllerAdvice 全局异常处理
    将 OciException 转换为统一 JSON 响应。
    """
    logger.warning(f"业务异常: {exc.message} (code={exc.code})")
    return JSONResponse(
        status_code=200,
        content=ResponseData.fail(message=exc.message, code=exc.code),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    details = [
        {
            "field": ".".join(str(item) for item in error["loc"] if item != "body"),
            "message": error["msg"],
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            **ResponseData.fail(message="请求参数校验失败", code=422),
            "data": details,
        },
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content=ResponseData.fail(message=str(exc.detail), code=exc.status_code),
        headers=exc.headers,
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """
    全局未捕获异常处理器

    将所有未预期的异常转换为统一错误响应，避免暴露内部错误信息。
    """
    logger.error(f"未捕获异常: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content=ResponseData.fail(message="服务器内部错误", code=-1),
    )


# ============================
#  注册路由
# ============================

# 系统管理路由 (/api/sys)
from routers.sys_router import router as sys_router

app.include_router(sys_router)

# OCI 配置与实例管理路由 (/api/oci)
from routers.oci_router import router as oci_router

app.include_router(oci_router)

# SSH 公钥管理（公钥持久化，生成的私钥仅在下载响应中返回）
from routers.ssh_key_router import router as ssh_key_router

app.include_router(ssh_key_router)

# 附属路由（租户、安全规则、VCN、引导卷、流量、限额、IP 数据）
from routers.extra_routers import (
    boot_volume_router,
    limits_router,
    security_rule_router,
    tenant_router,
    traffic_router,
    vcn_router,
)

app.include_router(tenant_router)
app.include_router(security_rule_router)
app.include_router(vcn_router)
app.include_router(boot_volume_router)
app.include_router(traffic_router)
app.include_router(limits_router)


# ============================
#  前端静态文件服务
# ============================

from fastapi.responses import FileResponse

# 获取 dist 目录的绝对路径
_dist_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """网站图标"""
    return FileResponse(os.path.join(_dist_dir, "favicon.ico"))


@app.get("/api/health", include_in_schema=False)
async def health():
    return {"status": "ok"}


@app.get("/{full_path:path}", include_in_schema=False)
async def serve_frontend(full_path: str):
    """
    SPA 前端 Fallback

    所有未匹配到 /api/* 和 /docs 的 GET 请求都返回 index.html，
    由 Vue Router 在客户端处理路由。
    """
    from pathlib import Path

    if full_path == "api" or full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="API 路径不存在")

    dist_path = Path(_dist_dir).resolve()
    file_path = (dist_path / full_path).resolve()
    if full_path:
        try:
            file_path.relative_to(dist_path)
        except ValueError:
            file_path = dist_path / "index.html"
        if file_path.is_file():
            return FileResponse(file_path)
    # SPA fallback
    return FileResponse(os.path.join(_dist_dir, "index.html"))


# ============================
#  启动入口
# ============================

if __name__ == "__main__":
    # 启动 Uvicorn 服务器
    uvicorn.run(
        "main:app",
        host=settings.server_host,
        port=settings.server_port,
        reload=False,
        log_level="info",
    )
