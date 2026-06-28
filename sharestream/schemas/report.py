from __future__ import annotations

from pydantic import BaseModel, Field


class ReportRequest(BaseModel):
    reporter_name: str = Field(..., description="Reporter Name or Company")
    reporter_email: str = Field(..., description="Reporter Email")
    reporter_website: str = Field("", description="Reporter Website (Optional)")
    report_type: str = Field(..., description="Reason for the report")
    reported_links: str = Field(..., description="Reported Links")
    description: str = Field(..., description="Additional details about the report")
