"""Deterministic Kalshi paper-trading / execution simulator.

SIMULATION ONLY. This package evaluates *hypothetical* orders against synthetic
fixtures or explicitly-supplied historical production market data. It contains
NO order-submission path -- no real orders, no demo orders, no authenticated
exchange requests -- and does not import or consume H0019 (or any other)
prediction artifacts. See docs/adr/0015-execution-simulator.md.

Money is integer cents throughout (domain/money.py); no binary float touches
accounting. Every portfolio state is reproducible by replaying an append-only
ledger, and every run is reproducible from its immutable artifact directory.
"""

SIMULATION_ONLY_BANNER = "SIMULATION ONLY -- NO EXCHANGE ORDERS WILL BE SUBMITTED"
