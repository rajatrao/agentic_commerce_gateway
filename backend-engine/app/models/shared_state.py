"""State that every replica must see: service-JWT replay guard and the rendered llm.txt cache."""

from datetime import datetime

from sqlalchemy import Column, String, Text
from sqlmodel import Field, SQLModel

from app.models.base import tz_column, utcnow


class ServiceTokenJti(SQLModel, table=True):
    """`jti` of every accepted frontend-ui service JWT, kept until the token expires."""

    __tablename__ = "service_token_jtis"

    jti: str = Field(sa_column=Column(String(64), primary_key=True))
    expires_at: datetime = Field(sa_column=tz_column(index=True))


class LlmTxtCacheEntry(SQLModel, table=True):
    __tablename__ = "llm_txt_cache"

    shop_domain: str = Field(sa_column=Column(String, primary_key=True))
    body: str = Field(sa_column=Column(Text, nullable=False))
    generated_at: datetime = Field(default_factory=utcnow, sa_column=tz_column())
