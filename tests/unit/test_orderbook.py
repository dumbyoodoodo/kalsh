import pytest
from hypothesis import given
from hypothesis import strategies as st

from kalshi_weather.kalshi.orderbook import (
    FULL_RANGE_CENTS,
    InvalidPriceLevelError,
    reconstruct_best_quote,
)


def test_full_book_both_sides() -> None:
    yes_bids = [(45, 100), (44, 50), (40, 200)]
    no_bids = [(52, 80), (50, 20)]

    quote = reconstruct_best_quote(yes_bids, no_bids)

    assert quote.best_yes_bid_cents == 45
    assert quote.best_no_bid_cents == 52
    # best_yes_ask = 100 - best_no_bid
    assert quote.best_yes_ask_cents == 48
    # best_no_ask = 100 - best_yes_bid
    assert quote.best_no_ask_cents == 55
    assert quote.yes_spread_cents == 3
    assert quote.no_spread_cents == 3


def test_empty_book_both_sides() -> None:
    quote = reconstruct_best_quote([], [])

    assert quote.best_yes_bid_cents is None
    assert quote.best_yes_ask_cents is None
    assert quote.best_no_bid_cents is None
    assert quote.best_no_ask_cents is None
    assert quote.yes_spread_cents is None
    assert quote.no_spread_cents is None


def test_empty_yes_side_only() -> None:
    """No resting YES bids: yes_bid is None, and no_ask (which depends on yes_bid) is None too."""
    quote = reconstruct_best_quote([], [(60, 10)])

    assert quote.best_yes_bid_cents is None
    assert quote.best_no_bid_cents == 60
    assert quote.best_yes_ask_cents == 40
    assert quote.best_no_ask_cents is None


def test_empty_no_side_only() -> None:
    quote = reconstruct_best_quote([(30, 5)], [])

    assert quote.best_yes_bid_cents == 30
    assert quote.best_no_bid_cents is None
    assert quote.best_yes_ask_cents is None
    assert quote.best_no_ask_cents == 70


def test_boundary_prices_1_and_99_cents() -> None:
    quote = reconstruct_best_quote([(1, 1)], [(99, 1)])

    assert quote.best_yes_bid_cents == 1
    assert quote.best_no_bid_cents == 99
    assert quote.best_yes_ask_cents == 1
    assert quote.best_no_ask_cents == 99


def test_single_level_each_side() -> None:
    quote = reconstruct_best_quote([(50, 1)], [(50, 1)])
    assert quote.best_yes_bid_cents == 50
    assert quote.best_no_bid_cents == 50
    assert quote.best_yes_ask_cents == 50
    assert quote.best_no_ask_cents == 50
    assert quote.yes_spread_cents == 0


@pytest.mark.parametrize("bad_price", [0, 100, -1, 200])
def test_out_of_range_price_level_is_skipped_not_rejected(bad_price: int) -> None:
    # Regression: Kalshi intermittently returns a price-0 level on some
    # long-horizon markets. The whole book must NOT be discarded -- the
    # out-of-range level is excluded from quote derivation and the valid
    # levels still produce a quote.
    quote = reconstruct_best_quote([(bad_price, 5), (40, 10)], [])
    assert quote.best_yes_bid_cents == 40  # valid level survives
    assert quote.best_no_ask_cents == 100 - 40


def test_side_with_only_out_of_range_levels_has_no_bid() -> None:
    # A side whose only level is out-of-range reconstructs as "no bid",
    # never a bogus zero bid.
    quote = reconstruct_best_quote([(0, 5)], [(30, 2)])
    assert quote.best_yes_bid_cents is None
    assert quote.best_no_ask_cents is None  # depends on the (absent) yes bid
    assert quote.best_no_bid_cents == 30


def test_rejects_negative_quantity() -> None:
    # Negative quantity is genuine corruption and still fails closed.
    with pytest.raises(InvalidPriceLevelError):
        reconstruct_best_quote([(50, -1)], [])


@given(
    yes_bids=st.lists(
        st.tuples(st.integers(min_value=1, max_value=99), st.integers(min_value=0, max_value=1000)),
        max_size=10,
    ),
    no_bids=st.lists(
        st.tuples(st.integers(min_value=1, max_value=99), st.integers(min_value=0, max_value=1000)),
        max_size=10,
    ),
)
def test_complement_identity_holds(
    yes_bids: list[tuple[int, int]], no_bids: list[tuple[int, int]]
) -> None:
    """DATA_MODEL.md invariant: yes_ask = 100 - no_bid, no_ask = 100 - yes_bid."""
    quote = reconstruct_best_quote(yes_bids, no_bids)

    if no_bids:
        assert quote.best_yes_ask_cents == FULL_RANGE_CENTS - quote.best_no_bid_cents
    else:
        assert quote.best_yes_ask_cents is None

    if yes_bids:
        assert quote.best_no_ask_cents == FULL_RANGE_CENTS - quote.best_yes_bid_cents
    else:
        assert quote.best_no_ask_cents is None
