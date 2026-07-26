"""Formal modeling experiments on frozen canonical datasets.

The first experiment (H0018) evaluates whether point-in-time weather + contract
features improve on the Kalshi market-implied probability for settled weather
markets, at a fixed decision horizon, scored by Brier score. Numpy-only models
(no scikit-learn) keep the first baseline simple and dependency-light, per the
project's model-progression rule (CLAUDE.md).
"""
