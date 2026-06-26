"""Library domain — the Retrieval & Understanding role (Agent 05, §5.1).

Book info retrieval + indexing and the supplementary metadata that lets the
shelf (and, later, the illustrators) reason about a title: the curated
public-domain :mod:`~app.library.catalog` manifest and the HD
:mod:`~app.library.covers` sourcing/fallback logic. Kept framework-free and
fully unit-tested so the seeder + cover scripts stay thin orchestration.
"""
