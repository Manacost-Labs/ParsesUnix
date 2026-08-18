from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.contracts import PAID_ESCALATION_VERDICTS
from web_scraper.diagnose import diagnose_attempts, diagnose_queue
from web_scraper.diagnose.cli import main as diagnose_main
from web_scraper.queue import QueueStore


def attempt(url, verdict, reason="", level="L1"):
    return {"url": url, "verdict": verdict, "reason": reason, "level": level}


class GroupingTests(unittest.TestCase):
    def test_successes_are_not_failures(self) -> None:
        diagnosis = diagnose_attempts(
            [
                attempt("https://x.example/1", "OK"),
                attempt("https://x.example/2", "NOT_MODIFIED"),
            ]
        )
        self.assertEqual(diagnosis.failures, 0)
        self.assertEqual(diagnosis.success_rate, 1.0)
        self.assertEqual(diagnosis.groups, ())
        self.assertIn("no failures", diagnosis.headline)

    def test_same_shape_different_numbers_group_together(self) -> None:
        # "HTTP 502" and "HTTP 503" are one operational group, not two.
        diagnosis = diagnose_attempts(
            [
                attempt("https://x.example/1", "ORIGIN_DOWN", "target returned HTTP 502"),
                attempt("https://x.example/2", "ORIGIN_DOWN", "target returned HTTP 503"),
            ]
        )
        self.assertEqual(len(diagnosis.groups), 1)
        self.assertEqual(diagnosis.groups[0].count, 2)

    def test_groups_are_ordered_by_size_with_shares(self) -> None:
        records = (
            [attempt(f"https://x.example/o{i}", "ORIGIN_DOWN", "HTTP 502") for i in range(6)]
            + [
                attempt(f"https://x.example/b{i}", "BLOCKED", "blocking signature")
                for i in range(3)
            ]
            + [attempt("https://x.example/p", "PARSE_FAIL", "canary missing")]
        )
        diagnosis = diagnose_attempts(records)
        self.assertEqual(
            [g.verdict for g in diagnosis.groups], ["ORIGIN_DOWN", "BLOCKED", "PARSE_FAIL"]
        )
        self.assertAlmostEqual(diagnosis.groups[0].share, 0.6)
        self.assertAlmostEqual(sum(g.share for g in diagnosis.groups), 1.0)

    def test_failures_are_counted_per_domain(self) -> None:
        diagnosis = diagnose_attempts(
            [
                attempt("https://a.example/1", "ORIGIN_DOWN"),
                attempt("https://a.example/2", "ORIGIN_DOWN"),
                attempt("https://b.example/1", "BLOCKED"),
            ]
        )
        self.assertEqual(diagnosis.by_domain, {"a.example": 2, "b.example": 1})


class PolicyTests(unittest.TestCase):
    """The whole point: advice must never contradict the escalation policy."""

    def test_origin_down_is_never_paid_eligible(self) -> None:
        diagnosis = diagnose_attempts([attempt("https://x.example/1", "ORIGIN_DOWN", "HTTP 502")])
        group = diagnosis.groups[0]
        self.assertFalse(group.may_escalate_to_paid)
        self.assertIn("not blocking us", group.remedy)
        self.assertIn("must NOT be escalated", diagnosis.headline)

    def test_dead_url_and_rate_limit_are_never_paid_eligible(self) -> None:
        for verdict in ("DEAD_URL", "RATE_LIMITED", "PARSE_FAIL", "THIN_CONTENT", "ACCESS_DENIED"):
            diagnosis = diagnose_attempts([attempt("https://x.example/1", verdict)])
            self.assertFalse(diagnosis.groups[0].may_escalate_to_paid, verdict)

    def test_only_block_verdicts_are_paid_eligible(self) -> None:
        eligible = set()
        for verdict in (
            "OK",
            "DEAD_URL",
            "ORIGIN_DOWN",
            "RATE_LIMITED",
            "AUTH_REQUIRED",
            "ACCESS_DENIED",
            "BLOCKED",
            "SOFT_BLOCK",
            "THIN_CONTENT",
            "PROVIDER_ERROR",
            "PARSE_FAIL",
        ):
            diagnosis = diagnose_attempts([attempt("https://x.example/1", verdict)])
            if diagnosis.groups and diagnosis.groups[0].may_escalate_to_paid:
                eligible.add(verdict)
        # Derived from the contract, so it cannot drift away from the policy.
        self.assertEqual(eligible, {v.value for v in PAID_ESCALATION_VERDICTS})

    def test_paid_escalation_share_reflects_only_eligible_failures(self) -> None:
        diagnosis = diagnose_attempts(
            [
                attempt("https://x.example/1", "ORIGIN_DOWN"),
                attempt("https://x.example/2", "ORIGIN_DOWN"),
                attempt("https://x.example/3", "BLOCKED"),
                attempt("https://x.example/4", "SOFT_BLOCK"),
            ]
        )
        self.assertAlmostEqual(diagnosis.paid_escalation_share, 0.5)

    def test_auth_required_advice_does_not_suggest_bypassing(self) -> None:
        diagnosis = diagnose_attempts([attempt("https://x.example/1", "AUTH_REQUIRED")])
        remedy = diagnosis.groups[0].remedy.lower()
        self.assertIn("authorized", remedy)
        self.assertNotIn("bypass", remedy.replace("do not attempt to bypass", ""))


class QueueIntegrationTests(unittest.TestCase):
    def test_diagnose_reads_the_run_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = QueueStore(Path(tmp) / "queue.sqlite3")
            queue.add("https://x.example/a")
            queue.log_attempt(
                "https://x.example/a", verdict="ORIGIN_DOWN", level="L1", reason="HTTP 502"
            )
            queue.log_attempt("https://x.example/a", verdict="OK", level="L1", reason="passed")
            diagnosis = diagnose_queue(queue)
            self.assertEqual(diagnosis.total_attempts, 2)
            self.assertEqual(diagnosis.failures, 1)
            self.assertEqual(diagnosis.groups[0].verdict, "ORIGIN_DOWN")


class DiagnoseCliTests(unittest.TestCase):
    def run_cli(self, argv) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = diagnose_main(argv)
        return code, buffer.getvalue()

    def test_json_output_from_attempts_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "attempts.json"
            path.write_text(
                json.dumps(
                    [
                        attempt("https://x.example/1", "ORIGIN_DOWN", "HTTP 502"),
                        attempt("https://x.example/2", "BLOCKED", "blocking signature"),
                    ]
                )
            )
            code, out = self.run_cli(["--attempts-json", str(path), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["diagnosis"]["failures"], 2)

    def test_text_output_marks_paid_eligibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "attempts.json"
            path.write_text(json.dumps([attempt("https://x.example/1", "ORIGIN_DOWN", "HTTP 502")]))
            _code, out = self.run_cli(["--attempts-json", str(path)])
        self.assertIn("NO paid", out)

    def test_missing_queue_is_a_clean_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            buffer = io.StringIO()
            with contextlib.redirect_stderr(buffer):
                code = diagnose_main(["--queue", str(Path(tmp) / "nope.sqlite3")])
        self.assertEqual(code, 2)
        self.assertIn("not found", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
