"""Run unittest discovery and fail when the set of skipped tests differs from the expected set.

``python -m unittest`` reports a skip as success, so a provisioned runtime that silently disappears (or a
new ``skipUnless`` nobody noticed) would keep CI green. This runner compares every skipped test id with
the lines of ``--expected-skips`` (blank lines and ``#`` comments ignored) and fails on any difference in
either direction. Standard library only, so it also runs under the Docling interpreter.
"""
import argparse
from pathlib import Path
import sys
import unittest


def expected(path):
    if path is None:
        return set()
    lines = (line.strip() for line in Path(path).read_text().splitlines())
    return {line for line in lines if line and not line.startswith("#")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="tests", help="discovery directory (default: tests)")
    parser.add_argument("--pattern", default="test*.py")
    parser.add_argument("--expected-skips", help="file of test ids that must be the exact skip set")
    parser.add_argument("--durations", type=int, default=15, help="report the N slowest tests")
    arguments = parser.parse_args(argv)

    sys.path.insert(0, str(Path.cwd()))
    suite = unittest.TestLoader().discover(arguments.start, pattern=arguments.pattern)
    runner = unittest.TextTestRunner(verbosity=2, durations=arguments.durations)
    result = runner.run(suite)
    if not result.wasSuccessful():
        return 1

    skipped = {test.id() for test, _ in result.skipped}
    allowed = expected(arguments.expected_skips)
    unexpected, missing = sorted(skipped - allowed), sorted(allowed - skipped)
    for name in unexpected:
        print(f"::error::unexpected skip: {name}")
    for name in missing:
        print(f"::error::expected skip did not happen (remove it from the list): {name}")
    if unexpected or missing:
        print(f"Skipped {len(skipped)} tests; expected {len(allowed)}.", file=sys.stderr)
        return 1
    print(f"Skip set matches the expected {len(allowed)} tests.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
