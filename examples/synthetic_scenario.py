"""Deterministic synthetic change/failure demonstration. Does not access the network."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import catalog


def item(pid, price="12.30", available=True):
    return {"id": pid, "title": f"SYNTHETIC fixture shirt {pid}", "handle": f"synthetic-shirt-{pid}",
            "body_html": "<p>Synthetic fixture only, not a live merchant product.</p>",
            "vendor": "Synthetic Fixture", "options": [{"name": "Color", "position": 1}],
            "variants": [{"id": pid * 10, "price": price, "available": available, "option1": "Blue"}],
            "images": []}


class SyntheticClient:
    def __init__(self, products, fail=False):
        self.products, self.fail = products, fail

    def get(self, url):
        if url.endswith("robots.txt"):
            return b"User-agent: *\nAllow: /"
        if url.endswith("/"):
            return b'Shopify.currency = {"active":"CAD"}'
        if url.endswith("page=1"):
            return json.dumps({"products": self.products}).encode()
        if self.fail:
            raise catalog.FetchError("SYNTHETIC injected page-2 timeout; no network request")
        return b'{"products":[]}'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="work/synthetic-demo")
    args = parser.parse_args()
    directory = Path(args.output)
    path = directory / "synthetic.sqlite"
    if path.exists():
        parser.error("output database already exists; choose a new --output directory")
    trace = []
    scenarios = [("baseline", [item(1), item(2)], False),
                 ("unchanged repeat", [item(1), item(2)], False),
                 ("injected price + availability change", [item(1, "14.50", False), item(2)], False),
                 ("injected partial traversal; item 2 stays observed", [item(1, "14.50", False)], True)]
    for name, products, fail in scenarios:
        run = catalog.collect(path, store="https://synthetic.example.test", network="synthetic_no_network",
                              source_kind="synthetic_fixture", delay=0,
                              client_factory=lambda db, run: SyntheticClient(products, fail))
        trace.append({"scenario": name, **run})
    bundle = catalog.export(path, directory)
    assert len(bundle["products"]) == 2
    assert [r["status"] for r in trace] == ["complete", "complete", "complete", "partial"]
    assert {c["field"] for c in bundle["changes"]} == {"price", "available"}
    assert len(bundle["changes"]) == 2
    assert all(p["observation_state"] == "observed" for p in bundle["products"])
    (directory / "trace.json").write_text(json.dumps({"source": "SYNTHETIC ONLY / NO NETWORK", "runs": trace, "assertions": "passed"}, indent=2), encoding="utf-8")
    print("SYNTHETIC ONLY: 4 runs; 2 field changes; partial omission did not change observation state.")
    print(path)


if __name__ == "__main__":
    main()
