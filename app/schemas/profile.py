"""Current-user profile response models."""

from datetime import date

from pydantic import BaseModel, ConfigDict, field_serializer

from app.models.user import UserStatus


class PersonalLink(BaseModel):
    label: str
    url: str


class ProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str | None
    account_email: str | None
    department_id: int | None
    department_name: str | None
    user_status: UserStatus
    feishu_open_id: str | None
    nickname: str | None
    avatar: str | None
    signature: str | None
    bio: str | None
    work_phone: str | None
    extension: str | None
    work_email: str | None
    office_location: str | None
    seat: str | None
    expertise_tags: list[str]
    personal_links: list[PersonalLink]
    hidden_public_fields: list[str]
    personal_phone: str | None
    birthday: date | None
    emergency_contact_name: str | None
    emergency_contact_phone: str | None

    @field_serializer("birthday", when_used="json")
    def serialize_birthday(self, value: date | None) -> str | None:
        return value.isoformat() if value is not None else None


__all__: tuple[str, ...] = ("PersonalLink", "ProfileRead")
