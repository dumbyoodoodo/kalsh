"""Cursor-based pagination helper for Kalshi list endpoints.

# CONFIRMED (docs/API_VERIFICATION.md): list endpoints accept a `cursor`
# query parameter and return a `cursor` field in the response body that is
# empty on the last page -- verified against real, genuinely multi-page
# /markets responses on the live demo API. Some endpoints (e.g. /series as
# currently observed) return everything in a single page with no cursor at
# all; that is handled the same way (loop exits after one page).
"""

from collections.abc import AsyncIterator, Awaitable, Callable

#: A page fetcher: given a cursor (None for the first page), returns
#: (items, next_cursor). next_cursor is None/empty when there are no more pages.
type PageFetcher[T] = Callable[[str | None], Awaitable[tuple[list[T], str | None]]]

#: Safety bound so a malformed or looping cursor can't paginate forever.
DEFAULT_MAX_PAGES = 1000


class PaginationSafetyError(RuntimeError):
    """Raised when pagination would otherwise loop indefinitely."""


async def paginate[T](
    fetch_page: PageFetcher[T], *, max_pages: int = DEFAULT_MAX_PAGES
) -> AsyncIterator[T]:
    """Yield all items across every page of a cursor-paginated endpoint.

    Guards against two ways a buggy or malicious server response could cause
    an infinite loop: exceeding `max_pages`, and the server repeating the
    same cursor it was just given (which would otherwise re-fetch the same
    page forever).
    """
    cursor: str | None = None
    seen_cursors: set[str] = set()
    for _page_number in range(max_pages):
        items, next_cursor = await fetch_page(cursor)
        for item in items:
            yield item
        if not next_cursor:
            return
        if next_cursor in seen_cursors or next_cursor == cursor:
            raise PaginationSafetyError(
                f"pagination cursor repeated ({next_cursor!r}); refusing to loop forever"
            )
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    raise PaginationSafetyError(f"pagination exceeded max_pages={max_pages} without terminating")
