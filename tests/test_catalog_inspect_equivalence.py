"""A cheaper catalog ``inspect`` refuses and accepts exactly what the original did (Kata wjf9)."""
from contextlib import closing
import json
import os
from pathlib import Path
import random
import shutil
import sqlite3
import tempfile
import unicodedata
import unittest

from tests.reference_inspect import reference_inspect
from tools.catalog import catalog_store as catalog
from tools.catalog import contracts
from tools.catalog.contracts import CatalogError
from tools.diagnostics.catalog_lock_benchmark import registration
from tools.intake import store


def original_text_ok(value, maximum=500):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= maximum
            and not any(unicodedata.category(c) in {"Cc", "Cs"} for c in value))


class TextAndEncoding(unittest.TestCase):
    def test_text_agrees_with_the_per_character_check_for_every_low_code_point(self):
        for code in list(range(0, 0x300)) + [0x2028, 0x200B, 0xD800, 0xDFFF, 0xE000, 0xFFFF, 0x1F600]:
            for template in ("{}", "a{}b", " {} ", "{}{}"):
                value = template.format(chr(code), chr(code))
                try:
                    contracts.text(value, "x")
                    accepted = True
                except CatalogError:
                    accepted = False
                self.assertEqual(accepted, original_text_ok(value), hex(code))

    def test_text_agrees_on_random_strings_and_limits(self):
        rng = random.Random(7)
        alphabet = [chr(c) for c in (*range(0, 130), 0xA0, 0xAD, 0x85, 0x2028, 0xD800, 0x4E2D, 0x1F600)]
        for _ in range(3000):
            value = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 8)))
            for maximum in (3, 500):
                try:
                    contracts.text(value, "x", maximum)
                    accepted = True
                except CatalogError:
                    accepted = False
                self.assertEqual(accepted, original_text_ok(value, maximum), repr(value))
        for value in (None, 5, b"abc", ["a"]):
            with self.assertRaises(CatalogError):
                contracts.text(value, "x")

    def test_canonical_equals_json_dumps_and_still_refuses_non_json(self):
        rng = random.Random(11)
        for _ in range(300):
            value = {rng.choice("bca"): [rng.choice([1, 1.5, "é", None, True, "\u2028", "\x00"]) for _ in range(3)],
                     "z": {"y": "\U0001F600"}}
            self.assertEqual(contracts.canonical(value), json.dumps(
                value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False))
        for bad in (float("nan"), float("inf"), {1, 2}, object(), {(1, 2): 3}):
            with self.assertRaisesRegex(CatalogError, "not canonical JSON"):
                contracts.canonical(bad)


class Case(unittest.TestCase):
    template = None

    @classmethod
    def setUpClass(cls):
        cls.template_temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.template_temp.cleanup)
        root = Path(cls.template_temp.name) / "instance"
        root.mkdir()
        store.initialize(root)
        catalog.initialize(root)
        for n in range(4):
            catalog.submit(root, registration(n), actor="a", request_id=f"r{n}", reason="test",
                           expected_state=catalog.status(root)["state_token"])
        # An edit of one record, so the history holds a superseded version.
        edited = registration(1)
        edited[3]["data"]["reason"] = "Edited later."
        catalog.submit(root, edited, actor="b", request_id="r-edit", reason="edit",
                       expected_state=catalog.status(root)["state_token"])
        cls.template = root

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "instance"
        shutil.copytree(self.template, self.root, symlinks=True)
        self.home, self.db, self.backups = catalog.locations(self.root)
        catalog._VALID_PAYLOADS.clear()

    def rebuild(self):
        """A fresh copy of the unmodified template, for several tamperings in one test."""
        self.temp.cleanup()
        self.setUp()

    def outcome(self, function, **kwargs):
        with closing(catalog.connect(self.db)) as connection:
            connection.execute("BEGIN")
            try:
                return ("ok", function(connection, **kwargs))
            except CatalogError as error:
                return ("refused", str(error))

    def both(self):
        catalog._VALID_PAYLOADS.clear()
        reference = self.outcome(reference_inspect)
        full = self.outcome(catalog.inspect)
        catalog._VALID_PAYLOADS.clear()
        light = self.outcome(catalog.inspect, detail=False)
        return reference, full, light

    def assertSame(self, label):
        reference, full, light = self.both()
        self.assertEqual(full, reference, label)
        if reference[0] == "ok":
            self.assertEqual(light[1][0], reference[1][0], label)
            self.assertEqual(light[1][1:], (None, None, None, None), label)
        else:
            self.assertEqual(light, reference, label)
        return reference

    def mutate(self, *statements, triggers=True):
        with closing(sqlite3.connect(self.db, isolation_level=None)) as connection:
            for table in ("metadata", "revisions", "versions"):
                for action in ("update", "delete"):
                    connection.execute(f"DROP TRIGGER IF EXISTS immutable_{table}_{action}")
            connection.execute("PRAGMA foreign_keys=OFF")
            # With the lookup indexes present SQLite itself refuses a malformed-JSON payload edit, so the
            # edit is made on the legacy shape and the lookup structures are rebuilt from the edited
            # payloads where SQLite accepts them: inspect is compared on both shapes with the reference.
            catalog.drop_lookup(connection)
            for sql, *params in statements:
                try:
                    connection.execute(sql, params)
                except sqlite3.IntegrityError:
                    pass  # SQLite's own constraint refused this edit; the database is unchanged
            try:
                connection.execute("BEGIN")
                catalog.create_lookup(connection)
                connection.execute("COMMIT")
            except sqlite3.Error:
                connection.execute("ROLLBACK")  # malformed JSON cannot be indexed: stays the legacy shape
            if triggers:  # leave only the intended edit, so the refusal is not the dropped trigger's
                for table in ("metadata", "revisions", "versions"):
                    for action in ("update", "delete"):
                        connection.execute(catalog.trigger_sql(table, action))
        os.chmod(self.db, 0o600)


class ValidHistory(Case):
    def test_all_five_results_equal_the_original_on_a_valid_history(self):
        reference, full, light = self.both()
        self.assertEqual(reference[0], "ok")
        self.assertEqual(full, reference)
        self.assertEqual(light[1][0], reference[1][0])
        self.assertEqual(reference[1][0]["revision"], 5)

    def test_empty_catalog(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "instance"
            root.mkdir()
            store.initialize(root)
            catalog.initialize(root)
            db = catalog.locations(root)[1]
            with closing(catalog.connect(db)) as connection:
                connection.execute("BEGIN")
                self.assertEqual(catalog.inspect(connection), reference_inspect(connection))

    def test_a_memoized_payload_is_still_hash_checked_after_a_later_edit(self):
        with closing(catalog.connect(self.db)) as connection:
            connection.execute("BEGIN")
            catalog.inspect(connection, detail=False)
        self.mutate(("UPDATE versions SET payload_hash=? WHERE revision=2 AND ordinal=0", "0" * 64))
        self.assertEqual(self.assertSame("memo then payload hash")[1], "Payload integrity mismatch")


class Tampering(Case):
    def test_named_tampering_classes_are_refused_with_the_original_messages(self):
        payload = "SELECT payload FROM versions WHERE revision=2 AND ordinal=%d"
        cases = {
            "payload text edited": [("UPDATE versions SET payload=replace(payload,'Synthetic.','Edited.') "
                                     "WHERE revision=2")],
            "payload not canonical": [("UPDATE versions SET payload=replace(payload,'\"kind\":','\"kind\": ') "
                                       "WHERE revision=3 AND ordinal=0")],
            "payload escaped differently": [("UPDATE versions SET payload=replace(payload,'Synthetic.','Synthetic\\u002e') "
                                             "WHERE revision=3 AND ordinal=2")],
            "payload hash edited": [("UPDATE versions SET payload_hash=? WHERE revision=1 AND ordinal=0", "1" * 64)],
            "payload record id edited": [("UPDATE versions SET record_id='S999' WHERE revision=2 AND ordinal=0")],
            "payload record id swapped in payload": [(
                "UPDATE versions SET payload=replace(payload,'\"id\":\"S2\"','\"id\":\"S9\"') WHERE record_id='S2'")],
            "duplicate key": [("UPDATE versions SET payload=replace(payload,'{\"data\"','{\"id\":\"S0\",\"data\"') "
                               "WHERE revision=1 AND ordinal=0")],
            "invalid json": [("UPDATE versions SET payload='{' WHERE revision=1 AND ordinal=0")],
            "not an object": [("UPDATE versions SET payload='[]' WHERE revision=1 AND ordinal=0")],
            "nonfinite": [("UPDATE versions SET payload=replace(payload,'\"bytes\":100','\"bytes\":NaN') "
                           "WHERE revision=2 AND ordinal=2")],
            "extra field": [("UPDATE versions SET payload=replace(payload,'{\"data\"','{\"extra\":1,\"data\"') "
                             "WHERE revision=1 AND ordinal=0")],
            "invalid value": [("UPDATE versions SET payload=replace(payload,'\"role\":\"original\"','\"role\":\"x\"') "
                               "WHERE revision=2 AND ordinal=2")],
            "payload stored as blob": [("UPDATE versions SET payload=CAST(payload AS BLOB) WHERE revision=1")],
            "payload stored as integer": [("UPDATE versions SET payload=5 WHERE revision=1 AND ordinal=0")],
            "revision input hash": [("UPDATE revisions SET input_hash=? WHERE revision=3", "2" * 64)],
            "revision hash": [("UPDATE revisions SET revision_hash=? WHERE revision=3", "3" * 64)],
            "previous hash": [("UPDATE revisions SET previous_hash=? WHERE revision=3", "4" * 64)],
            "actor edited": [("UPDATE revisions SET actor='mallory' WHERE revision=2")],
            "actor control": [("UPDATE revisions SET actor=char(7) WHERE revision=2")],
            "reason empty": [("UPDATE revisions SET reason='  ' WHERE revision=2")],
            "request id blank": [("UPDATE revisions SET request_id=' ' WHERE revision=2")],
            "committed_at naive": [("UPDATE revisions SET committed_at='2026-01-01T00:00:00' WHERE revision=2")],
            "record count": [("UPDATE revisions SET record_count=3 WHERE revision=2")],
            "ordinal gap": [("UPDATE versions SET ordinal=9 WHERE revision=2 AND ordinal=3")],
            "member deleted": [("DELETE FROM versions WHERE revision=2 AND ordinal=3")],
            "last revision deleted": [("DELETE FROM versions WHERE revision=5"), ("DELETE FROM revisions WHERE revision=5")],
            "middle revision deleted": [("DELETE FROM versions WHERE revision=3"), ("DELETE FROM revisions WHERE revision=3")],
            "revision without members": [("DELETE FROM versions WHERE revision=2")],
            "members without revision": [("DELETE FROM revisions WHERE revision=5")],
            "projection row deleted": [("DELETE FROM current_records WHERE record_id='S2'")],
            "projection row edited": [("UPDATE current_records SET revision=1, ordinal=0 WHERE record_id='S2'")],
            "projection points to stale version": [("UPDATE current_records SET revision=2, ordinal=3 "
                                                    "WHERE record_id='A-upload-1-submission'")],
            "projection row added": [("INSERT INTO current_records VALUES ('S77',1,0)")],
            "metadata edited": [("UPDATE metadata SET value='other' WHERE key='authority'")],
            "store id edited": [("UPDATE metadata SET value='not-a-uuid' WHERE key='store_id'")],
            "metadata row added": [("INSERT INTO metadata VALUES ('extra','x')")],
            "created_at edited": [("UPDATE metadata SET value='yesterday' WHERE key='created_at'")],
            "application id": [("PRAGMA application_id=1")],
            "user version": [("PRAGMA user_version=9")],
        }
        refused, seen = 0, set()
        for label, statements in cases.items():
            with self.subTest(label):
                self.rebuild()
                self.mutate(*[(s,) if isinstance(s, str) else s for s in statements])
                kind, message = self.assertSame(label)
                refused += kind == "refused"
                self.assertEqual(kind, "refused", (label, message))
                seen.add(message)
        self.assertEqual(refused, len(cases))
        self.assertGreater(len(seen), 12)  # many distinct refusals, not one catch-all

    def test_a_dropped_immutability_trigger_is_refused(self):
        self.mutate(triggers=False)
        self.assertEqual(self.assertSame("triggers")[1], "Immutable trigger changed")

    def test_random_single_field_mutations_agree_with_the_original(self):
        rng = random.Random(20261111)
        columns = {"revisions": ["actor", "reason", "request_id", "committed_at", "input_hash", "previous_hash",
                                 "revision_hash", "record_count"],
                   "versions": ["record_id", "payload", "payload_hash", "ordinal"]}
        values = ["x", "", " ", "\x00", "\t", "é", "\u2028", "0" * 64, "2026-01-01T00:00:00+00:00", "{}", "null"]
        outcomes = set()
        for _ in range(60):
            self.rebuild()
            table = rng.choice(list(columns))
            column = rng.choice(columns[table])
            where = "revision=%d" % rng.randint(1, 5)
            if table == "versions":
                where += " AND ordinal=%d" % rng.randint(0, 3)
            if column == "payload" and rng.random() < 0.7:
                # Edit inside a payload: swap a few characters of the stored JSON.
                self.mutate((f"UPDATE versions SET payload=substr(payload,1,?)||?||substr(payload,?) WHERE {where}",
                             (cut := rng.randint(2, 60)), rng.choice(["x", " ", "\\u0000", "é", "\"", ""]), cut + 2))
            else:
                self.mutate((f"UPDATE {table} SET {column}=? WHERE {where}", rng.choice(values)))
            outcomes.add(self.assertSame(f"{table}.{column}")[0])
        self.assertEqual(outcomes, {"refused"} | (outcomes & {"ok"}))


class CallersGetWhatTheyNeed(Case):
    def test_rebase_index_equals_the_original_history_maps(self):
        _, _, histories, _, committed_states = reference_inspect(self.open_reference())
        expected_tokens = {c["state_token"]: n for n, c in committed_states.items()}
        state = catalog.status(self.root)
        expected_tokens[catalog.make_state(state, 0, "", 0)["state_token"]] = 0
        expected_writers = {rid: versions[-1]["revision"] for rid, versions in histories.items()}
        self.assertEqual(catalog.rebase_index(self.root), (expected_tokens, expected_writers))

    def open_reference(self):
        connection = catalog.connect(self.db)
        self.addCleanup(connection.close)
        connection.execute("BEGIN")
        return connection

    def test_rebase_index_refuses_a_tampered_catalog(self):
        self.mutate(("UPDATE versions SET payload=replace(payload,'Synthetic.','Edited.') WHERE revision=2",))
        with self.assertRaises(CatalogError):
            catalog.rebase_index(self.root)

    def test_streaming_inspect_keeps_no_history_without_detail(self):
        connection = self.open_reference()
        result = catalog.inspect(connection, detail=False)
        self.assertEqual(result[1:], (None, None, None, None))


if __name__ == "__main__":
    unittest.main()
