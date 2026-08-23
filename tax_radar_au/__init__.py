"""Synthetic, provenance-first tax-change impact queue."""

from .monitor import compare, validate_review, write_queue

__all__ = ["compare", "validate_review", "write_queue"]
