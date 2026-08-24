"""Offline, leakage-audited research evaluation for Where Papers Go.

This package intentionally depends only on the Python standard library and
never imports the production Search/LLM clients.  It is safe to use for a
frozen, reproducible paper benchmark.
"""

from .types import Query, Run, ScoredDocument, VenueDocument

__all__ = ["Query", "Run", "ScoredDocument", "VenueDocument"]
