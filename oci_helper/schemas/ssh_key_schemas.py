"""Typed API contracts for persisted SSH public keys."""

from __future__ import annotations

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from core.ssh_keys import normalize_ssh_public_key


class ApiModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


def _normalize_name(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("must not be blank")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError("must not contain control characters")
    return normalized


class GenerateSshKeyPairParams(ApiModel):
    name: str = Field(min_length=1, max_length=128)

    _validate_name = field_validator("name")(_normalize_name)


class AddSshPublicKeyParams(GenerateSshKeyPairParams):
    public_key: str = Field(
        min_length=1,
        max_length=16_384,
        validation_alias=AliasChoices("publicKey", "public_key"),
        serialization_alias="publicKey",
    )

    @field_validator("public_key")
    @classmethod
    def validate_public_key(cls, value: str) -> str:
        return normalize_ssh_public_key(value)


class RenameSshPublicKeyParams(GenerateSshKeyPairParams):
    id: str = Field(min_length=1, max_length=64)


class SshPublicKeyRsp(ApiModel):
    id: str
    name: str
    public_key: str = Field(serialization_alias="publicKey")
    fingerprint: str
    create_time: str = Field(serialization_alias="createTime")
    update_time: str = Field(serialization_alias="updateTime")
