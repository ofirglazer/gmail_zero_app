"""
SearchService — parameterised message search for the Search view.

This service is a thin delegation wrapper over MessageRepository.search()
and MessageRepository.count_search().  It exists to give the presentation
layer a stable, named entry point without coupling routes directly to
repository types.

Design note:
    SearchService is deliberately minimal.  All filter logic lives in
    MessageFilter (DTO) and MessageRepository (persistence).  This service
    adds no transformation beyond invoking both repository methods and
    returning the paired (results, total_count) tuple.

    If search requirements grow substantially (e.g. multi-index federation,
    relevance scoring), this is the correct place to add that logic without
    touching the repository.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from domain.models.message import Message
    from infrastructure.persistence.repositories.message_repository import (
        MessageFilter,
        MessageRepository,
    )


class SearchService:
    """
    Application service for filtered message search.

    Args:
        msg_repo: Message repository providing ``search()`` and ``count_search()``.
    """

    def __init__(self, msg_repo: MessageRepository) -> None:
        self._msg_repo = msg_repo

    def search(self, filters: MessageFilter) -> tuple[list[Message], int]:
        """
        Return matching messages and the unpaginated total count.

        Two separate repository calls are made so the presentation layer can
        render pagination controls (total pages, current offset) without
        loading all matching rows.

        Args:
            filters: MessageFilter DTO specifying all active search constraints.

        Returns:
            A ``(results, total_count)`` tuple where:
            - ``results``     is the paginated list of matching Message entities.
            - ``total_count`` is the total matching count ignoring pagination.
        """
        results = self._msg_repo.search(filters)
        total_count = self._msg_repo.count_search(filters)
        return results, total_count
