from datetime import datetime
from typing import Optional


class FormFlow:
    def __init__(
        self,
        flow_id: int,
        name: str,
        flow_type: str,
        target_channel_id: int,
        created_at: Optional[datetime] = None,
    ):
        self.id = flow_id
        self.name = name
        self.type = flow_type
        self.target_channel_id = target_channel_id
        self.created_at = created_at

    def __str__(self) -> str:
        return f"{self.name} ({self.type})"


class FormQuestion:
    def __init__(
        self,
        question_id: int,
        flow_id: int,
        question_text: str,
        position: int,
        placeholder_text: Optional[str] = None,
        required: bool = False,
    ):
        self.id = question_id
        self.flow_id = flow_id
        self.question_text = question_text
        self.position = position
        self.placeholder_text = placeholder_text
        self.required = required

    def __str__(self) -> str:
        return self.question_text
