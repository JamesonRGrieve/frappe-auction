# frappe-auction

Sell ERPNext items by auction from a Frappe v16 site. Each **Auction** sells one lot (or, for Dutch and Uniform
Price, a number of units) in one of five formats and has its own public web page. Every published auction that is open or coming up
is listed at `/auctions`.

| Format | How it works | Who wins, at what price |
|---|---|---|
| **English** (open, ascending) | Bidders enter the most they will pay. The page shows the price, not the maximums. A bid in the last few minutes moves the end back ("Extend on Late Bid"). | The highest maximum wins and pays one increment over the runner-up's maximum, never more than their own maximum. |
| **Dutch** (descending) | The price starts at the start price and drops by "Price Drop per Step" every "Step Interval" down to the floor. Buyers take units at the price of that moment. | Each buyer pays the clock price at the moment they buy, until the units run out or the auction ends. |
| **Sealed** (blind) | Each bidder submits a bid nobody else can see. A bidder may raise their own bid. | The highest bid wins. **First Price**: the winner pays their own bid. **Second Price** (Vickrey): the winner pays the runner-up's bid, or the start price or reserve if higher. |
| **Uniform Price** (blind, several units) | Each bidder bids a price a unit and how many whole units they want. Nobody sees another's bid. | The highest bids take units until they run out (the last may get fewer than asked). Every winner pays the same price: the lowest winning bid. |
| **Best Offer** | A fixed asking price (the Buy It Now price). Buyers can pay it at once, or offer less. A new offer replaces the buyer's pending one. | Offers at or above "Auto-Accept" sell at once and offers below "Auto-Decline" are declined at once. Staff answer the rest with Accept or Decline on the Auction Bid. Offers still pending at the end expire. |

**Buy It Now** (English, Sealed and Best Offer) ends the auction at a fixed price. On English it lasts until the
first bid, or, with a reserve, until the reserve is met. On Sealed it lasts until the close.

Equal bids go to whoever bid first. A **reserve** (English and Sealed) is never shown. When an English leader's
maximum reaches it, the price rises to the reserve. If no bid reaches it, the lot goes unsold.

## Settlement

When an English, Sealed or Uniform Price auction ends (checked every minute), or a buyer buys (Dutch, Buy It
Now) or has an offer accepted, the app:

1. creates and submits a **Sales Order** for each buyer's Customer, at the auction price, with the auction's
   Sales Taxes and Charges Template;
2. creates a **Payment Request** and emails it to the buyer, if the auction names a Payment Gateway Account.
   With no gateway, staff collect payment from the Sales Order.

The winner pays after the close. Payment isn't authorized when the bid is placed.

A buyer must be signed in. Their Customer is the one their account is a portal user of. With the webshop
installed, a buyer with no Customer gets one the way the webshop's checkout does it. Otherwise the bid is refused
with "Your account can't buy yet".

If settlement fails (a disabled item, a missing account), the auction stays Open, the error goes in
**Settlement Error** and the Error Log, and the scheduler stops retrying it. Fix the cause and clear the field;
the next minute settles it.

## Rules that hold

- Bids are judged one at a time: each write to an auction locks its row (`SELECT … FOR UPDATE`).
- Once anyone has bid, the auction's terms (item, quantity, format, prices, start) can't change.
- The web page and `auction.api.get_state` never reveal another bidder's identity, maximum or offer, or any
  Sealed or Uniform Price bid before the close. `Auction Bid` records are readable only by System Manager and Sales Manager.

## Model

| File | What |
|---|---|
| `auction/rules.py` | The five formats as pure functions over bids: standings, minimum bids, anti-sniping, the Dutch clock, sealed pricing, uniform-price allocation, Buy It Now availability, offer thresholds. |
| `auction/engine.py` | Applies the rules to the database: `place_bid`, `accept_price`, `buy_now`, `make_offer`, `accept_offer`, `decline_offer`, `close`, `settle`, the per-minute `open_and_close_due` and `public_state`. |
| `auction/api.py` | The whitelisted methods the web page calls, and the seller's offer answers (Sales Manager, System Manager). |
| `Auction` | A WebsiteGenerator: the lot, its format and terms, schedule, settlement settings, and status. Route `auctions/<title>-<id>`. |
| `Auction Bid` | One bid, purchase (Dutch, Buy It Now) or offer, written only by the engine. |

## Testing

```sh
python3 -m unittest auction.tests.test_rules      # the pure rules, no bench
BENCH_IMAGES=<dir> tools/test-bench.sh up          # a throwaway ERPNext v16 bench in rootless podman
tools/test-bench.sh test                           # the whole suite, on the working tree as it is now
tools/test-bench.sh down
```

Run the bench on a build runner, never a desktop. `BENCH_PORT_BASE` picks the three ports it uses (MariaDB, Redis,
desk). `BENCH_IMAGES` names a directory of saved images (`erpnext_v16.36.1.tar`, `mariadb_11.8.tar`,
`redis_7-alpine.tar`); on the build runners they are in `/var/cache/runner/images/erpnext-v16.36.1`. Without it
podman pulls the images itself.

## Not done yet

- Payment Requests are written but not exercised by a test (that needs a payment gateway on the test bench).
- No second-chance offer to the runner-up when a winner doesn't pay.
- Staff can read sealed bids in the desk before the close.
- A buyer isn't emailed when their offer is declined; the auction page shows it.

## License

AGPL-3.0-or-later.
