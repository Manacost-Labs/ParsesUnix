"""Persistence helpers: saved responses now; snapshots and queues later."""

from web_scraper.storage.fixtures import SavedResponse, iter_saved_responses, load_saved_response

__all__ = ["SavedResponse", "iter_saved_responses", "load_saved_response"]
