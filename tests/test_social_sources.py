"""Reddit and X: what is collected, and what is refused.

Neither adapter is live verified. Reddit blocks this project's user agent, so
its API documentation could not be read from the vendor and no call has been
made; X is exercised through a scripted gateway. What these tests pin down is
the part that is ours: the requests we build, the way answers are normalised,
and — mostly — the things we decline to do.
"""

from __future__ import annotations

import json
import sys
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.contracts import Result, Verdict
from web_scraper.sources.contract import (
    Checkpoint,
    CollectionRoute,
    EntityKind,
    Platform,
    SearchWindow,
    SocialAccounting,
    ThreadCompleteness,
    dedupe,
    external_links_of,
)
from web_scraper.sources.reddit import (
    RateLimited,
    RedditAccessError,
    RedditAdapter,
    collect_incrementally,
)
from web_scraper.sources.x import XApiConfig, XSearchAdapter


# --------------------------------------------------------------------------
# shared contract
# --------------------------------------------------------------------------
class ContractTests(unittest.TestCase):
    def entity(self, post_id, *, text="hi", platform=Platform.REDDIT, links=()):
        from web_scraper.sources.contract import Provenance, SocialEntity

        return SocialEntity(
            platform=platform,
            platform_post_id=post_id,
            kind=EntityKind.POST,
            created_at=1000.0,
            text=text,
            external_links=tuple(links),
            engagement_collected_at=2.0,
            provenance=Provenance(
                platform=platform,
                route=CollectionRoute.OFFICIAL_API,
                source_url="https://x/1",
                collected_at=1.0,
            ),
        )

    def test_dedup_is_by_platform_id_not_by_text(self) -> None:
        # Two people writing the same word are two posts; one post edited for a
        # typo is one post.
        same_text = [self.entity("a", text="this"), self.entity("b", text="this")]
        self.assertEqual(len(dedupe(same_text)), 2)

        edited = [self.entity("a", text="teh"), self.entity("a", text="the")]
        self.assertEqual(len(dedupe(edited)), 1)

    def test_identity_is_namespaced_by_platform(self) -> None:
        reddit = self.entity("123", platform=Platform.REDDIT)
        x = self.entity("123", platform=Platform.X)
        self.assertNotEqual(reddit.natural_key, x.natural_key)
        self.assertEqual(len(dedupe([reddit, x])), 2)

    def test_a_thread_with_more_nodes_is_not_complete(self) -> None:
        self.assertFalse(ThreadCompleteness(entities_loaded=50, more_nodes=3).is_complete)
        self.assertTrue(ThreadCompleteness(entities_loaded=50).is_complete)

    def test_completeness_distinguishes_their_paging_from_our_ceiling(self) -> None:
        # An operator can act on one of these and not the other.
        theirs = ThreadCompleteness(more_nodes=5)
        ours = ThreadCompleteness(depth_limit_hit=True)
        self.assertIn("platform did not send", theirs.reason)
        self.assertIn("our depth ceiling", ours.reason)

    def test_accounting_must_balance(self) -> None:
        self.assertTrue(SocialAccounting(discovered=10, resolved=7, unavailable=3).is_complete)
        leaky = SocialAccounting(discovered=10, resolved=7)
        self.assertFalse(leaky.is_complete)
        self.assertEqual(leaky.unaccounted, 3)

    def test_link_following_is_bounded_and_domain_scoped(self) -> None:
        # Unbounded following turns one query into a crawl of the whole internet.
        entity = self.entity(
            "a",
            links=(
                "https://good.example/1",
                "https://good.example/2",
                "https://good.example/3",
                "https://elsewhere.example/x",
            ),
        )
        links = external_links_of([entity], allowed_domains=["good.example"], max_per_entity=2)
        self.assertEqual(links, ["https://good.example/1", "https://good.example/2"])

    def test_content_and_engagement_freshness_are_separate(self) -> None:
        entity = self.entity("a")
        self.assertIsNotNone(entity.provenance.collected_at)
        self.assertIsNotNone(entity.engagement_collected_at)
        payload = entity.to_dict()
        self.assertIn("engagement_collected_at", payload)
        self.assertIn("collected_at", payload["provenance"])


# --------------------------------------------------------------------------
# Reddit
# --------------------------------------------------------------------------
class FakeReddit:
    """Scripted OAuth responses, recording every request."""

    def __init__(self, payloads, *, headers=None, status=200):
        self._payloads = list(payloads)
        self._headers = headers or {}
        self._status = status
        self.requests: list = []

    def urlopen(self, request, timeout=None):
        self.requests.append(request)
        outer = self
        if request.full_url.endswith("access_token"):
            body = json.dumps({"access_token": "T", "expires_in": 3600}).encode()
        else:
            if outer._status != 200:
                raise urllib.error.HTTPError(
                    request.full_url, outer._status, "no", outer._headers, None
                )
            body = json.dumps(
                outer._payloads.pop(0) if outer._payloads else {"data": {"children": []}}
            ).encode()

        class Response:
            status = 200
            headers = outer._headers

            def read(self, amount=None):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        return Response()


def listing(*posts):
    return {
        "data": {
            "after": "t3_next",
            "children": [{"kind": "t3", "data": p} for p in posts],
        }
    }


def post(post_id="abc", **kw):
    base = {
        "id": post_id,
        "title": "A title",
        "selftext": "body text",
        "author": "someone",
        "subreddit": "python",
        "created_utc": 1000.0,
        "score": 42,
        "num_comments": 7,
        "permalink": f"/r/python/comments/{post_id}/x/",
    }
    base.update(kw)
    return base


class RedditAccessTests(unittest.TestCase):
    def test_it_refuses_to_run_without_credentials(self) -> None:
        # And there is no unauthenticated fallback to slip into.
        adapter = RedditAdapter(client_id="", client_secret="")
        with self.assertRaises(RedditAccessError) as caught:
            adapter.search(SearchWindow(query="x"))
        self.assertIn("no unauthenticated fallback", str(caught.exception))

    def test_it_identifies_itself_honestly(self) -> None:
        # Imitating a browser here would be pretending not to be an application.
        adapter = RedditAdapter(client_id="a", client_secret="b")
        self.assertIn("ParsesUnix", adapter.user_agent)
        self.assertNotIn("Mozilla", adapter.user_agent)

    def test_a_refusal_is_reported_not_worked_around(self) -> None:
        http = FakeReddit([], status=403)
        adapter = RedditAdapter(client_id="a", client_secret="b", opener=http)
        with self.assertRaises(RedditAccessError):
            adapter.search(SearchWindow(query="x"))


class RedditRateLimitTests(unittest.TestCase):
    def adapter(self, headers, waits):
        return RedditAdapter(
            client_id="a",
            client_secret="b",
            opener=FakeReddit([listing(post())], headers=headers),
            sleep=waits.append,
            clock=lambda: 1000.0,
        )

    def test_the_limit_is_read_from_the_platform_not_assumed(self) -> None:
        waits: list[float] = []
        adapter = self.adapter({"x-ratelimit-remaining": "42", "x-ratelimit-reset": "30"}, waits)
        adapter.search(SearchWindow(query="x"))
        self.assertEqual(adapter.rate_limit.remaining, 42.0)
        self.assertEqual(adapter.rate_limit.reset_seconds, 30.0)

    def test_it_slows_down_before_being_told_to(self) -> None:
        waits: list[float] = []
        adapter = self.adapter({"x-ratelimit-remaining": "1", "x-ratelimit-reset": "25"}, waits)
        adapter.search(SearchWindow(query="x"))  # observes the low budget
        adapter.search(SearchWindow(query="x"))  # and waits before the next call
        self.assertEqual(waits, [25.0])

    def test_a_429_defers_and_carries_the_platforms_own_wait(self) -> None:
        http = FakeReddit([], headers={"retry-after": "90"}, status=429)
        adapter = RedditAdapter(client_id="a", client_secret="b", opener=http)
        with self.assertRaises(RateLimited) as caught:
            adapter.search(SearchWindow(query="x"))
        self.assertEqual(caught.exception.retry_after_seconds, 90.0)

    def test_there_is_no_rotation_anywhere_in_the_module(self) -> None:
        # The only honest response to "you are going too fast" is to go slower.
        source = Path(ROOT / "src/web_scraper/sources/reddit.py").read_text()
        # Strip docstrings: the module explains at length why it does not
        # rotate, and grepping prose would fail on its own explanation.
        import ast

        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                source = source.replace(node.value, "")
        for forbidden in ("proxy", "rotate", "user_agents", "PROXY"):
            self.assertNotIn(forbidden, source, f"{forbidden} has no business here")


class RedditSearchTests(unittest.TestCase):
    def adapter(self, payloads):
        return RedditAdapter(
            client_id="a",
            client_secret="b",
            opener=FakeReddit(payloads),
            clock=lambda: 5000.0,
        )

    def test_a_post_is_normalised(self) -> None:
        entities, _, _ = self.adapter([listing(post())]).search(SearchWindow(query="q"))
        entity = entities[0]
        self.assertEqual(entity.platform, Platform.REDDIT)
        self.assertEqual(entity.natural_key, "reddit:abc")
        self.assertEqual(entity.community, "python")
        self.assertEqual(entity.engagement.score, 42)
        self.assertEqual(entity.provenance.route, CollectionRoute.OFFICIAL_API)

    def test_a_community_search_restricts_to_that_community(self) -> None:
        http = FakeReddit([listing(post())])
        adapter = RedditAdapter(client_id="a", client_secret="b", opener=http)
        adapter.search(SearchWindow(query="q", community="python"))
        url = http.requests[-1].full_url
        self.assertIn("/r/python/search", url)
        self.assertIn("restrict_sr", url)

    def test_a_time_window_excludes_out_of_range_posts(self) -> None:
        payload = listing(post("old", created_utc=10.0), post("new", created_utc=9000.0))
        entities, _, accounting = self.adapter([payload]).search(
            SearchWindow(query="q", since=1000.0)
        )
        self.assertEqual([e.platform_post_id for e in entities], ["new"])
        self.assertEqual(accounting.unavailable, 1, "the excluded post is counted, not dropped")
        self.assertTrue(accounting.is_complete)

    def test_a_deleted_author_becomes_none_rather_than_a_literal(self) -> None:
        entities, _, _ = self.adapter([listing(post(author="[deleted]"))]).search(
            SearchWindow(query="q")
        )
        self.assertIsNone(entities[0].author_name)

    def test_a_checkpoint_carries_the_cursor_for_the_next_run(self) -> None:
        _, checkpoint, _ = self.adapter([listing(post())]).search(SearchWindow(query="q"))
        self.assertEqual(checkpoint.cursor, "t3_next")
        self.assertEqual(checkpoint.latest_seen_id, "abc")

    def test_a_resumed_search_sends_the_cursor(self) -> None:
        http = FakeReddit([listing(post())])
        adapter = RedditAdapter(client_id="a", client_secret="b", opener=http)
        adapter.search(SearchWindow(query="q"), checkpoint=Checkpoint(cursor="t3_prev"))
        self.assertIn("after=t3_prev", http.requests[-1].full_url)

    def test_incremental_collection_stops_at_a_rate_limit_without_losing_pages(self) -> None:
        class Limiting(FakeReddit):
            def __init__(self):
                super().__init__([listing(post("p1"))])
                self.calls = 0

            def urlopen(self, request, timeout=None):
                if request.full_url.endswith("access_token"):
                    return super().urlopen(request, timeout)
                self.calls += 1
                if self.calls > 1:
                    raise urllib.error.HTTPError(
                        request.full_url, 429, "slow", {"retry-after": "60"}, None
                    )
                return super().urlopen(request, timeout)

        adapter = RedditAdapter(client_id="a", client_secret="b", opener=Limiting())
        entities, checkpoint, accounting = collect_incrementally(
            adapter, SearchWindow(query="q"), max_pages=5
        )
        self.assertEqual(len(entities), 1, "the page we did get is kept")
        self.assertEqual(checkpoint.cursor, "t3_next", "and we know where to resume")
        self.assertTrue(accounting.is_complete)


class RedditThreadTests(unittest.TestCase):
    def thread_payload(self, *, with_more=False):
        replies = {
            "data": {
                "children": [
                    {
                        "kind": "t1",
                        "data": {
                            "id": "c2",
                            "body": "a reply",
                            "author": "b",
                            "created_utc": 1100.0,
                        },
                    }
                ]
            }
        }
        children = [
            {
                "kind": "t1",
                "data": {
                    "id": "c1",
                    "body": "a comment",
                    "author": "a",
                    "created_utc": 1050.0,
                    "replies": replies,
                },
            }
        ]
        if with_more:
            children.append({"kind": "more", "data": {"count": 37, "children": ["x"]}})
        return [listing(post("root")), {"data": {"children": children}}]

    def adapter(self, payload):
        class MultiListing(FakeReddit):
            def urlopen(self, request, timeout=None):
                if request.full_url.endswith("access_token"):
                    return super().urlopen(request, timeout)
                outer_body = json.dumps(payload).encode()

                class Response:
                    status = 200
                    headers: dict = {}

                    def read(self, amount=None):
                        return outer_body

                    def __enter__(self):
                        return self

                    def __exit__(self, *_):
                        return False

                return Response()

        return RedditAdapter(client_id="a", client_secret="b", opener=MultiListing([]))

    def test_a_thread_keeps_its_shape(self) -> None:
        thread = self.adapter(self.thread_payload()).thread("root")
        by_id = {e.platform_post_id: e for e in thread.entities}
        self.assertEqual(by_id["c1"].reply_to_id, "root")
        self.assertEqual(by_id["c2"].reply_to_id, "c1")
        self.assertEqual(by_id["c2"].depth, 2)
        self.assertEqual(by_id["c1"].kind, EntityKind.COMMENT)
        self.assertEqual(by_id["c2"].kind, EntityKind.REPLY)

    def test_a_fully_retrieved_thread_is_complete(self) -> None:
        thread = self.adapter(self.thread_payload()).thread("root")
        self.assertTrue(thread.is_complete)

    def test_a_partial_thread_never_claims_to_be_complete(self) -> None:
        # The failure this prevents: an analysis over a fragment, confidently.
        thread = self.adapter(self.thread_payload(with_more=True)).thread("root")
        self.assertFalse(thread.is_complete)
        self.assertEqual(thread.completeness.more_nodes, 37)
        self.assertIn("platform did not send", thread.completeness.reason)

    def test_our_own_depth_ceiling_also_makes_it_incomplete(self) -> None:
        adapter = self.adapter(self.thread_payload())
        adapter.max_depth = 1
        thread = adapter.thread("root")
        self.assertFalse(thread.is_complete)
        self.assertTrue(thread.completeness.depth_limit_hit)


# --------------------------------------------------------------------------
# X
# --------------------------------------------------------------------------
class FakeOutcome:
    def __init__(self, verdict, body=b"", paid=None):
        self.result = Result(url="u", verdict=verdict)
        self.response = type("R", (), {"body": body})() if body else None
        self.paid = paid


class XConfigTests(unittest.TestCase):
    def test_the_api_is_off_by_default(self) -> None:
        self.assertFalse(XApiConfig().enabled)
        self.assertIsNone(XApiConfig().token())

    def test_a_disabled_api_yields_no_token_even_when_one_exists(self) -> None:
        import os

        os.environ["X_BEARER_TOKEN"] = "SECRET"
        self.addCleanup(lambda: os.environ.pop("X_BEARER_TOKEN", None))
        self.assertIsNone(XApiConfig(enabled=False).token())
        self.assertEqual(XApiConfig(enabled=True).token(), "SECRET")

    def test_enabling_the_api_does_not_silently_fall_back(self) -> None:
        # An operator who turned the API on wants the API; quietly using the
        # public path would answer a different question than the one they asked.
        adapter = XSearchAdapter(fetch=lambda u: None, api=XApiConfig(enabled=True))
        with self.assertRaises(NotImplementedError):
            adapter.search(SearchWindow(query="q"))


class XQueryTests(unittest.TestCase):
    def adapter(self, outcome=None):
        return XSearchAdapter(fetch=lambda u: outcome, clock=lambda: 100.0)

    def test_the_time_window_goes_into_the_query_not_a_post_filter(self) -> None:
        # A page of results we then discard was still a page we paid to render.
        url = self.adapter().search_url(
            SearchWindow(query="python", since=1_700_000_000.0, until=1_700_600_000.0)
        )
        self.assertIn("since%3A", url)
        self.assertIn("until%3A", url)

    def test_an_author_filter_uses_the_platforms_grammar(self) -> None:
        url = self.adapter().search_url(SearchWindow(query="q", community="someone"))
        self.assertIn("from%3Asomeone", url)

    def test_sort_selects_the_right_tab(self) -> None:
        self.assertIn("f=live", self.adapter().search_url(SearchWindow(query="q", sort="new")))
        self.assertIn("f=top", self.adapter().search_url(SearchWindow(query="q", sort="top")))


class XCollectionTests(unittest.TestCase):
    def adapter(self, outcome):
        return XSearchAdapter(fetch=lambda u: outcome, clock=lambda: 100.0)

    def test_a_login_wall_ends_the_query_rather_than_being_worked_around(self) -> None:
        result = self.adapter(FakeOutcome(Verdict.AUTH_REQUIRED)).search(SearchWindow(query="q"))
        self.assertTrue(result.login_wall)
        self.assertEqual(result.entities, ())

    def test_no_credential_or_session_handling_exists_in_the_module(self) -> None:
        source = Path(ROOT / "src/web_scraper/sources/x.py").read_text()
        for forbidden in ("auth_token", "ct0", "set_cookie", "guest_token"):
            self.assertNotIn(forbidden, source, f"{forbidden} has no business here")

    def test_posts_are_recovered_from_canonical_links(self) -> None:
        body = b'<a href="/someone/status/1234567890">x</a><a href="/other/status/999">y</a>'
        result = self.adapter(FakeOutcome(Verdict.OK, body)).search(SearchWindow(query="q"))
        self.assertEqual([e.platform_post_id for e in result.entities], ["1234567890", "999"])
        self.assertEqual(result.entities[0].author_name, "someone")

    def test_duplicates_on_a_page_are_collapsed_by_id(self) -> None:
        body = b'<a href="/a/status/1">x</a><a href="/a/status/1">x again</a>'
        result = self.adapter(FakeOutcome(Verdict.OK, body)).search(SearchWindow(query="q"))
        self.assertEqual(len(result.entities), 1)
        self.assertTrue(result.accounting.is_complete)

    def test_a_created_at_that_is_not_stated_is_not_invented(self) -> None:
        body = b'<a href="/a/status/1">x</a>'
        result = self.adapter(FakeOutcome(Verdict.OK, body)).search(SearchWindow(query="q"))
        self.assertIsNone(result.entities[0].created_at)

    def test_provenance_records_that_a_provider_fetched_it(self) -> None:
        paid = type(
            "P", (), {"attempted": True, "provider": "scrape.do", "strategy_id": "normal"}
        )()
        body = b'<a href="/a/status/1">x</a>'
        result = self.adapter(FakeOutcome(Verdict.OK, body, paid=paid)).search(
            SearchWindow(query="q")
        )
        provenance = result.entities[0].provenance
        self.assertEqual(provenance.route, CollectionRoute.PROVIDER)
        self.assertEqual(provenance.provider, "scrape.do")

    def test_a_public_conversation_is_never_claimed_to_be_whole(self) -> None:
        body = b'<a href="/a/status/1">root</a><a href="/b/status/2">reply</a>'
        thread = self.adapter(FakeOutcome(Verdict.OK, body)).conversation(
            "https://x.com/a/status/1"
        )
        self.assertFalse(thread.is_complete)
        self.assertIn("subset of replies", thread.completeness.reason)

    def test_a_walled_conversation_returns_the_root_and_says_why(self) -> None:
        thread = self.adapter(FakeOutcome(Verdict.AUTH_REQUIRED)).conversation(
            "https://x.com/a/status/1"
        )
        self.assertFalse(thread.is_complete)
        self.assertIn("requires an account", thread.completeness.reason)


if __name__ == "__main__":
    unittest.main()
