"""Offline consistency audit of the shipped live evidence. Makes no HTTP requests."""
import argparse
import csv
from contextlib import closing
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3


def verify(db_path, export_dir):
    with closing(sqlite3.connect(db_path)) as db:
        db.row_factory = sqlite3.Row
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        runs = [dict(r) for r in db.execute("SELECT * FROM runs ORDER BY id")]
        assert len(runs) >= 2, "Need two actual runs for repeatability evidence"
        assert all(r["status"] == "complete" and r["source_kind"] == "public_demo" for r in runs)
        snapshots = list(db.execute("SELECT * FROM snapshots"))
        products = list(db.execute("SELECT * FROM products"))
        for row in snapshots + products:
            assert hashlib.sha256(row["data"].encode()).hexdigest() == row["content_hash"]
        for run in runs:
            observed = [json.loads(r["data"]) for r in snapshots if r["run_id"] == run["id"]]
            assert len(observed) == run["product_count"]
            assert sum(len(p["variants"]) for p in observed) == run["variant_count"]
        requests = list(db.execute("SELECT * FROM requests"))
        retained = 0
        for row in requests:
            assert row["status"] == 200 and row["sha256"] and not row["error"]
            if row["raw_path"]:
                raw_path = Path(db_path).parent / row["raw_path"]
                body = gzip.decompress(raw_path.read_bytes())
                assert len(body) == row["byte_count"]
                assert hashlib.sha256(body).hexdigest() == row["sha256"]
                retained += 1
        latest_ids = {r["product_id"] for r in snapshots if r["run_id"] == runs[-1]["id"]}
        prior_ids = {r["product_id"] for r in snapshots if r["run_id"] == runs[-2]["id"]}
        assert latest_ids == prior_ids
        assert {r["product_id"]: r["content_hash"] for r in snapshots if r["run_id"] == runs[-1]["id"]} == {r["product_id"]: r["content_hash"] for r in snapshots if r["run_id"] == runs[-2]["id"]}
        assert len(products) == len(latest_ids)
        assert db.execute("SELECT count(*) FROM changes").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM quality").fetchone()[0] == 0
        with (Path(export_dir) / "variants.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == runs[-1]["variant_count"]
        assert len({(r["store"], r["product_id"], r["variant_id"]) for r in rows}) == len(rows)
        exported = json.loads((Path(export_dir) / "catalog.json").read_text(encoding="utf-8"))
        assert len(exported["products"]) == len(products)
        assert exported["runs"] == runs
        result = {"audit": "PASS", "scope": "shipped public-demo evidence; zero live field changes",
                  "runs": runs, "unique_products": len(products), "snapshots": len(snapshots),
                  "csv_variant_rows": len(rows), "requests_hashed": len(requests),
                  "raw_responses_verified": retained, "homepage_policy": "hash-only; HTML not retained",
                  "quality_events": 0, "field_changes": 0,
                  "checks": ["SQLite integrity and foreign keys", "snapshot/content SHA-256",
                             "gzip response SHA-256 and sizes", "run counts vs snapshots",
                             "repeat identity sets and normalized content hashes", "CSV uniqueness and counts", "JSON/database agreement"]}
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/catalog.sqlite")
    parser.add_argument("--exports", default="exports")
    parser.add_argument("--output", default="evidence/verification.json")
    args = parser.parse_args()
    result = verify(args.db, args.exports)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
