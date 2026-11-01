"""Synthetic fixtures only. Never writes to the online demonstration database."""
import copy
from contextlib import closing
import gzip
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import shutil
import uuid
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import catalog


class ScratchDirectory:
    """Use a workspace sandbox rather than the host's restricted TEMP directory."""
    def __init__(self):
        self.root = (Path.cwd() / "work").resolve()
        self.path = self.root / ("test-" + uuid.uuid4().hex)
        self.path.mkdir(parents=True)
        self.name = str(self.path)

    def cleanup(self):
        if self.path.resolve().parent != self.root or not self.path.name.startswith("test-"):
            raise RuntimeError("refusing cleanup outside test workspace")
        shutil.rmtree(self.path)

    def __enter__(self):
        return self.name

    def __exit__(self, *args):
        self.cleanup()


def product(pid=1, price="12.30", available=True):
    return {"id": pid, "title": "  Ｔｅａ\u00a0  Shirt ", "handle": f"shirt-{pid}",
            "vendor": "Fixture Co", "body_html": "<p>Soft &amp; light</p><script>bad()</script><p>Cotton</p>",
            "options": [{"name": "Size", "position": 2}, {"name": "Color", "position": 1}],
            "images": [{"src": "//cdn.example.test/shirt.jpg"}],
            "variants": [{"id": pid * 10, "sku": " T-1 ", "title": "Blue / M", "price": price,
                          "available": available, "option1": "Blue", "option2": "M", "option3": None,
                          "compare_at_price": "15.00"}]}


class FixtureClient:
    def __init__(self, pages, currency="CAD", deny=False):
        self.pages, self.currency, self.deny = pages, currency, deny

    def get(self, url):
        if url.endswith("/robots.txt"):
            return b"User-agent: *\nDisallow: /" if self.deny else b"User-agent: *\nAllow: /"
        if url.endswith("/"):
            return ("Shopify.currency = " + json.dumps({"active": self.currency})).encode()
        page = int(url.rsplit("page=", 1)[1])
        value = self.pages.get(page, [])
        if isinstance(value, Exception):
            raise value
        if isinstance(value, bytes):
            return value
        return json.dumps({"products": value}).encode()


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = ScratchDirectory()
        self.db_path = Path(self.temp.name) / "synthetic.sqlite"

    def tearDown(self):
        self.temp.cleanup()

    def run_fixture(self, pages, **kwargs):
        currency = kwargs.pop("currency", "CAD")
        deny = kwargs.pop("deny", False)
        return catalog.collect(self.db_path, source_kind="synthetic_fixture", delay=0,
                               client_factory=lambda db, run: FixtureClient(pages, currency, deny), **kwargs)

    def rows(self, table):
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(f"SELECT * FROM {table}")]

    def test_repeat_is_idempotent_but_retains_snapshots(self):
        first = self.run_fixture({1: [product()]})
        second = self.run_fixture({1: [product()]})
        self.assertEqual((first["status"], second["status"]), ("complete", "complete"))
        self.assertEqual(len(self.rows("products")), 1)
        self.assertEqual(len(self.rows("snapshots")), 2)
        self.assertEqual(self.rows("changes"), [])
        self.assertEqual(self.rows("products")[0]["first_seen_run"], 1)
        self.assertEqual(self.rows("products")[0]["last_seen_run"], 2)

    def test_price_and_availability_changes(self):
        self.run_fixture({1: [product()]})
        self.run_fixture({1: [product(price="13.05", available=False)]})
        changes = self.rows("changes")
        self.assertEqual({c["field"] for c in changes}, {"price", "available"})
        self.assertTrue(all(c["variant_id"] == "10" for c in changes))
        self.assertEqual(json.loads(next(c["after_value"] for c in changes if c["field"] == "price")), "13.05")

    def test_equivalent_decimals_are_not_price_changes(self):
        self.run_fixture({1: [product(price="12.30")]})
        self.run_fixture({1: [product(price="12.300")]})
        self.assertEqual(self.rows("changes"), [])

    def test_currency_change_does_not_compare_unlike_money(self):
        self.run_fixture({1: [product()]})
        self.run_fixture({1: [product(price="100.00")]}, currency="JPY")
        self.assertEqual([c["field"] for c in self.rows("changes")], ["currency"])

    def test_unknown_currency_is_not_guessed(self):
        run = self.run_fixture({1: [product()]}, currency=None)
        self.assertIsNone(run["currency"])
        self.assertIn("currency_unknown", [q["code"] for q in self.rows("quality")])

    def test_partial_failure_never_marks_missing(self):
        self.run_fixture({1: [product(1), product(2)]})
        result = self.run_fixture({1: [product(1)], 2: catalog.FetchError("synthetic timeout")})
        self.assertEqual(result["status"], "partial")
        self.assertTrue(all(p["observation_state"] == "observed" for p in self.rows("products")))
        self.assertEqual(self.rows("changes"), [])

    def test_complete_missing_means_only_not_observed(self):
        self.run_fixture({1: [product(1), product(2)]})
        self.run_fixture({1: [product(1)]})
        states = {p["product_id"]: p["observation_state"] for p in self.rows("products")}
        self.assertEqual(states, {"1": "observed", "2": "not_observed"})
        self.run_fixture({1: [product(1), product(2)]})
        self.assertTrue(all(p["observation_state"] == "observed" for p in self.rows("products")))

    def test_repeated_page_is_partial(self):
        result = self.run_fixture({1: [product()], 2: [product()]})
        self.assertEqual(result["status"], "partial")
        self.assertIn("repeated", result["error"])
        self.assertEqual(len(self.rows("snapshots")), 1)

    def test_duplicate_and_bad_records_disable_absence_detection(self):
        self.run_fixture({1: [product(1), product(2)]})
        bad = product(3)
        bad["variants"][0]["available"] = "yes"
        result = self.run_fixture({1: [product(1), product(1), bad]})
        self.assertEqual(result["status"], "partial")
        self.assertEqual({q["code"] for q in self.rows("quality")}, {"duplicate_product", "invalid_product"})
        self.assertTrue(all(p["observation_state"] == "observed" for p in self.rows("products")))

    def test_page_limit_is_partial(self):
        result = self.run_fixture({1: [product()]}, max_pages=1)
        self.assertEqual(result["status"], "partial")
        self.assertIn("max_pages", result["error"])

    def test_short_page_does_not_end_traversal(self):
        result = self.run_fixture({1: [product(1)], 2: [product(2)]}, limit=100)
        self.assertEqual(result["product_count"], 2)
        self.assertEqual(result["pages"], 3)

    def test_invalid_json_and_schema_fail_explicitly(self):
        for response in (b"<html>challenge</html>", b'{"products":null}'):
            result = self.run_fixture({1: response})
            self.assertEqual(result["status"], "failed")

    def test_robots_denial_stops_collection(self):
        result = self.run_fixture({1: [product()]}, deny=True)
        self.assertEqual(result["status"], "failed")
        self.assertIn("robots", result["error"])

    def test_mapping_cleaning_and_decimal_units(self):
        p = catalog.normalize(product(), catalog.DEFAULT_STORE, "CAD", "fixture")
        self.assertEqual(p["title"], "Tea Shirt")
        self.assertEqual(p["description"], "Soft & light Cotton")
        self.assertEqual(p["variants"][0]["options"], {"Color": "Blue", "Size": "M"})
        self.assertEqual(p["variants"][0]["price"], "12.30")
        self.assertEqual(p["images"], ["https://cdn.example.test/shirt.jpg"])
        self.assertNotIn("inventory_quantity", p["variants"][0])

    def test_rejects_invalid_money_and_ids(self):
        for value in ("NaN", "Infinity", "-1", 1.1, None, True, "oops"):
            with self.assertRaises(ValueError):
                catalog.money(value)
        for value in (None, True, "", 0, "abc"):
            with self.assertRaises(ValueError):
                catalog.identity(value)

    def test_duplicate_variant_and_missing_option_rejected(self):
        p = product()
        p["variants"].append(copy.deepcopy(p["variants"][0]))
        with self.assertRaises(ValueError):
            catalog.normalize(p, catalog.DEFAULT_STORE, None, None)
        p = product()
        p["variants"][0]["option2"] = None
        with self.assertRaises(ValueError):
            catalog.normalize(p, catalog.DEFAULT_STORE, None, None)

    def test_foreign_product_variant_rejected(self):
        p = product()
        p["variants"][0]["product_id"] = 999
        with self.assertRaises(ValueError):
            catalog.normalize(p, catalog.DEFAULT_STORE, None, None)

    def test_csv_formula_text_is_escaped_without_changing_json(self):
        import csv
        p = product()
        p["title"] = "=1+1"
        self.run_fixture({1: [p]})
        output = Path(self.temp.name) / "export"
        bundle = catalog.export(self.db_path, output)
        with (output / "variants.csv").open(encoding="utf-8-sig", newline="") as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row["title"], "'=1+1")
        self.assertEqual(bundle["products"][0]["title"], "=1+1")

    def test_export_provenance_and_report_escape(self):
        p = product()
        p["title"] = "<script>alert(1)</script>"
        self.run_fixture({1: [p]})
        output = Path(self.temp.name) / "export"
        report_path = catalog.report(self.db_path, output)
        bundle = json.loads((output / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(bundle["products"][0]["source_kind"], "synthetic_fixture")
        self.assertIn("&lt;script&gt;", report_path.read_text(encoding="utf-8"))
        self.assertNotIn("<script>", report_path.read_text(encoding="utf-8"))
        self.assertTrue((output / "variants.csv").read_bytes().startswith(b"\xef\xbb\xbf"))


class HTTPTests(unittest.TestCase):
    def test_homepage_is_hash_only(self):
        with ScratchDirectory() as temp:
            db = catalog.connect(Path(temp) / "synthetic.sqlite")
            run = db.execute("INSERT INTO runs(store,started_at,status,source_kind,network) VALUES('fixture',?,'running','synthetic_fixture','mock')", (catalog.now(),)).lastrowid
            client = catalog.HTTPClient(db, run, Path(temp) / "raw", delay=0)
            response = io.BytesIO(b'<html>synthetic homepage</html>')
            response.status, response.url = 200, "https://fixture.test/"
            with patch.object(client.opener, "open", return_value=response):
                body = client.get(response.url)
            row = db.execute("SELECT * FROM requests").fetchone()
            self.assertEqual(row["sha256"], hashlib.sha256(body).hexdigest())
            self.assertIsNone(row["raw_path"])
            self.assertEqual(list((Path(temp) / "raw").iterdir()), [])
            db.close()

    def test_retry_and_raw_hash_evidence(self):
        with ScratchDirectory() as temp:
            db = catalog.connect(Path(temp) / "synthetic.sqlite")
            run = db.execute("INSERT INTO runs(store,started_at,status,source_kind,network) VALUES('fixture',?,'running','synthetic_fixture','mock')", (catalog.now(),)).lastrowid
            client = catalog.HTTPClient(db, run, Path(temp) / "raw", delay=0, retries=1)
            response = io.BytesIO(b'{"products":[]}')
            response.status, response.url = 200, "https://fixture.test/products.json"
            error = urllib.error.HTTPError(response.url, 503, "Unavailable", {}, io.BytesIO(b"unavailable"))
            with patch.object(client.opener, "open", side_effect=[error, response]) as opener, patch("catalog.time.sleep"):
                body = client.get(response.url)
            self.assertEqual(opener.call_count, 2)
            rows = db.execute("SELECT * FROM requests ORDER BY id").fetchall()
            self.assertEqual([r["status"] for r in rows], [503, 200])
            self.assertEqual(rows[-1]["sha256"], hashlib.sha256(body).hexdigest())
            self.assertEqual(gzip.decompress((Path(temp) / rows[-1]["raw_path"]).read_bytes()), body)
            db.close()

    def test_403_not_retried(self):
        with ScratchDirectory() as temp:
            db = catalog.connect(Path(temp) / "synthetic.sqlite")
            run = db.execute("INSERT INTO runs(store,started_at,status,source_kind,network) VALUES('fixture',?,'running','synthetic_fixture','mock')", (catalog.now(),)).lastrowid
            client = catalog.HTTPClient(db, run, Path(temp) / "raw", delay=0)
            error = urllib.error.HTTPError("https://fixture.test/", 403, "Forbidden", {}, io.BytesIO(b"forbidden"))
            with patch.object(client.opener, "open", side_effect=error) as opener:
                with self.assertRaises(catalog.FetchError):
                    client.get("https://fixture.test/")
            self.assertEqual(opener.call_count, 1)
            db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
