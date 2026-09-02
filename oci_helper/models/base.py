"""
SQLAlchemy 基础模型定义

功能：提供所有 ORM 模型的基类 (DeclarativeBase)。
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """
    SQLAlchemy 声明式基类

    所有数据库模型都继承此基类。
    对应 Java 原项目中 MyBatis-Plus 的 @TableName 注解功能。
    """

    pass
