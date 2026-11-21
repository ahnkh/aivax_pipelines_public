"""`/v1/filter/testrule` HTTP 요청 계약."""

from typing import Literal

from pydantic import BaseModel, Field


class FilterRuleTestAttachmentItem(BaseModel):
    id: str = ""
    name: str = ""
    mime_type: str = ""
    size: int = 0


class FilterRuleTestSubjectItem(BaseModel):
    email: str = ""
    ai_service_id: str | int = 0


class FilterRuleTestRegexRuleItem(BaseModel):
    id: str
    name: str
    rule: str
    action: Literal["accept", "masking", "block"]
    scope: Literal["user", "service", "group", "default"] = "default"
    category: str | None = None
    subject_id: str = ""


class FilterRuleTestMultimodalRuleItem(FilterRuleTestRegexRuleItem):
    targets: str = "file"
    regex_flag: int = 0


class FilterRuleTestSlmRuleItem(BaseModel):
    id: str
    name: str
    action: Literal["accept", "masking", "block"]
    detection_kind: str = ""
    detection_type: str | None = None
    scope: Literal["user", "service", "group", "default"] = "default"
    category: str | None = None
    subject_id: str = ""


class FilterRuleTestSettingItem(BaseModel):
    skey: str
    svalue: str


class FilterRuleTestRegexPolicyItem(BaseModel):
    group_members: dict[str, list[str]] = Field(default_factory=dict)
    rules: list[FilterRuleTestRegexRuleItem] = Field(default_factory=list)


class FilterRuleTestMultimodalPolicyItem(BaseModel):
    group_members: dict[str, list[str]] = Field(default_factory=dict)
    rules: list[FilterRuleTestMultimodalRuleItem] = Field(default_factory=list)
    settings: list[FilterRuleTestSettingItem] = Field(default_factory=list)


class FilterRuleTestSlmPolicyItem(BaseModel):
    group_members: dict[str, list[str]] = Field(default_factory=dict)
    rules: list[FilterRuleTestSlmRuleItem] = Field(default_factory=list)


class FilterRuleTestPoliciesItem(BaseModel):
    regex: FilterRuleTestRegexPolicyItem | None = None
    multimodal: FilterRuleTestMultimodalPolicyItem | None = None
    slm: FilterRuleTestSlmPolicyItem | None = None


class FilterRuleTestItem(BaseModel):
    prompt: str = Field(default="", description="입력 프롬프트")
    attachments: list[FilterRuleTestAttachmentItem] = Field(default_factory=list)
    subject: FilterRuleTestSubjectItem = Field(
        default_factory=FilterRuleTestSubjectItem,
    )
    policies: FilterRuleTestPoliciesItem
