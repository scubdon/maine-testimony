"""Typed models for the Maine Legislature testimony JSON endpoints.

Field names mirror the upstream JSON (camelCase) via aliases so that raw
responses validate directly, while Python code uses snake_case.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1
    )
}
# e.g. "Tue Feb 4 00:00:00 EST 2025" (Java Date.toString() format)
_JAVA_DATE = re.compile(r"^\w{3}\s+(\w{3})\s+(\d{1,2})\s+[\d:]+\s+\w+\s+(\d{4})$")


def parse_presented_date(value: str) -> date:
    """Parse the upstream ``presentedDate`` string into a ``date``.

    The server emits Java ``Date.toString()`` output with a timezone
    abbreviation (EST/EDT). Only the calendar date is meaningful.
    """
    m = _JAVA_DATE.match(value.strip())
    if not m:
        # Fall back to ISO in case the backend ever changes format.
        return datetime.fromisoformat(value.strip()[:10]).date()
    mon, day, year = m.groups()
    return date(int(year), _MONTHS[mon], int(day))


class _Base(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore", frozen=True)


class Session(_Base):
    session_number: int = Field(alias="sessionNumber")
    session_years: str = Field(alias="sessionYears")


class Committee(_Base):
    code: str = Field(alias="committeeCode")
    committee_id: int = Field(alias="committeeId")
    name: str = Field(alias="committeeName")
    date_count: int = Field(alias="dateCount", default=0)


class HearingDate(_Base):
    presented_date: date = Field(alias="presentedDate")

    @field_validator("presented_date", mode="before")
    @classmethod
    def _parse(cls, v: object) -> object:
        return parse_presented_date(v) if isinstance(v, str) else v


class TestimonyRecord(_Base):
    """One written testimony submission, as listed for a committee hearing date."""

    __test__ = False  # stop pytest from trying to collect this class

    id: int
    first_name: str = Field(alias="firstName", default="")
    last_name: str = Field(alias="lastName", default="")
    organization: str = Field(default="")
    ld: int | None = None
    paper_number: str = Field(alias="paperNumber", default="")
    title: str = Field(default="")
    session_number: int = Field(alias="sessionNumber")
    policy_area: str = Field(alias="policyArea", default="")
    topic: str = Field(default="")

    @field_validator("first_name", "last_name", "organization", "title", "topic", mode="before")
    @classmethod
    def _clean_str(cls, v: object) -> object:
        return " ".join(v.split()) if isinstance(v, str) else ("" if v is None else v)

    @field_validator("ld", mode="before")
    @classmethod
    def _ld(cls, v: object) -> object:
        # Unprinted bills / budget items come through as 0.
        return None if v in (0, "0", "", None) else v
