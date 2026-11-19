"""Slice-2 posters: approval-gated publishing to YouTube, TikTok,
Instagram and Facebook. All posting goes through posters.gate.post_clip
— never call an adapter's post() directly from the UI or CLI."""
from . import facebook, instagram, tiktok, youtube  # noqa: F401
from .gate import configured_map, post_clip  # noqa: F401

__all__ = ["youtube", "tiktok", "instagram", "facebook", "post_clip", "configured_map"]
