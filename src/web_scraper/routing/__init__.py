"""Route statistics and adaptive route selection.

A Site Profile already declares several independent routes per level (a primary
plus alternatives). What was missing is memory: which of those doors actually
opens, how often, how fast, and at what price. This package supplies that memory
and the selection policy built on it.

Two pieces, deliberately separate:

* :mod:`web_scraper.routing.stats` — a durable per-(domain, url_class, route,
  level) record of what happened.
* :mod:`web_scraper.routing.router` — a transparent, testable policy that ranks
  routes from those records. No machine learning: an EWMA plus a Wilson lower
  bound answers the question, and both can be explained to an operator.
"""

from web_scraper.routing.stats import RouteKey, RouteStats, RouteStatsStore, wilson_lower_bound

__all__ = ["RouteKey", "RouteStats", "RouteStatsStore", "wilson_lower_bound"]
