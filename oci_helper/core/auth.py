"""
JWT 认证与安全中间件

对应 Java 原项目：
    - com.yohann.ocihelper.config.auth.AuthInterceptor
    - CommonUtils 中的 JWT 生成/验证方法

功能：
    - 生成和验证 JWT 令牌
    - FastAPI 依赖注入：请求认证拦截
    - IP 黑名单和登录失败自动封禁机制
"""

import threading
import time
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Depends, HTTPException, Request
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from core.admin_credentials import constant_time_equal, load_admin_credentials
from database import get_db

# ============================
#  JWT 配置常量
# ============================

# JWT 签名算法
JWT_ALGORITHM: str = "HS256"

# 无需认证的白名单路径
AUTH_WHITELIST: set = {
    "/api/sys/login",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/favicon.ico",
    "/api/health",
}


# ============================
#  IP 安全管理
# ============================


class IpSecurityManager:
    """
    IP 安全管理器

    对应 Java 原项目中 IpSecurityService 和 AuthInterceptor 的 IP 封禁逻辑。
    实现：
        - 登录失败计数
        - 自动封禁 IP（连续失败 5 次封禁 24 小时）
        - 黑名单管理

    使用线程安全的 dict 实现，适合单进程场景。
    """

    def __init__(self):
        # 登录失败计数器: {ip: count}
        self._fail_counts: dict[str, int] = {}
        # IP 黑名单: {ip: 解封时间戳}
        self._blacklist: dict[str, float] = {}
        # 连续失败多少次触发封禁
        self._max_failures = settings.ip_ban_threshold
        self._ban_duration = settings.ip_ban_minutes * 60
        self._lock = threading.RLock()

    def record_failure(self, ip: str) -> None:
        """
        记录一次登录失败

        对应 Java: IpSecurityService.recordLoginFailure(ip)

        参数:
            ip: 客户端 IP 地址
        """
        with self._lock:
            count = self._fail_counts.get(ip, 0) + 1
            self._fail_counts[ip] = count
        logger.warning(f"IP {ip} 登录失败第 {count} 次")

        # 达到阈值则自动封禁
        if count >= self._max_failures:
            self.ban_ip(ip)
            logger.warning(f"IP {ip} 已被封禁 {self._ban_duration // 3600} 小时")

    def record_success(self, ip: str) -> None:
        """
        记录登录成功，清除失败计数

        参数:
            ip: 客户端 IP 地址
        """
        with self._lock:
            self._fail_counts.pop(ip, None)

    def ban_ip(self, ip: str) -> None:
        """
        封禁指定 IP

        参数:
            ip: 要封禁的 IP 地址
        """
        with self._lock:
            self._blacklist[ip] = time.time() + self._ban_duration
            self._fail_counts.pop(ip, None)

    def is_banned(self, ip: str) -> bool:
        """
        检查 IP 是否被封禁

        对应 Java: AuthInterceptor 中的 IP 黑名单检查

        参数:
            ip: 客户端 IP 地址

        返回:
            True 表示已被封禁
        """
        with self._lock:
            if ip not in self._blacklist:
                return False
            if time.time() > self._blacklist[ip]:
                self._blacklist.pop(ip, None)
                return False
            return True

    def unban_ip(self, ip: str) -> None:
        """手动解封 IP"""
        with self._lock:
            self._blacklist.pop(ip, None)
            self._fail_counts.pop(ip, None)


# 全局 IP 安全管理器实例
ip_security = IpSecurityManager()


# ============================
#  JWT 工具函数
# ============================


def create_jwt_token(account: str, auth_version: str) -> str:
    """
    生成 JWT 令牌

    对应 Java: CommonUtils.generateJwtToken(password, account)

    参数:
        account: 登录账号
        auth_version: 管理员凭据版本

    返回:
        JWT 令牌字符串
    """
    now = datetime.now(UTC)
    payload = {
        "sub": account,
        "av": auth_version,
        "iat": now,
        "exp": now + timedelta(hours=settings.jwt_expire_hours),
    }
    return jwt.encode(payload, settings.effective_jwt_secret, algorithm=JWT_ALGORITHM)


def verify_jwt_token(token: str) -> dict | None:
    """
    验证 JWT 令牌

    对应 Java: AuthInterceptor.preHandle 中的令牌验证逻辑

    参数:
        token: JWT 令牌字符串

    返回:
        解码后的 payload 字典，无效时返回 None
    """
    try:
        payload = jwt.decode(token, settings.effective_jwt_secret, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        logger.warning("JWT 令牌已过期")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning(f"JWT 令牌无效: {e}")
        return None


# ============================
#  FastAPI 认证依赖
# ============================


async def require_auth(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    FastAPI 认证依赖注入

    对应 Java: AuthInterceptor.preHandle()
    将此依赖注入需要认证的路由，自动检查 JWT 令牌和 IP 黑名单。

    使用示例:
        @router.get("/api/data")
        async def get_data(user: dict = Depends(require_auth)):
            ...

    参数:
        request: FastAPI 请求对象

    返回:
        JWT payload 字典

    异常:
        HTTPException 401: 未授权
        HTTPException 403: IP 被封禁
    """
    # 白名单路径放行
    if request.url.path in AUTH_WHITELIST:
        return {}

    # 获取客户端 IP
    from utils.common import get_client_ip

    client_ip = get_client_ip(request)

    # 检查 IP 黑名单
    if ip_security.is_banned(client_ip):
        raise HTTPException(status_code=403, detail=f"IP {client_ip} 已被封禁，请稍后重试")

    # Only accept bearer tokens in headers; URL tokens leak through logs/history.
    token = request.headers.get("Authorization", "")
    if token.startswith("Bearer "):
        token = token[7:]
    else:
        token = ""

    if not token:
        raise HTTPException(status_code=401, detail="未提供认证令牌")

    # 验证 JWT
    payload = verify_jwt_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="认证令牌无效或已过期")

    credentials = await load_admin_credentials(db)
    subject = payload.get("sub")
    auth_version = payload.get("av")
    account_matches = isinstance(subject, str) and constant_time_equal(subject, credentials.account)
    if credentials.is_persisted:
        version_matches = isinstance(auth_version, str) and constant_time_equal(
            auth_version, credentials.auth_version
        )
    else:
        # Tokens issued before credential versioning remain valid until credentials change.
        version_matches = auth_version is None or (
            isinstance(auth_version, str)
            and constant_time_equal(auth_version, credentials.auth_version)
        )
    if not account_matches or not version_matches:
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录")

    return payload
