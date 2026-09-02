"""
异常定义模块

对应 Java 原项目：com.yohann.ocihelper.exception.OciException
功能：定义自定义业务异常类，用于统一错误处理。
"""


class OciException(Exception):
    """
    OCI 业务异常类

    对应 Java 原项目中的 OciException。
    用于在业务逻辑中抛出可预期的错误，由全局异常处理器捕获并返回标准错误响应。

    属性:
        code (int): 错误码，-1 表示通用业务错误，401 表示未授权
        message (str): 错误描述信息
    """

    def __init__(self, code: int = -1, message: str = "请求失败"):
        """
        初始化 OCI 异常

        参数:
            code: 错误码，默认 -1
            message: 错误描述信息，默认 "请求失败"
        """
        self.code = code
        self.message = message
        super().__init__(self.message)

    def __repr__(self) -> str:
        return f"OciException(code={self.code}, message='{self.message}')"
