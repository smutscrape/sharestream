from __future__ import annotations

from pydantic import BaseModel, Field


class DMCARequest(BaseModel):
    requester_name: str = Field(..., description="Requester Name or Company")
    requester_email: str = Field(..., description="Requester Email")
    requester_website: str = Field("", description="Requester Website")
    infringing_links: str = Field(..., description="Allegedly Infringing Links")
