"""Field resolvers over the existing Kinora repositories.

Each module resolves one slice of the domain (books, shots, scenes, sessions,
canon, viewer, mutations) by reading the same repositories the REST routes use
(``app/db/repositories/*``) through the wired
:class:`~app.composition.Container` on the request context. Reads enforce the
API key's ownership boundary (``ctx.user_id``) and scopes; relationship
traversals dataloader-batch to avoid N+1.
"""

from __future__ import annotations
