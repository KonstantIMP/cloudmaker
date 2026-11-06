from uuid import UUID

from pydantic import BaseModel, Field


class ImageCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    path: str = Field(min_length=1)
    description: str = ""


class VMCreate(BaseModel):
    image_id: UUID
    name: str = Field(default="vm", min_length=1, max_length=80)
    vcpus: int = Field(default=1, ge=1)
    memory_mb: int = Field(default=512, ge=256)
    disk_gb: int = Field(default=8, ge=1)
    hostname: str | None = Field(default=None, max_length=63, pattern=r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?$")
    ssh_keys: list[str] = Field(default_factory=list)
    user_data: str = ""
