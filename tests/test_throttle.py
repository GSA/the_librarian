"""Adaptive model-call concurrency (AIMD) and the range-encoded inspected_records form."""
import json
import threading
import time
import unittest
from collections import Counter

from tools.intake import model_review, throttle
from tools.intake.model_review import TransportError


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def call(limiter, outcome="ok", code=None):
    """One gated call that ends cleanly, raises ``code``, or reports ``code`` as a retry."""
    def transport(model, messages):
        if outcome == "retried":
            limiter._observe(code)
        if outcome == "error":
            raise TransportError(code)
        return {"content": "{}", "usage": {}}
    with limiter.gating(transport) as gated:
        try:
            gated("m", [])
        except TransportError:
            pass


class LimiterTests(unittest.TestCase):
    def test_limit_never_exceeds_cap_and_validates_arguments(self):
        for bad in ((0,), (3, {"initial": 4}), (3, {"initial": 0}), (3, {"decrease": 1})):
            with self.assertRaises(ValueError):
                throttle.Limiter(bad[0], **(bad[1] if len(bad) > 1 else {}))
        limiter = throttle.Limiter(4)
        for _ in range(50):
            call(limiter)
        self.assertEqual(limiter.limit, 4)
        self.assertEqual(limiter.report()["final"], 4)

    def test_backs_off_multiplicatively_on_429_and_503_only(self):
        limiter = throttle.Limiter(8)
        call(limiter, "error", "http-429")
        self.assertEqual(limiter.limit, 4)
        call(limiter, "error", "http-503")
        self.assertEqual(limiter.limit, 2)
        call(limiter, "retried", "http-429")
        self.assertEqual(limiter.limit, 1)
        call(limiter, "error", "http-429")
        self.assertEqual(limiter.limit, 1)
        for code in ("http-500", "http-400", "network", "invalid-response"):
            other = throttle.Limiter(8)
            call(other, "error", code)
            self.assertEqual(other.limit, 8, code)
        report = limiter.report()
        self.assertEqual((report["backoffs"], report["lowest"], report["cap"]), (3, 1, 8))

    def test_recovers_one_per_window_of_clean_replies_up_to_the_cap(self):
        limiter = throttle.Limiter(4, initial=1)
        seen = []
        for _ in range(12):
            call(limiter)
            seen.append(limiter.limit)
        self.assertEqual(seen, [2, 2, 3, 3, 3, 4, 4, 4, 4, 4, 4, 4])
        call(limiter, "error", "http-429")
        self.assertEqual(limiter.limit, 2)

    def test_a_retried_then_successful_call_does_not_earn_recovery(self):
        limiter = throttle.Limiter(4, initial=2)
        call(limiter, "retried", "http-503")
        self.assertEqual(limiter.limit, 1)
        for _ in range(10):
            call(limiter, "retried", "http-503")
        self.assertEqual(limiter.limit, 1)
        call(limiter)
        self.assertEqual(limiter.limit, 2)

    def test_non_adaptive_limiter_holds_the_limit_but_still_reports(self):
        limiter = throttle.Limiter(3, adaptive=False)
        call(limiter, "error", "http-429")
        self.assertEqual(limiter.limit, 3)
        report = limiter.report()
        self.assertEqual((report["adaptive"], report["backoffs"], report["throttle_signals"]),
                         (False, 0, 1))

    def test_a_burst_of_simultaneous_429s_costs_one_halving(self):
        limiter = throttle.Limiter(8)
        barrier, errors = threading.Barrier(8), []

        def transport(model, messages):
            barrier.wait(timeout=10)
            raise TransportError("http-429")
        with limiter.gating(transport) as gated:
            def run():
                try:
                    gated("m", [])
                except TransportError as error:
                    errors.append(error.code)
            threads = [threading.Thread(target=run) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(errors, ["http-429"] * 8)
        self.assertEqual(limiter.limit, 4)
        self.assertEqual(limiter.report()["backoffs"], 1)

    def test_in_flight_never_exceeds_the_current_limit(self):
        limiter = throttle.Limiter(5, initial=3)
        lock, running, peak = threading.Lock(), [0], [0]

        def transport(model, messages):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.01)
            with lock:
                running[0] -= 1
            return {"content": "{}", "usage": {}}
        with limiter.gating(transport) as gated:
            threads = [threading.Thread(target=gated, args=("m", [])) for _ in range(30)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertLessEqual(peak[0], 5)
        report = limiter.report()
        self.assertEqual((report["calls"], report["max_in_flight"]), (30, peak[0]))
        self.assertGreater(report["calls_per_minute"], 0)

    def test_effective_rate_uses_the_clock(self):
        clock = Clock()
        limiter = throttle.Limiter(2, clock=clock)

        def transport(model, messages):
            clock.now += 30.0
            return {"content": "{}", "usage": {}}
        with limiter.gating(transport) as gated:
            gated("m", [])
            gated("m", [])
        report = limiter.report()
        self.assertEqual((report["calls"], report["seconds"], report["calls_per_minute"],
                          report["mean_in_flight"]), (2, 60.0, 2.0, 1.0))

    def test_observer_is_attached_to_a_usai_transport_only_while_gating(self):
        transport = lambda model, messages: {"content": "{}", "usage": {}}  # noqa: E731
        transport.observers = []
        limiter = throttle.Limiter(4)
        with limiter.gating(transport):
            self.assertEqual(len(transport.observers), 1)
        self.assertEqual(transport.observers, [])


class RangeFormTests(unittest.TestCase):
    EXPECTED = {"records": list(range(10)), "warnings": ["#/payload/warnings/0"]}

    def parse(self, records):
        return model_review._parse(json.dumps({
            "decision": "accept", "inspected_records": records,
            "inspected_warnings": ["#/payload/warnings/0"], "explanation": "ok"}), self.EXPECTED)

    def test_ranges_and_integers_expand_to_the_same_findings_as_the_full_list(self):
        full = self.parse(list(range(10)))
        self.assertEqual(self.parse(["0-9"]), full)
        self.assertEqual(self.parse([0, "1-8", 9]), full)
        self.assertEqual(self.parse(["5-9"])["records"], [5, 6, 7, 8, 9])
        self.assertEqual(self.parse(["0-2", 4])["records"], [0, 1, 2, 4])

    def test_malformed_overlapping_or_out_of_range_replies_are_invalid(self):
        for bad in (["3-1"], ["0-10"], ["-1"], ["a-b"], ["0-3", 2], ["0-3", "3-5"], [1, 1],
                    ["0-999999999999"], ["0 - 3 - 5"], [None], [1.5], [True], "0-9", {"0": 9},
                    ["0-9", 0], [["0-9"]], ["1e3-5"], ["٠-٣"]):
            self.assertIsNone(self.parse(bad), bad)

    def test_a_range_cannot_expand_beyond_the_rendition_records(self):
        self.assertIsNone(model_review.expand_records(["0-5000"], 10))
        self.assertIsNone(model_review.expand_records(["0-5", "6-12"], 10))
        self.assertEqual(model_review.expand_records(["0-9"], 10), list(range(10)))

    def test_prompt_asks_for_ranges_only_when_requested(self):
        body = {"format": "text", "records": [{"i": 0, "text": "a"}], "warnings": []}
        plain, ranged = model_review._task(body), model_review._task(body, inspected_form="ranges")
        self.assertEqual(plain["messages"][0]["content"], model_review.SYSTEM)
        self.assertEqual(ranged["messages"][0]["content"], model_review.SYSTEM + model_review.RANGE_NOTE)
        self.assertEqual(plain["messages"][1], ranged["messages"][1])
        self.assertEqual(plain["expected"], ranged["expected"])
        with self.assertRaises(ValueError):
            model_review._task(body, inspected_form="sparse")

    def test_range_reply_with_a_gap_is_recorded_as_incomplete(self):
        prepared = model_review._task({"format": "text", "warnings": [], "records": [
            {"i": i, "text": str(i)} for i in range(10)]}, inspected_form="ranges")
        reply = {"content": json.dumps({"decision": "accept", "inspected_records": ["0-3", "5-9"],
                                        "inspected_warnings": [], "explanation": "x"}),
                 "usage": {}}
        result = model_review.assess(prepared, "m", lambda model, messages: reply)
        self.assertEqual((result["status"], result["complete"]), ("reviewed", False))
        self.assertEqual(result["parsed"]["records"], [0, 1, 2, 3, 5, 6, 7, 8, 9])
        reply["content"] = json.dumps({"decision": "accept", "inspected_records": ["0-9"],
                                       "inspected_warnings": [], "explanation": "x"})
        self.assertTrue(model_review.assess(prepared, "m", lambda m, x: reply)["complete"])


if __name__ == "__main__":
    unittest.main()
