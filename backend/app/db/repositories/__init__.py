"""Async, typed repositories — one class per aggregate, holding the real queries."""

from __future__ import annotations

from app.db.repositories.base import BaseRepository
from app.db.repositories.book import BookRepo, PageRepo
from app.db.repositories.continuity import ContinuityStateRepo
from app.db.repositories.defect import DefectRepo
from app.db.repositories.entity import EntityRepo
from app.db.repositories.pref import PrefsRepo
from app.db.repositories.render_job import RenderJobRepo
from app.db.repositories.session import SessionRepo
from app.db.repositories.shot import ShotCacheRepo, ShotRepo, SourceSpanRepo
from app.db.repositories.user import UserRepo

__all__ = [
    "BaseRepository",
    "BookRepo",
    "ContinuityStateRepo",
    "DefectRepo",
    "EntityRepo",
    "PageRepo",
    "PrefsRepo",
    "RenderJobRepo",
    "SessionRepo",
    "ShotCacheRepo",
    "ShotRepo",
    "SourceSpanRepo",
    "UserRepo",
]
