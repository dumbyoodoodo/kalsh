"""Automated settlement resolution: Kalshi market metadata -> SettlementSpec.

Replaces the Milestone 4 config-file market mapping with a deterministic
parser over structured Kalshi settlement-source citations and rules text; the
config file survives as the manual override layer. See
docs/adr/0005-settlement-resolution.md.
"""
