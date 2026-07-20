import pytest

from kalshi_weather.kalshi.pagination import PaginationSafetyError, paginate


async def test_paginate_single_page() -> None:
    async def fetch_page(cursor: str | None) -> tuple[list[int], str | None]:
        assert cursor is None
        return [1, 2, 3], None

    items = [item async for item in paginate(fetch_page)]
    assert items == [1, 2, 3]


async def test_paginate_multiple_pages() -> None:
    pages = {None: ([1, 2], "page2"), "page2": ([3, 4], "page3"), "page3": ([5], None)}

    async def fetch_page(cursor: str | None) -> tuple[list[int], str | None]:
        return pages[cursor]

    items = [item async for item in paginate(fetch_page)]
    assert items == [1, 2, 3, 4, 5]


async def test_paginate_empty_first_page() -> None:
    async def fetch_page(cursor: str | None) -> tuple[list[int], str | None]:
        return [], None

    items = [item async for item in paginate(fetch_page)]
    assert items == []


async def test_paginate_repeated_cursor_raises_instead_of_looping_forever() -> None:
    async def fetch_page(cursor: str | None) -> tuple[list[int], str | None]:
        # server bug: always returns the same next-cursor, regardless of input
        return [1], "stuck-cursor"

    with pytest.raises(PaginationSafetyError):
        [item async for item in paginate(fetch_page)]


async def test_paginate_max_pages_safety_limit() -> None:
    call_count = 0

    async def fetch_page(cursor: str | None) -> tuple[list[int], str | None]:
        nonlocal call_count
        call_count += 1
        # a distinct cursor every time, so the repeated-cursor guard doesn't trip first
        return [call_count], f"cursor-{call_count}"

    with pytest.raises(PaginationSafetyError):
        [item async for item in paginate(fetch_page, max_pages=5)]

    assert call_count == 5
