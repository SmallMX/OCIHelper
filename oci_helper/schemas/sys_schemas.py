"""System API contracts for login, administrator settings and the dashboard."""

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ApiModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class LoginParams(ApiModel):
    account: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class UpdateSysCfgParams(ApiModel):
    tg_chat_id: str | None = Field(default=None, alias="tgChatId", max_length=128)
    tg_bot_token: str | None = Field(default=None, alias="tgBotToken", max_length=256)

    @model_validator(mode="after")
    def validate_pair(self):
        if bool(self.tg_bot_token) != bool(self.tg_chat_id):
            raise ValueError("tgBotToken and tgChatId must be provided together")
        return self


class UpdateAdminCredentialsParams(ApiModel):
    account: str = Field(min_length=1, max_length=128)
    current_password: str = Field(alias="currentPassword", min_length=1, max_length=256)
    new_password: str | None = Field(
        default=None,
        alias="newPassword",
        min_length=12,
        max_length=256,
    )

    @field_validator("account")
    @classmethod
    def normalize_account(cls, value: str) -> str:
        account = value.strip()
        if not account:
            raise ValueError("管理员用户名不能为空")
        return account


class SendMsgParams(ApiModel):
    message: str = Field(min_length=1, max_length=4096)


class GetTaskLogsParams(ApiModel):
    lines: int = Field(default=300, ge=1, le=2000)


class LoginRsp(ApiModel):
    token: str
    current_version: str | None = Field(default=None, alias="currentVersion")


class GetSysCfgRsp(ApiModel):
    admin_account: str = Field(alias="adminAccount")
    tg_chat_id: str | None = Field(default=None, alias="tgChatId")
    tg_bot_configured: bool = Field(default=False, alias="tgBotConfigured")


class GetGlanceRsp(ApiModel):
    users: str | None = None
    tasks: str | None = None
    regions: str | None = None
    days: str | None = None
    current_version: str | None = Field(default=None, alias="currentVersion")


class GetTaskLogsRsp(ApiModel):
    lines: list[str] = Field(default_factory=list)
    line_count: int = Field(default=0, alias="lineCount")
    truncated: bool = False
    updated_at: str | None = Field(default=None, alias="updatedAt")
