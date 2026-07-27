"""Published Kalshi fee tables, transcribed verbatim from the CFTC-filed Kalshi
Fee Schedule (cftc.gov, rule091222kexdcm003, eff. 2022-09-12).

Each row is ``(price_cents, fee_for_1_contract_cents, fee_for_100_contracts_cents)``.
These are the exchange's own published tables, used as ground-truth for the
authoritative fee model. Do NOT edit to match the implementation -- edit only to
match the official document.
"""

#: General schedule -- "General Trading Fees Table" (0.07 coefficient).
CFTC_GENERAL: list[tuple[int, int, int]] = [
    (1, 1, 7),
    (5, 1, 34),
    (10, 1, 63),
    (15, 1, 90),
    (20, 2, 112),
    (25, 2, 132),
    (30, 2, 147),
    (35, 2, 160),
    (40, 2, 168),
    (45, 2, 174),
    (50, 2, 175),
    (55, 2, 174),
    (60, 2, 168),
    (65, 2, 160),
    (70, 2, 147),
    (75, 2, 132),
    (80, 2, 112),
    (85, 1, 90),
    (90, 1, 63),
    (95, 1, 34),
    (99, 1, 7),
]

#: S&P500 / NASDAQ-100 special schedule (0.035 coefficient). Every 1-contract
#: fee rounds up to $0.01.
CFTC_SPECIAL: list[tuple[int, int, int]] = [
    (1, 1, 4),
    (5, 1, 17),
    (10, 1, 32),
    (15, 1, 45),
    (20, 1, 56),
    (25, 1, 66),
    (30, 1, 74),
    (35, 1, 80),
    (40, 1, 84),
    (45, 1, 87),
    (50, 1, 88),
    (55, 1, 87),
    (60, 1, 84),
    (65, 1, 80),
    (70, 1, 74),
    (75, 1, 66),
    (80, 1, 56),
    (85, 1, 45),
    (90, 1, 32),
    (95, 1, 17),
    (99, 1, 4),
]
