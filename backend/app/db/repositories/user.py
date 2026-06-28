"""Repository for user accounts (auth, wired in a later phase)."""

from __future__ import annotations

from sqlalchemy import select

from app.db.base import new_id
from app.db.models.user import User
from app.db.repositories.base import BaseRepository


class UserRepo(BaseRepository):
    """Create and look up users."""

    async def create(
        self, *, email: str, hashed_password: str, user_id: str | None = None
    ) -> User:
        """Insert a new user."""
        user = User(id=user_id or new_id(), email=email, hashed_password=hashed_password)
        self.session.add(user)
        await self.session.flush()
        return user

    async def get(self, user_id: str) -> User | None:
        """Fetch a user by id."""
        return await self.session.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        """Fetch a user by their unique email (the auth lookup)."""
        stmt = select(User).where(User.email == email)
        return (await self.session.execute(stmt)).scalars().first()

    async def set_password(self, user_id: str, hashed_password: str) -> User | None:
        """Update a user's stored password hash (change / reset / transparent rehash)."""
        user = await self.get(user_id)
        if user is None:
            return None
        user.hashed_password = hashed_password
        await self.session.flush()
        return user
