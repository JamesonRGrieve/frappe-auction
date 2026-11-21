# frappe-auction — Agent Operating Guide

AGPL-3.0-or-later Frappe v16 app (Python 3.14): English, Dutch and sealed-bid auctions for ERPNext items, each
with its own web page, settled into submitted Sales Orders. Standards live in the SOP collectives (Zephyrex SOP
→ *Agent Operating Standards*); this file holds only what is specific to this repo. Card: Zephyrex Software #423.

**Not deployed anywhere.** Which site and store get it first is the operator's call.

## Model

- `rules.py`: pure rules, no frappe import. Every format decision lives here and is unit-tested
  (`tests/test_rules.py`). Money is `Decimal`.
- `engine.py`: the only writer of `Auction` status fields and `Auction Bid`. Every write locks the auction row
  first (`_locked`), then re-reads the bids. Status fields are written with `frappe.db.set_value` (`_update`),
  never `save()`, so `Auction.validate`'s locked-terms check doesn't run on engine writes.
- `settle()` runs as Administrator (`_as_administrator`): a Dutch buyer's request and the scheduler both create
  accounting documents the buyer may not.
- `public_state()` is the only thing the web page sees. Never add another bidder's identity or maximum, or a
  sealed amount before the close.
- `api.py` turns `BidRejected` into `frappe.throw`, so its message reaches the bidder and the transaction rolls
  back.

## Testing

- `tools/test-bench.sh pull|up|test|serve|down`: rootless podman with the root on local `/tmp`, host network,
  three ports from `BENCH_PORT_BASE`. Runner jobs follow the Build Runner Budget (15-minute cap, BUILD START/DONE
  to the slot manager).
- `tests/test_engine.py` runs on the real ERPNext test DB. `open_and_close_due` commits, so tests use their own
  lot item and clear its auctions in `setUp`. Engine calls take `now` so tests set the clock.
