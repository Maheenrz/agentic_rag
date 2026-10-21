"""
models.py
---------
All Pydantic request models. Keeping them here (instead of scattered across
routers) makes the API surface easy to scan and lets OpenAPI generate a
consistent schema.
"""
from typing import Optional


from pydantic import BaseModel, EmailStr, Field, field_validator



# ---------------- auth ----------------
class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    org_name: Optional[str] = Field(default=None, max_length=100)


class LoginRequest(BaseModel):
    """identifier = username OR email."""
    identifier: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=72)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=10, max_length=512)
    password: str = Field(min_length=8, max_length=72)


class AcceptInviteRequest(BaseModel):
    token: str = Field(min_length=10, max_length=512)
    password: str = Field(min_length=8, max_length=72)


# ---------------- admin: users & groups ----------------
class CreateUserRequest(BaseModel):
    """Admin-created users are invited: no password here, they set their own."""
    username: str = Field(min_length=3, max_length=64)
    email: EmailStr
    role: str = Field(default="member", pattern="^(admin|member)$")


class GroupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Group name can't be empty")
        return v.strip()


class GroupMemberRequest(BaseModel):
    user_id: str


# ---------------- collections ----------------
class CollectionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=240)
    visibility: str = Field(default="restricted", pattern="^(org|restricted)$")

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Collection name can't be empty")
        return v.strip()

    @field_validator("description")
    @classmethod
    def _description_trim(cls, v: str) -> str:
        return v.strip()


class AclRequest(BaseModel):
    user_ids: list[str] = Field(default_factory=list)
    group_ids: list[str] = Field(default_factory=list)
    visibility: Optional[str] = Field(default=None, pattern="^(org|restricted)$")


# ---------------- threads / chat ----------------
class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=80)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1)
    force_offline: bool = False        # ignored; kept so old clients don't break
    top_k: int = Field(default=4, ge=1, le=20)
    collection_ids: Optional[list[str]] = None


class GuestChatRequest(BaseModel):
    question: str
    collection_id: str


# ---------------- feedback ----------------
class FeedbackRequest(BaseModel):
    rating: str = Field(pattern="^(up|down)$")
    reason: str = Field(default="", max_length=40)
    comment: str = Field(default="", max_length=1000)