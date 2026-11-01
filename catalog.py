"""Public Shopify catalog evidence collector. Python 3.11+, standard library only."""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import html
from html.parser import HTMLParser
import json
import re
import sqlite3
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_STORE = "https://theme-dawn-demo.myshopify.com"
AGENT = "CatalogProof/1.0"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value).hexdigest()


def clean(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in ("p", "br", "div", "li", "h1", "h2"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        else:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain(value):
    parser = PlainText()
    parser.feed(str(value or ""))
    return clean("".join(parser.parts))


def money(value, optional=False):
    if optional and value in (None, ""):
        return None
    if isinstance(value, (bool, float)) or value is None:
        raise ValueError("money must be a decimal string or integer")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid decimal amount") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("amount must be finite and nonnegative")
    return format(amount, "f")


def identity(value):
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise ValueError("missing or invalid numeric ID")
    return str(value)


def image_url(value, store):
    if not value:
        return None
    url = urllib.parse.urljoin(store + "/", str(value))
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.username:
        raise ValueError("invalid image URL")
    return url


def normalize(product, store, currency, currency_source):
    if not isinstance(product, dict):
        raise ValueError("product must be an object")
    pid = identity(product.get("id"))
    title, handle = clean(product.get("title")), clean(product.get("handle"))
    if not title or not handle:
        raise ValueError("missing title or handle")
    options = product.get("options", [])
    if not isinstance(options, list) or len(options) > 3:
        raise ValueError("invalid option definitions")
    names = {}
    for index, option in enumerate(options, 1):
        if not isinstance(option, dict):
            raise ValueError("option must be an object")
        position = option.get("position", index)
        name = clean(option.get("name"))
        if type(position) is not int or position not in (1, 2, 3) or position in names or not name:
            raise ValueError("invalid option position or name")
        if name in names.values():
            raise ValueError("duplicate option name")
        names[position] = name
    variants = product.get("variants")
    if not isinstance(variants, list) or not variants:
        raise ValueError("missing variants")
    normalized, seen = [], set()
    for variant in variants:
        if not isinstance(variant, dict):
            raise ValueError("variant must be an object")
        vid = identity(variant.get("id"))
        if variant.get("product_id") is not None and identity(variant["product_id"]) != pid:
            raise ValueError("variant references a different product")
        if vid in seen:
            raise ValueError("duplicate variant ID")
        seen.add(vid)
        if type(variant.get("available")) is not bool:
            raise ValueError("available must be a boolean; it is not inventory quantity")
        mapped = {}
        for position, name in sorted(names.items()):
            option_value = clean(variant.get(f"option{position}"))
            if not option_value:
                raise ValueError("missing variant option value")
            mapped[name] = option_value
        if any(variant.get(f"option{i}") is not None and i not in names for i in (1, 2, 3)):
            raise ValueError("variant option lacks a definition")
        featured = variant.get("featured_image") or {}
        if not isinstance(featured, dict):
            raise ValueError("invalid variant image")
        normalized.append({"id": vid, "sku": clean(variant.get("sku")),
                           "title": clean(variant.get("title")), "options": mapped,
                           "price": money(variant.get("price")),
                           "compare_at_price": money(variant.get("compare_at_price"), True),
                           "available": variant["available"],
                           "image_url": image_url(featured.get("src"), store)})
    images = product.get("images", [])
    if not isinstance(images, list) or any(not isinstance(i, dict) for i in images):
        raise ValueError("invalid images")
    return {"id": pid, "title": title, "handle": handle, "vendor": clean(product.get("vendor")),
            "product_type": clean(product.get("product_type")),
            "description": plain(product.get("body_html")),
            "currency": currency, "currency_source": currency_source,
            "source_url": store + "/products/" + urllib.parse.quote(handle, safe=""),
            "images": list(dict.fromkeys(image_url(i.get("src"), store) for i in images if i.get("src"))),
            "variants": sorted(normalized, key=lambda v: v["id"])}


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS runs (
 id INTEGER PRIMARY KEY, store TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
 status TEXT NOT NULL, source_kind TEXT NOT NULL, network TEXT NOT NULL,
 currency TEXT, currency_source TEXT, pages INTEGER DEFAULT 0,
 product_count INTEGER DEFAULT 0, variant_count INTEGER DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS products (
 store TEXT NOT NULL, product_id TEXT NOT NULL, data TEXT NOT NULL, content_hash TEXT NOT NULL,
 first_seen_run INTEGER NOT NULL REFERENCES runs(id), last_seen_run INTEGER NOT NULL REFERENCES runs(id),
 observation_state TEXT NOT NULL, PRIMARY KEY(store, product_id));
CREATE TABLE IF NOT EXISTS snapshots (
 run_id INTEGER NOT NULL REFERENCES runs(id), product_id TEXT NOT NULL, data TEXT NOT NULL,
 content_hash TEXT NOT NULL, PRIMARY KEY(run_id, product_id));
CREATE TABLE IF NOT EXISTS changes (
 id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id), product_id TEXT NOT NULL,
 variant_id TEXT, field TEXT NOT NULL, before_value TEXT, after_value TEXT);
CREATE TABLE IF NOT EXISTS requests (
 id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id), url TEXT NOT NULL,
 attempted_at TEXT NOT NULL, status INTEGER, attempt INTEGER, elapsed_ms INTEGER,
 byte_count INTEGER, sha256 TEXT, raw_path TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS quality (
 id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id), code TEXT NOT NULL,
 detail TEXT NOT NULL);
"""


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def quality(db, run, code, detail):
    db.execute("INSERT INTO quality(run_id,code,detail) VALUES(?,?,?)", (run, code, str(detail)))
    db.commit()


class FetchError(Exception):
    pass


class HTTPClient:
    def __init__(self, db, run, evidence_dir, network="direct", delay=1.0, retries=2):
        self.db, self.run = db, run
        self.evidence_dir = Path(evidence_dir)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if network == "direct" else urllib.request.build_opener()
        self.delay, self.retries, self.last_request = delay, retries, None

    def get(self, url):
        for attempt in range(1, self.retries + 2):
            if self.last_request is not None:
                time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            started, stamp = time.monotonic(), now()
            self.last_request = started
            body, status, error, retry_after = b"", None, None, None
            try:
                request = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept": "application/json,text/html,text/plain;q=0.9"})
                with self.opener.open(request, timeout=30) as response:
                    # An unexpectedly large response is a failure, never a silently truncated catalog.
                    body = response.read(20_000_001)
                    status = response.status
                    if len(body) > 20_000_000:
                        raise FetchError("response exceeds 20 MB limit")
                    if urllib.parse.urlsplit(response.url).netloc != urllib.parse.urlsplit(url).netloc:
                        raise FetchError("cross-host redirect rejected")
            except urllib.error.HTTPError as exc:
                status, body, error = exc.code, exc.read(20_000_001), f"HTTP {exc.code}"
                retry_after = exc.headers.get("Retry-After")
            except (urllib.error.URLError, TimeoutError, OSError, FetchError) as exc:
                error = str(exc)
            cursor = self.db.execute("INSERT INTO requests(run_id,url,attempted_at,status,attempt,elapsed_ms,byte_count,sha256,error) VALUES(?,?,?,?,?,?,?,?,?)",
                                     (self.run, url, stamp, status, attempt, int((time.monotonic()-started)*1000), len(body), digest(body), error))
            # Homepages can embed ephemeral storefront/browser tokens. Keep only their
            # hash/size and extracted currency, never their HTML in a shareable package.
            raw_path = None
            if urllib.parse.urlsplit(url).path in ("/products.json", "/robots.txt"):
                raw_path = f"raw/request-{cursor.lastrowid:06d}.gz"
                with gzip.open(self.evidence_dir / Path(raw_path).name, "wb") as stream:
                    stream.write(body)
            self.db.execute("UPDATE requests SET raw_path=? WHERE id=?", (raw_path, cursor.lastrowid))
            self.db.commit()
            if error is None:
                return body
            retryable = status in (429, 500, 502, 503, 504) or status is None
            if not retryable or attempt > self.retries:
                raise FetchError(f"{url}: {error}")
            backoff = min(30, 2 ** (attempt - 1))
            if retry_after:
                try:
                    wait = float(retry_after)
                    if wait > 60:
                        raise FetchError("Retry-After exceeds 60s; stop and retry a later run")
                    backoff = max(backoff, wait)
                except ValueError:
                    # HTTP dates are valid too; stop instead of violating a server-requested pause.
                    raise FetchError("non-numeric Retry-After; stop and retry a later run")
            time.sleep(backoff)
        raise AssertionError("unreachable")


def persist(db, run, store, product):
    payload = encoded(product)
    hashed = digest(payload.encode("utf-8"))
    old = db.execute("SELECT data FROM products WHERE store=? AND product_id=?", (store, product["id"])).fetchone()
    if old:
        previous = json.loads(old["data"])
        old_variants = {v["id"]: v for v in previous["variants"]}
        for variant in product["variants"]:
            prior = old_variants.get(variant["id"])
            if prior:
                fields = ["available"]
                # Comparing amounts across different or unknown currencies would be misleading.
                if product["currency"] and previous["currency"] == product["currency"]:
                    fields += ["price", "compare_at_price"]
                for field in fields:
                    before, after = prior[field], variant[field]
                    same = before == after
                    if field in ("price", "compare_at_price") and before is not None and after is not None:
                        same = Decimal(before) == Decimal(after)
                    if not same:
                        db.execute("INSERT INTO changes(run_id,product_id,variant_id,field,before_value,after_value) VALUES(?,?,?,?,?,?)",
                                   (run, product["id"], variant["id"], field, encoded(before), encoded(after)))
        if previous["currency"] != product["currency"]:
            db.execute("INSERT INTO changes(run_id,product_id,field,before_value,after_value) VALUES(?,?,?,?,?)",
                       (run, product["id"], "currency", encoded(previous["currency"]), encoded(product["currency"])))
    db.execute("INSERT INTO snapshots VALUES(?,?,?,?)", (run, product["id"], payload, hashed))
    db.execute("""INSERT INTO products VALUES(?,?,?,?,?,?, 'observed')
        ON CONFLICT(store,product_id) DO UPDATE SET data=excluded.data, content_hash=excluded.content_hash,
        last_seen_run=excluded.last_seen_run, observation_state='observed'""",
               (store, product["id"], payload, hashed, run, run))


def collect(db_path, store=DEFAULT_STORE, network="direct", delay=1, retries=2,
            limit=100, max_pages=100, source_kind="public_demo", client_factory=None):
    parsed = urllib.parse.urlsplit(store)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("store must be an HTTPS origin without credentials, path, query, or fragment")
    if not 1 <= limit <= 250 or max_pages < 1 or delay < 0 or not 0 <= retries <= 5:
        raise ValueError("invalid pagination/rate/retry settings")
    store = store.rstrip("/")
    db = connect(db_path)
    run = db.execute("INSERT INTO runs(store,started_at,status,source_kind,network) VALUES(?,?,'running',?,?)", (store, now(), source_kind, network)).lastrowid
    db.commit()
    client = (client_factory(db, run) if client_factory else HTTPClient(db, run, Path(db_path).parent / "raw", network, delay, retries))
    seen, page_hashes, pages, variant_count = set(), set(), 0, 0
    complete, compromised, error = False, False, None
    currency, currency_source = None, None
    try:
        robots_url = store + "/robots.txt"
        robots_body = client.get(robots_url).decode("utf-8", errors="replace")
        robots = urllib.robotparser.RobotFileParser()
        robots.parse(robots_body.splitlines())
        catalog_url = store + f"/products.json?limit={limit}&page=1"
        if not robots.can_fetch(AGENT, catalog_url) or not robots.can_fetch(AGENT, store + "/"):
            raise FetchError("robots.txt does not permit required catalog/homepage paths")
        crawl_delay = robots.crawl_delay(AGENT)
        if crawl_delay and hasattr(client, "delay"):
            client.delay = max(client.delay, crawl_delay)
        try:
            homepage = client.get(store + "/").decode("utf-8", errors="replace")
            match = re.search(r'Shopify\.currency\s*=\s*(\{[^{}]*\})', homepage)
            if match:
                active = json.loads(match.group(1)).get("active")
                if isinstance(active, str) and re.fullmatch(r"[A-Z]{3}", active):
                    currency, currency_source = active, store + "/ (Shopify.currency.active)"
        except (FetchError, ValueError) as exc:
            quality(db, run, "currency_lookup_failed", exc)
        if not currency:
            quality(db, run, "currency_unknown", "No reliable currency declaration; no currency guessed")
        db.execute("UPDATE runs SET currency=?,currency_source=? WHERE id=?", (currency, currency_source, run))
        db.commit()
        for page in range(1, max_pages + 1):
            url = store + f"/products.json?limit={limit}&page={page}"
            if not robots.can_fetch(AGENT, url):
                raise FetchError("robots.txt disallows pagination URL")
            body = client.get(url)
            try:
                document = json.loads(body)
            except (ValueError, UnicodeDecodeError) as exc:
                raise FetchError("catalog response is not valid JSON") from exc
            if not isinstance(document, dict) or not isinstance(document.get("products"), list):
                raise FetchError("catalog response lacks a products array")
            records = document["products"]
            pages += 1
            if not records:
                complete = True
                break
            fingerprint = digest(encoded(records).encode("utf-8"))
            if fingerprint in page_hashes:
                raise FetchError("repeated nonempty page; pagination may be ignored")
            page_hashes.add(fingerprint)
            for record in records:
                try:
                    product = normalize(record, store, currency, currency_source)
                    if product["id"] in seen:
                        quality(db, run, "duplicate_product", f"page={page}; id={product['id']}")
                        compromised = True
                        continue
                    with db:
                        persist(db, run, store, product)
                    seen.add(product["id"])
                    variant_count += len(product["variants"])
                except (ValueError, TypeError, KeyError) as exc:
                    compromised = True
                    quality(db, run, "invalid_product", f"page={page}; {exc}")
        if not complete:
            error = "max_pages reached before an empty terminal page"
    except (FetchError, ValueError, OSError) as exc:
        error = str(exc)
        quality(db, run, "collection_error", error)
    except KeyboardInterrupt:
        error = "interrupted by operator"
        quality(db, run, "interrupted", error)
    status = "complete" if complete and not compromised else ("partial" if seen or pages else "failed")
    if compromised and not error:
        error = "record quality issues; absence detection disabled"
    with db:
        if status == "complete":
            missing = db.execute("SELECT product_id FROM products WHERE store=? AND last_seen_run<>? AND observation_state='observed'", (store, run)).fetchall()
            for row in missing:
                db.execute("INSERT INTO changes(run_id,product_id,field,before_value,after_value) VALUES(?,?,?,?,?)", (run, row["product_id"], "observation_state", encoded("observed"), encoded("not_observed")))
            db.execute("UPDATE products SET observation_state='not_observed' WHERE store=? AND last_seen_run<>?", (store, run))
        db.execute("UPDATE runs SET ended_at=?,status=?,pages=?,product_count=?,variant_count=?,error=? WHERE id=?", (now(), status, pages, len(seen), variant_count, error, run))
    result = dict(db.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone())
    db.close()
    return result


def export(db_path, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    db = connect(db_path)
    bundle = {"generated_at": now(), "amount_unit": "decimal major units from public catalog strings",
              "inventory_quantity": "not collected; available is a boolean only",
              "runs": [dict(r) for r in db.execute("SELECT * FROM runs ORDER BY id")],
              "products": [], "changes": [dict(r) for r in db.execute("SELECT * FROM changes ORDER BY id")],
              "quality": [dict(r) for r in db.execute("SELECT * FROM quality ORDER BY id")],
              "requests": [dict(r) for r in db.execute("SELECT * FROM requests ORDER BY id")]}
    fields = ["store", "product_id", "variant_id", "title", "sku", "options", "price", "compare_at_price", "currency", "currency_source", "available", "image_url", "source_url", "observation_state", "first_seen_run", "last_seen_run", "collected_at", "source_kind"]
    runs = {r["id"]: r for r in bundle["runs"]}
    with (output / "variants.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in db.execute("SELECT * FROM products ORDER BY store,product_id"):
            product = json.loads(row["data"])
            provenance = {key: row[key] for key in ("store", "first_seen_run", "last_seen_run", "observation_state", "content_hash")}
            provenance.update(collected_at=runs[row["last_seen_run"]]["started_at"], source_kind=runs[row["last_seen_run"]]["source_kind"])
            bundle["products"].append(dict(product, **provenance))
            for variant in product["variants"]:
                csv_row = {"store": row["store"], "product_id": product["id"], "variant_id": variant["id"],
                                 "title": product["title"], "sku": variant["sku"], "options": encoded(variant["options"]),
                                 "price": variant["price"], "compare_at_price": variant["compare_at_price"],
                                 "currency": product["currency"], "currency_source": product["currency_source"],
                                 "available": str(variant["available"]).lower(), "image_url": variant["image_url"] or (product["images"][0] if product["images"] else None),
                                 "source_url": product["source_url"], **{k: provenance[k] for k in fields if k in provenance}}
                # Preserve canonical JSON; prevent untrusted text from becoming spreadsheet formulas.
                writer.writerow({key: ("'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")) else value) for key, value in csv_row.items()})
    (output / "catalog.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    db.close()
    return bundle


def report(db_path, output):
    bundle = export(db_path, output)
    esc = lambda value: html.escape(str(value))
    runs, products = bundle["runs"], bundle["products"]
    latest = runs[-1] if runs else {}
    latest_products = [p for p in products if p["last_seen_run"] == latest.get("id")]
    available = sum(v["available"] for p in latest_products for v in p["variants"])
    rows = []
    for p in latest_products[:7]:
        prices = [Decimal(v["price"]) for v in p["variants"]]
        rows.append(f'<tr><td><a href="{esc(p["source_url"])}">{esc(p["title"])}</a><small>{esc(p["id"])} · {esc(p["vendor"])}</small></td><td>{len(p["variants"])}</td><td>{min(prices):,.2f} {esc(p["currency"] or "UNKNOWN")}</td><td>{sum(v["available"] for v in p["variants"])}/{len(p["variants"])} available</td></tr>')
    history = "".join(f'<div class="run"><b>Run {r["id"]} <span class="pill {esc(r["status"])}">{esc(r["status"])}</span></b><span>{r["product_count"]} products · {r["variant_count"]} variants · {r["pages"]} pages</span><small>{esc(r["started_at"])}{(" · " + esc(r["error"])) if r["error"] else ""}</small></div>' for r in runs[-3:])
    requests = bundle["requests"]
    hashes = sum(bool(r["sha256"]) for r in requests)
    source = latest.get("source_kind", "no data")
    changes_latest = sum(c["run_id"] == latest.get("id") for c in bundle["changes"])
    page = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Shopify Catalog Proof</title><style>
    *{box-sizing:border-box}body{margin:0;background:#f4f6f8;color:#15283a;font:15px/1.5 'Segoe UI',sans-serif}main{max-width:1300px;margin:0 auto;padding:32px 38px}.top{display:flex;justify-content:space-between;align-items:center}.brand{font-weight:700;letter-spacing:2px;font-size:12px;color:#397768}.tag{border:1px solid #b8ceca;border-radius:20px;padding:6px 13px;color:#286956;font-size:12px}h1{font-size:36px;letter-spacing:-1px;margin:14px 0 6px}p{color:#536779;margin:0}a{color:#176d63;text-decoration:none}a:hover{text-decoration:underline}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:25px 0}.card,.panel{background:white;border:1px solid #e0e7eb;border-radius:12px}.card{padding:17px 20px}.card strong{display:block;font-size:31px;line-height:1.2}.card span{font-size:12px;color:#647687}.card label{display:block;color:#4e6374;margin-bottom:6px;font-size:13px}.grid{display:grid;grid-template-columns:1.8fr 1fr;gap:18px}.panel{padding:20px}h2{font-size:16px;margin:0 0 14px}table{border-collapse:collapse;width:100%;font-size:13px}th{text-align:left;font-size:11px;text-transform:uppercase;color:#718190;letter-spacing:.6px;padding:0 8px 10px}td{padding:11px 8px;border-top:1px solid #eaf0f3}small{display:block;font-size:11px;color:#718190}.run{padding:12px 0;border-top:1px solid #eaf0f3}.run:first-of-type{border-top:0}.run span:not(.pill){display:block;font-size:13px;margin:5px 0}.pill{display:inline-block;font-size:10px;border-radius:10px;padding:2px 8px;margin-left:7px;text-transform:uppercase;background:#e9f2ee;color:#276b51}.partial,.running{background:#fff1d8;color:#8b5c12}.failed{background:#ffe3df;color:#962e23}.note{margin-top:18px;background:#eaf1f5;padding:17px 20px;border-radius:10px;font-size:12px;color:#496073}.evidence{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;margin-top:18px}.evidence b{display:block;color:#223f51;font-size:13px}.footer{display:flex;justify-content:space-between;margin-top:18px;font-size:12px;color:#687c8b}.links a{margin-right:15px}@media(max-width:850px){.grid{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}main{padding:20px}.evidence{grid-template-columns:1fr}}
    </style><main><div class="top"><div class="brand">CATALOG / ENGINEERING PROOF</div><div class="tag">SOURCE_KIND</div></div><h1>Observable data. Repeatable collection.</h1><p>Public Shopify catalog → normalized variants → SQLite snapshots → auditable exports.</p><div class="stats">STATS</div><div class="grid"><section class="panel"><h2>Latest observed catalog <small>Sample of PRODUCTS products · prices shown as minimum per product</small></h2><table><thead><tr><th>Product / source ID</th><th>Variants</th><th>From price</th><th>Availability</th></tr></thead><tbody>ROWS</tbody></table></section><aside class="panel"><h2>Collection history</h2>HISTORY<div class="note"><b>LATEST_CHANGES field changes on latest run</b><br>Change detection compares variant price and availability. Unchanged observations create snapshots without duplicating product identities.</div></aside></div><div class="evidence"><div><b>HASHES / REQUESTS responses hashed</b>Catalog responses retained as gzip; homepage HTML is hash-only. Request URLs, attempts, HTTP status and SHA-256 are linked in the evidence export.</div><div><b>Currency: CURRENCY</b>Declared by Shopify.currency.active on the store homepage. Catalog decimal strings are kept in major units; no binary floats.</div><div><b>QUALITY quality events</b>Only an empty terminal page plus clean records permits absence detection. Missing items are “not_observed”, never confirmed delisted.</div></div><div class="note"><b>Scope & provenance</b><br>STORE · SOURCE_KIND_LABEL. This is a public theme demo catalog, not a merchant production-scale benchmark. “Complete” means this endpoint traversal reached an empty page, not a guarantee of all store products or variants. Availability is not inventory quantity or sales. Online results and synthetic test changes are kept in separate databases.</div><div class="footer"><div class="links"><a href="catalog.json">JSON + evidence</a><a href="variants.csv">Variant CSV</a></div><span>Generated GENERATED · UTC</span></div></main></html>"""
    stats = [(latest.get("product_count", 0), "Products observed", "Latest run, unique product IDs"),
             (latest.get("variant_count", 0), "Variants normalized", "Stable variant IDs + option mapping"),
             (available, "Available variants", "Boolean status · no stock estimates"),
             (len(runs), "Persisted runs", "Idempotent identities, separate snapshots")]
    mapping = {"SOURCE_KIND_LABEL": esc(source), "SOURCE_KIND": esc(source.replace("_", " ").upper()),
               "STATS": "".join(f'<div class="card"><label>{label}</label><strong>{value}</strong><span>{hint}</span></div>' for value, label, hint in stats),
               "PRODUCTS": str(latest.get("product_count", 0)), "ROWS": "".join(rows), "HISTORY": history,
               "LATEST_CHANGES": str(changes_latest), "HASHES": str(hashes), "REQUESTS": str(len(requests)),
               "CURRENCY": esc(latest.get("currency") or "UNKNOWN"), "QUALITY": str(len(bundle["quality"])),
               "STORE": esc(latest.get("store", "No collection yet")), "GENERATED": esc(bundle["generated_at"])}
    if source != "public_demo":
        page = page.replace("This is a public theme demo catalog, not a merchant production-scale benchmark.",
                            "Synthetic fixture data; no online collection." if source == "synthetic_fixture" else
                            "Public-store capture; no production-scale coverage claim.")
    if not latest.get("currency"):
        page = page.replace("Declared by Shopify.currency.active on the store homepage.",
                            "No reliable currency declaration found; currency is unknown.")
    for key in sorted(mapping, key=len, reverse=True):
        page = page.replace(key, mapping[key])
    path = Path(output) / "report.html"
    path.write_text(page, encoding="utf-8")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    collect_parser = sub.add_parser("collect")
    collect_parser.add_argument("--store", default=DEFAULT_STORE)
    collect_parser.add_argument("--db", default="data/catalog.sqlite")
    collect_parser.add_argument("--network", choices=("direct", "env"), default="direct")
    collect_parser.add_argument("--delay", type=float, default=1)
    collect_parser.add_argument("--retries", type=int, default=2)
    collect_parser.add_argument("--limit", type=int, default=100)
    collect_parser.add_argument("--max-pages", type=int, default=100)
    collect_parser.add_argument("--source-kind", choices=("public_demo", "public_store"), default=None)
    for command in ("export", "report"):
        command_parser = sub.add_parser(command)
        command_parser.add_argument("--db", default="data/catalog.sqlite")
        command_parser.add_argument("--output", default="exports")
    serve_parser = sub.add_parser("serve")
    serve_parser.add_argument("--directory", default="exports")
    serve_parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.command == "collect":
        kwargs = vars(args).copy()
        kwargs.pop("command")
        kwargs["db_path"] = kwargs.pop("db")
        kwargs["source_kind"] = kwargs["source_kind"] or ("public_demo" if args.store.rstrip("/") == DEFAULT_STORE else "public_store")
        result = collect(**kwargs)
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "complete" else 2
    if args.command == "export":
        bundle = export(args.db, args.output)
        print(f"Exported {len(bundle['products'])} product identities to {args.output}")
    elif args.command == "report":
        print(report(args.db, args.output))
    else:
        import functools
        handler = functools.partial(SimpleHTTPRequestHandler, directory=str(Path(args.directory).resolve()))
        print(f"Read-only report: http://127.0.0.1:{args.port}/report.html", flush=True)
        ThreadingHTTPServer(("127.0.0.1", args.port), handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
