from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class AllowlistEntry(BaseModel):
    phone_number: str
    label: str = ""


class AllowlistResponse(BaseModel):
    id: int
    phone_number: str
    label: str
    created_at: str


class MessageResponse(BaseModel):
    id: int
    sender: str
    receiver: str
    content: str
    channel: str
    direction: str
    timestamp: str


class SettingsUpdate(BaseModel):
    system_prompt: Optional[str] = None


class SettingsResponse(BaseModel):
    system_prompt: str
