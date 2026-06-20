from __future__ import annotations

from pydantic import BaseModel, Field

from sharestream.config import DEFAULT_RESOLUTION
from sharestream.db.models import Resolution


class ShareVideoRequest(BaseModel):
    video_name: str
    stash_video_id: int
    days_valid: int = 7
    resolution: Resolution = Field(default=Resolution[DEFAULT_RESOLUTION], description="Streaming resolution")
    password: str | None = None
    show_in_gallery: bool = False
    custom_share_id: str | None = None
    embed_mode: str | None = None
    clear_password: bool = False


class ShareTagRequest(BaseModel):
    tag_name: str
    tag_id: str
    days_valid: int = 7
    resolution: Resolution = Field(default=Resolution[DEFAULT_RESOLUTION], description="Streaming resolution")
    password: str | None = None
    show_in_gallery: bool = False
    custom_share_id: str | None = None
    embed_mode: str | None = None
    default_sort: str | None = None  # date|title|hits|rating|duration|random | None(=config default)
    # Whether limit_to_tag applies to this share's own surfaces when it's NOT a
    # featured public share. Defaults True; only meaningful when LIMIT_TO_TAG is set.
    apply_limit_tag: bool = True
    # Experimental masonry gallery layout (native aspect ratios) for this share.
    gallery_mode: bool = False
    clear_password: bool = False


class ReorderTagsRequest(BaseModel):
    order: list[str]  # share_ids in the desired display order
