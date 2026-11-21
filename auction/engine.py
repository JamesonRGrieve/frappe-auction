# SPDX-License-Identifier: AGPL-3.0-or-later
"""Applies ``auction.rules`` to auctions in the database: bids, Dutch and Buy It Now purchases, Best Offer
offers, card holds, the scheduled open and close, and settlement (``auction.settlement``).

Every write to an auction first locks its row (``SELECT … FOR UPDATE``), so two bids arriving together are
judged one after the other, each against the bids before it. The lock is held until the request's
transaction commits.

Each bid, purchase or offer is an Auction Bid row. On an auction that settles "After Close" a row takes
effect as soon as it is made. On one that holds payment ("Authorize at Bid") it waits, uncounted, until the
buyer's gateway reports the hold (``hold_authorized``), and is judged again then: the auction may have moved
while the buyer authorized.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, now_datetime

from auction import holds, rules, settlement
from auction.rules import Bid, BidRejected

if TYPE_CHECKING:
	from auction.auction.doctype.auction.auction import Auction

SCHEDULED = "Scheduled"
OPEN = "Open"
SOLD = "Sold"
UNSOLD = "Unsold"
CLOSED_STATUSES = (SOLD, UNSOLD)
DEFAULT_CURRENCY_PRECISION = 2

# Auction Bid types.
BID = "Bid"
BUY_NOW = "Buy Now"
OFFER = "Offer"
PURCHASE = "Purchase"
# The Auction Bid rows a format's page reads; the rest read bids.
ROW_TYPE = {rules.DUTCH: PURCHASE, rules.BEST_OFFER: OFFER}

# Payment timing.
AFTER_CLOSE = "After Close"
AUTHORIZE_AT_BID = "Authorize at Bid"
PAYMENT_TIMINGS = (AFTER_CLOSE, AUTHORIZE_AT_BID)

# Auction Bid hold statuses. A row awaiting authorization, or whose hold failed, doesn't count.
NOT_REQUIRED = "Not Required"
AWAITING = "Awaiting Authorization"
AUTHORIZED = "Authorized"
RELEASED = "Released"
CAPTURED = "Captured"
FAILED = "Failed"
UNCOUNTED_HOLDS = (AWAITING, FAILED)


def _precision() -> int:
	return cint(frappe.db.get_default("currency_precision")) or DEFAULT_CURRENCY_PRECISION


def money(value: Any) -> Decimal:
	"""A currency amount from the database or a request, rounded to the site's currency precision."""
	try:
		amount = Decimal(str(value).strip())
	except (InvalidOperation, AttributeError) as error:
		raise BidRejected(_("Enter an amount.")) from error
	if not amount.is_finite():
		raise BidRejected(_("Enter an amount."))
	return Decimal(str(flt(amount, _precision())))


def _optional_money(value: Any) -> Decimal | None:
	return money(value) if value else None


def _qty(value: Any) -> Decimal:
	return Decimal(str(flt(value)))


def _units(value: Any) -> Decimal:
	try:
		return Decimal(str(value).strip())
	except (InvalidOperation, AttributeError) as error:
		raise BidRejected(_("Choose at least one unit.")) from error


def _locked(name: str) -> Auction:
	if not frappe.db.sql("select name from `tabAuction` where name = %s for update", name):
		raise frappe.DoesNotExistError(_("Auction {0} not found").format(name))
	return frappe.get_doc("Auction", name)


ROW_FIELDS = [
	"name",
	"bid_type",
	"bidder",
	"customer",
	"amount",
	"qty",
	"placed_at",
	"offer_status",
	"is_winner",
	"units_won",
	"hold_status",
	"hold_reference",
	"hold_amount",
]


def _bid_rows(name: str, bid_type: str = BID) -> list[Any]:
	"""The rows of one type that count: every one but those awaiting or failing a hold."""
	return frappe.get_all(
		"Auction Bid",
		filters={"auction": name, "bid_type": bid_type, "hold_status": ["not in", UNCOUNTED_HOLDS]},
		fields=ROW_FIELDS,
		order_by="placed_at asc, creation asc",
	)


def _as_bid(row: Any) -> Bid:
	return Bid(bidder=row.bidder, amount=money(row.amount), placed=get_datetime(row.placed_at), qty=_qty(row.qty))


def _bids(name: str) -> list[Bid]:
	return [_as_bid(row) for row in _bid_rows(name)]


def _update(auction: Auction, **values: Any) -> None:
	frappe.db.set_value("Auction", auction.name, values)
	auction.update(values)


def is_open(auction: Auction, now: datetime) -> bool:
	return (
		bool(auction.published)
		and auction.status not in CLOSED_STATUSES
		and get_datetime(auction.starts_at) <= now < get_datetime(auction.ends_at)
	)


def _require_open(auction: Auction, now: datetime) -> None:
	if not is_open(auction, now):
		raise BidRejected(_("This auction isn't open."))


def holds_payment(auction: Auction) -> bool:
	return auction.payment_timing == AUTHORIZE_AT_BID


def customer_for(user: str) -> str:
	"""The Customer a signed-in buyer buys as: the one their account is a portal user of, or, with the webshop
	installed, the one the webshop finds or makes for them (as its checkout does)."""
	if not user or user == "Guest":
		raise BidRejected(_("Sign in to bid."))
	customers = frappe.get_all("Portal User", filters={"user": user, "parenttype": "Customer"}, pluck="parent")
	if customers:
		return customers[0]
	if "webshop" in frappe.get_installed_apps():
		from webshop.webshop.shopping_cart.cart import get_party

		party = get_party(user)
		if party and party.doctype == "Customer":
			return party.name
	raise BidRejected(_("Your account can't buy yet. Please contact us."))


def _terms(auction: Auction) -> dict[str, Any]:
	return {
		"start": money(auction.start_price or 0),
		"increment": money(auction.bid_increment or 0),
		"reserve": _optional_money(auction.reserve_price),
	}


def english_standing(auction: Auction, bids: list[Bid]) -> rules.Standing:
	terms = _terms(auction)
	return rules.english_standing(bids, terms["start"], terms["increment"], terms["reserve"])


def sealed_standing(auction: Auction, bids: list[Bid]) -> rules.Standing:
	return rules.sealed_standing(
		bids, money(auction.start_price), _optional_money(auction.reserve_price), auction.sealed_pricing
	)


def dutch_price(auction: Auction, at: datetime) -> Decimal:
	return rules.dutch_price_at(
		money(auction.start_price),
		money(auction.floor_price),
		money(auction.price_step),
		timedelta(seconds=cint(auction.step_interval_seconds)),
		get_datetime(auction.starts_at),
		at,
	)


def units_left(auction: Auction) -> Decimal:
	return _qty(auction.quantity) - _qty(auction.units_sold)


# Checks. Each runs when a row is made, and again when its hold is authorized.


def _check(auction: Auction, row: Any) -> None:
	"""Refuses a row the auction can't take now, judged against the rows that count."""
	if row.bid_type == BID:
		bids = _bids(auction.name)
		bid = _as_bid(row)
		if auction.auction_type == rules.ENGLISH:
			terms = _terms(auction)
			rules.check_english_bid(bids, bid, terms["start"], terms["increment"], terms["reserve"])
		elif auction.auction_type == rules.UNIFORM:
			rules.check_uniform_bid(bids, bid, money(auction.start_price), _qty(auction.quantity))
		else:
			rules.check_sealed_bid(bids, bid, money(auction.start_price))
	elif row.bid_type == PURCHASE:
		rules.check_dutch_accept(units_left(auction), _qty(row.qty))
	elif row.bid_type == BUY_NOW:
		standing = (
			english_standing(auction, _bids(auction.name)) if auction.auction_type == rules.ENGLISH else rules.NO_BIDS
		)
		price = _optional_money(auction.buy_now_price)
		if price is None or not rules.buy_now_available(
			auction.auction_type, price, standing, _optional_money(auction.reserve_price)
		):
			raise BidRejected(_("Buy It Now isn't available on this auction now."))
	elif row.bid_type == OFFER:
		_offer_decision(auction, money(row.amount))
	else:
		raise ValueError(f"Unknown Auction Bid type {row.bid_type!r}")


def _offer_decision(auction: Auction, amount: Decimal) -> str:
	return rules.offer_decision(
		amount,
		money(auction.buy_now_price),
		_optional_money(auction.auto_accept_offer),
		_optional_money(auction.auto_decline_offer),
	)


# Making rows.


def _make_row(auction: Auction, user: str, amount: Decimal, qty: Decimal, now: datetime, bid_type: str) -> frappe._dict:
	"""A row as it will be stored, checked before anything is written."""
	row = frappe._dict(
		bid_type=bid_type,
		bidder=user,
		customer=customer_for(user),
		amount=amount,
		qty=qty,
		placed_at=now,
	)
	_check(auction, row)
	return row


def _submit_row(auction: Auction, row: frappe._dict, now: datetime) -> dict[str, Any]:
	"""Stores a checked row. After Close: it takes effect now. Authorize at Bid: it waits for its hold, and the
	result carries the page where the buyer authorizes it."""
	held = holds_payment(auction)
	doc = frappe.get_doc(
		{
			"doctype": "Auction Bid",
			"auction": auction.name,
			**row,
			"hold_status": AWAITING if held else NOT_REQUIRED,
			"hold_amount": row.amount * row.qty if held else 0,
		}
	).insert(ignore_permissions=True)
	if held:
		return public_state(auction, row.bidder, now) | {"authorize_url": holds.request(auction, doc)}
	result = _apply(auction, doc, now)
	return public_state(auction, row.bidder, now) | result


def place_bid(name: str, amount: Any, user: str, now: datetime | None = None, qty: Any = None) -> dict[str, Any]:
	"""An English bid (the bidder's maximum), a Sealed bid, or a Uniform Price bid for `qty` units."""
	now = now or now_datetime()
	auction = _locked(name)
	_require_open(auction, now)
	if auction.auction_type == rules.DUTCH:
		raise BidRejected(_("This is a Dutch auction: accept the price instead of bidding."))
	if auction.auction_type == rules.BEST_OFFER:
		raise BidRejected(_("This listing takes offers, not bids."))
	units = _units(qty) if auction.auction_type == rules.UNIFORM else _qty(auction.quantity)
	return _submit_row(auction, _make_row(auction, user, money(amount), units, now, BID), now)


def accept_price(name: str, qty: Any, user: str, now: datetime | None = None) -> dict[str, Any]:
	"""A Dutch purchase: `qty` units at the clock price of this moment."""
	now = now or now_datetime()
	auction = _locked(name)
	_require_open(auction, now)
	if auction.auction_type != rules.DUTCH:
		raise BidRejected(_("Only a Dutch auction can be bought at the current price."))
	row = _make_row(auction, user, dutch_price(auction, now), _units(qty), now, PURCHASE)
	return _submit_row(auction, row, now)


def buy_now(name: str, user: str, now: datetime | None = None) -> dict[str, Any]:
	"""Ends an English, Sealed or Best Offer auction by buying the lot at its Buy It Now price."""
	now = now or now_datetime()
	auction = _locked(name)
	_require_open(auction, now)
	price = _optional_money(auction.buy_now_price) or Decimal(0)
	return _submit_row(auction, _make_row(auction, user, price, _qty(auction.quantity), now, BUY_NOW), now)


def make_offer(name: str, amount: Any, user: str, now: datetime | None = None) -> dict[str, Any]:
	"""An offer below a Best Offer listing's asking price. It replaces the buyer's own pending offer, and is
	accepted or declined at once when it crosses the seller's thresholds."""
	now = now or now_datetime()
	auction = _locked(name)
	_require_open(auction, now)
	if auction.auction_type != rules.BEST_OFFER:
		raise BidRejected(_("This auction doesn't take offers."))
	return _submit_row(auction, _make_row(auction, user, money(amount), _qty(auction.quantity), now, OFFER), now)


# Holds.


def hold_authorized(bid_name: str, hold_reference: str, now: datetime | None = None) -> None:
	"""Called by the gateway once it holds a row's amount. The row is judged again against the auction as it
	is now: refused, its hold is released and the row fails; accepted, it takes effect."""
	now = now or now_datetime()
	auction, row = _awaiting(bid_name)
	if row is None:
		return
	# Judged while still uncounted, so the row isn't measured against itself.
	try:
		_require_open(auction, now)
		_check(auction, row)
	except BidRejected as refused:
		row.db_set({"hold_status": AUTHORIZED, "hold_reference": hold_reference})
		_release(auction, row)
		row.db_set({"hold_status": FAILED, "hold_error": str(refused)})
		return
	row.db_set({"hold_status": AUTHORIZED, "hold_reference": hold_reference})
	_apply(auction, row, now)


def hold_failed(bid_name: str, reason: str) -> None:
	"""Called by the gateway when the buyer's card won't hold the amount."""
	_auction, row = _awaiting(bid_name)
	if row is not None:
		row.db_set({"hold_status": FAILED, "hold_error": reason})


def _awaiting(bid_name: str) -> tuple[Auction, Any | None]:
	auction_name = frappe.db.get_value("Auction Bid", bid_name, "auction")
	if not auction_name:
		raise frappe.DoesNotExistError(_("Auction Bid {0} not found").format(bid_name))
	auction = _locked(auction_name)
	row = frappe.get_doc("Auction Bid", bid_name)
	# A gateway may report twice; only the first report of a waiting row counts.
	return auction, row if row.hold_status == AWAITING else None


def _release(auction: Auction, row: Any) -> None:
	"""Lets a row's hold go. A gateway that fails to release leaves the hold Authorized and an Error Log entry,
	and doesn't undo the sale or close that released it."""
	if row.hold_status != AUTHORIZED:
		return
	try:
		holds.release(auction, row.hold_reference)
	except Exception:
		frappe.log_error(
			title=f"Auction Bid {row.name}: releasing the hold failed",
			reference_doctype="Auction Bid",
			reference_name=row.name,
		)
		return
	frappe.db.set_value("Auction Bid", row.name, "hold_status", RELEASED)


def _release_holds(auction: Auction, keep: tuple[str, ...] = (), bidder: str | None = None) -> None:
	filters: dict[str, Any] = {"auction": auction.name, "hold_status": AUTHORIZED}
	if bidder:
		filters["bidder"] = bidder
	for row in frappe.get_all("Auction Bid", filters=filters, fields=["name", "hold_status", "hold_reference"]):
		if row.name not in keep:
			_release(auction, row)


# Effects.


def _apply(auction: Auction, row: Any, now: datetime) -> dict[str, Any]:
	"""Gives a checked row its effect. Returns what the buyer should be told beyond the page state (the
	document a purchase made)."""
	if row.bid_type == BID:
		_apply_bid(auction, row, now)
		return {}
	if row.bid_type == PURCHASE:
		return _apply_purchase(auction, row, now)
	if row.bid_type == BUY_NOW:
		made = _sell_lot(auction, row, money(row.amount), now)
		return {made.field: made.name}
	return _apply_offer(auction, row, now)


def _apply_bid(auction: Auction, row: Any, now: datetime) -> None:
	bids = _bids(auction.name)
	values: dict[str, Any] = {"bid_count": len(bids), "status": OPEN}
	if auction.auction_type == rules.ENGLISH:
		before = english_standing(auction, [bid for bid in bids if bid != _as_bid(row)])
		standing = english_standing(auction, bids)
		values["current_price"] = standing.price
		window = timedelta(minutes=cint(auction.extend_window_minutes))
		values["ends_at"] = rules.extended_end(get_datetime(auction.ends_at), now, window)
		if before.leader and before.leader != standing.leader:
			_release_holds(auction, bidder=before.leader)
	_update(auction, **values)


def _apply_purchase(auction: Auction, row: Any, now: datetime) -> dict[str, Any]:
	units = _qty(row.qty)
	made = settle(auction, row, units, money(row.amount))
	frappe.db.set_value("Auction Bid", row.name, {"is_winner": 1, "units_won": units, made.field: made.name})
	sold = _qty(auction.units_sold) + units
	values: dict[str, Any] = {
		"units_sold": sold,
		"current_price": money(row.amount),
		"bid_count": cint(auction.bid_count) + 1,
		"status": OPEN,
	}
	if sold >= _qty(auction.quantity):
		values.update(status=SOLD, closed_at=now)
	_update(auction, **values)
	return {made.field: made.name}


def _apply_offer(auction: Auction, row: Any, now: datetime) -> dict[str, Any]:
	decision = _offer_decision(auction, money(row.amount))
	_set_pending_offers(auction, rules.OFFER_REPLACED, bidder=row.bidder)
	frappe.db.set_value("Auction Bid", row.name, "offer_status", decision)
	row.offer_status = decision
	_update(auction, bid_count=cint(auction.bid_count) + 1, status=OPEN)
	if decision == rules.OFFER_DECLINED:
		_release(auction, row)
	if decision == rules.OFFER_ACCEPTED:
		made = _sell_lot(auction, row, money(row.amount), now)
		return {made.field: made.name}
	return {}


def _sell_lot(auction: Auction, row: Any, price: Decimal, now: datetime) -> settlement.Settlement:
	"""Sells an English, Sealed or Best Offer lot to one buyer at `price`. Pending offers are declined, and
	every other hold is released."""
	made = settle(auction, row, _qty(auction.quantity), price)
	frappe.db.set_value("Auction Bid", row.name, {"is_winner": 1, made.field: made.name})
	_set_pending_offers(auction, rules.OFFER_DECLINED)
	_release_holds(auction, keep=(row.name,))
	_update(
		auction,
		status=SOLD,
		closed_at=now,
		current_price=price,
		winning_bidder=row.bidder,
		winning_customer=row.customer,
		**{made.field: made.name},
	)
	return made


def _set_pending_offers(auction: Auction, status: str, bidder: str | None = None) -> None:
	filters: dict[str, Any] = {"auction": auction.name, "bid_type": OFFER, "offer_status": rules.OFFER_PENDING}
	if bidder:
		filters["bidder"] = bidder
	for pending in frappe.get_all("Auction Bid", filters=filters, fields=["name", "hold_status", "hold_reference"]):
		frappe.db.set_value("Auction Bid", pending.name, "offer_status", status)
		_release(auction, pending)


def _pending_offer(bid_name: str) -> tuple[Auction, Any]:
	auction_name = frappe.db.get_value("Auction Bid", bid_name, "auction")
	if not auction_name:
		raise frappe.DoesNotExistError(_("Offer {0} not found").format(bid_name))
	auction = _locked(auction_name)
	row = frappe.get_doc("Auction Bid", bid_name)
	if row.bid_type != OFFER or row.offer_status != rules.OFFER_PENDING:
		raise BidRejected(_("Only a pending offer can be answered."))
	if auction.status in CLOSED_STATUSES:
		raise BidRejected(_("This listing has closed."))
	return auction, row


def accept_offer(bid_name: str, now: datetime | None = None) -> str:
	"""The seller accepts a pending offer: the lot is sold to that buyer at the offer. Returns the order or
	invoice made."""
	now = now or now_datetime()
	auction, row = _pending_offer(bid_name)
	row.db_set("offer_status", rules.OFFER_ACCEPTED)
	return _sell_lot(auction, row, money(row.amount), now).name


def decline_offer(bid_name: str) -> None:
	auction, row = _pending_offer(bid_name)
	row.db_set("offer_status", rules.OFFER_DECLINED)
	_release(auction, row)


# Closing.


def close(name: str, now: datetime | None = None) -> str:
	"""Settles an auction whose end has passed. Returns its final status."""
	now = now or now_datetime()
	auction = _locked(name)
	if auction.status in CLOSED_STATUSES:
		return auction.status
	if now < get_datetime(auction.ends_at):
		raise ValueError(f"Auction {name} hasn't ended")
	if auction.auction_type == rules.DUTCH:
		status = SOLD if flt(auction.units_sold) > 0 else UNSOLD
		_update(auction, status=status, closed_at=now)
		return status
	if auction.auction_type == rules.BEST_OFFER:
		_set_pending_offers(auction, rules.OFFER_EXPIRED)
		return _unsold(auction, now)
	if auction.auction_type == rules.UNIFORM:
		return _close_uniform(auction, now)
	rows = _bid_rows(auction.name)
	bids = [_as_bid(row) for row in rows]
	if auction.auction_type == rules.ENGLISH:
		standing = english_standing(auction, bids)
	else:
		standing = sealed_standing(auction, bids)
	if not standing.sells or standing.leader is None or standing.price is None:
		return _unsold(auction, now, standing.price)
	winning = next(row for row in rows if row.bidder == standing.leader and money(row.amount) == standing.leader_amount)
	_sell_lot(auction, winning, standing.price, now)
	return SOLD


def _unsold(auction: Auction, now: datetime, price: Decimal | None = None) -> str:
	_release_holds(auction)
	values: dict[str, Any] = {"status": UNSOLD, "closed_at": now}
	if price is not None:
		# Currency columns are NOT NULL: an auction with no bids keeps its price as it was.
		values["current_price"] = price
	_update(auction, **values)
	return UNSOLD


def _close_uniform(auction: Auction, now: datetime) -> str:
	rows = _bid_rows(auction.name)
	price, won = rules.uniform_result(
		[_as_bid(row) for row in rows],
		_qty(auction.quantity),
		money(auction.start_price),
		_optional_money(auction.reserve_price),
	)
	if price is None:
		return _unsold(auction, now)
	winners: list[str] = []
	for allocation in won:
		row = next(r for r in rows if _as_bid(r) == allocation.bid)
		made = settle(auction, row, allocation.units, price)
		frappe.db.set_value(
			"Auction Bid", row.name, {"is_winner": 1, "units_won": allocation.units, made.field: made.name}
		)
		winners.append(row.name)
	_release_holds(auction, keep=tuple(winners))
	_update(
		auction,
		status=SOLD,
		closed_at=now,
		current_price=price,
		units_sold=sum((a.units for a in won), Decimal(0)),
	)
	return SOLD


# Settlement.


def _as_administrator[T](action: Callable[[], T]) -> T:
	"""Settlement creates accounting documents the buyer may not create, whoever triggered it (a buyer's
	request, a gateway's callback, or the scheduler)."""
	user = frappe.session.user
	frappe.set_user("Administrator")
	try:
		return action()
	finally:
		frappe.set_user(user)


def settle(auction: Auction, row: Any, qty: Decimal, rate: Decimal) -> settlement.Settlement:
	"""The buyer's submitted order or invoice. A held row's hold is captured (up to what it holds) and booked as
	a Payment Entry; whatever is still owed gets a Payment Request when the auction names a gateway."""

	def make() -> settlement.Settlement:
		made, document = settlement.create(auction, row.customer, qty, rate)
		if row.hold_status == AUTHORIZED:
			amount = min(money(row.hold_amount), money(document.grand_total))
			transaction_id = holds.capture(auction, row.hold_reference, amount)
			settlement.book_payment(auction, made, amount, transaction_id)
			frappe.db.set_value("Auction Bid", row.name, "hold_status", CAPTURED)
		settlement.request_payment(auction, made)
		return made

	return _as_administrator(make)


def open_and_close_due(now: datetime | None = None) -> None:
	"""Scheduled every minute: opens auctions whose start has come and settles those whose end has passed.
	Each auction commits on its own; one that fails to settle keeps its error and is skipped until someone
	clears it."""
	now = now or now_datetime()
	frappe.db.set_value(
		"Auction",
		{"status": SCHEDULED, "starts_at": ["<=", now], "ends_at": [">", now]},
		"status",
		OPEN,
	)
	frappe.db.commit()
	due = frappe.get_all(
		"Auction",
		filters={"status": ["in", (SCHEDULED, OPEN)], "ends_at": ["<=", now], "settlement_error": ["is", "not set"]},
		pluck="name",
	)
	for name in due:
		try:
			close(name, now)
			frappe.db.commit()
		except Exception as error:
			frappe.db.rollback()
			frappe.db.set_value("Auction", name, "settlement_error", str(error) or type(error).__name__)
			frappe.log_error(
				title=f"Auction {name} settlement failed", reference_doctype="Auction", reference_name=name
			)
			frappe.db.commit()


# What visitors see.


def public_state(auction: Auction, user: str, now: datetime | None = None) -> dict[str, Any]:
	"""What a visitor may see of an auction now. Sealed and Uniform Price bids stay hidden until the close;
	nobody's identity, maximum or offer is shown except the visitor's own."""
	now = now or now_datetime()
	starts = get_datetime(auction.starts_at)
	ends = get_datetime(auction.ends_at)
	closed = auction.status in CLOSED_STATUSES
	if closed:
		status = auction.status
	elif now < starts:
		status = SCHEDULED
	elif now < ends:
		status = OPEN
	else:
		status = "Closing"
	signed_in = bool(user and user != "Guest")
	state: dict[str, Any] = {
		"name": auction.name,
		"auction_type": auction.auction_type,
		"status": status,
		"open": is_open(auction, now),
		"currency": auction.currency,
		"quantity": flt(auction.quantity),
		"bid_count": cint(auction.bid_count),
		"seconds_to_start": max(0, int((starts - now).total_seconds())),
		"seconds_to_end": max(0, int((ends - now).total_seconds())),
		"signed_in": signed_in,
		"payment_timing": auction.payment_timing or AFTER_CLOSE,
	}
	rows = _bid_rows(auction.name, ROW_TYPE.get(auction.auction_type, BID))
	own = [row for row in rows if row.bidder == user] if signed_in else []
	your_best = flt(max(row.amount for row in own)) if own else None
	standing = rules.NO_BIDS
	if auction.auction_type == rules.ENGLISH:
		standing = english_standing(auction, [_as_bid(row) for row in rows])
		terms = _terms(auction)
		state |= {
			"current_price": flt(standing.price) if standing.price is not None else None,
			"minimum_bid": flt(rules.english_minimum_bid(standing, terms["start"], terms["increment"])),
			"you_lead": signed_in and standing.leader == user,
			"your_max": your_best,
			"reserve_met": standing.reserve_met if terms["reserve"] else None,
		}
	elif auction.auction_type in rules.BLIND_FORMATS:
		state |= {
			"minimum_bid": flt(auction.start_price),
			"your_bid": your_best,
			"current_price": flt(auction.current_price) if closed and auction.current_price else None,
		}
		if auction.auction_type == rules.SEALED:
			state["sealed_pricing"] = auction.sealed_pricing
		else:
			best_own = max(own, key=lambda row: (flt(row.amount), get_datetime(row.placed_at))) if own else None
			state |= {
				"your_qty": flt(best_own.qty) if best_own else None,
				"you_won_units": sum(flt(row.units_won) for row in own if row.is_winner),
			}
	elif auction.auction_type == rules.BEST_OFFER:
		latest = own[-1] if own else None
		state |= {
			"asking_price": flt(auction.buy_now_price),
			"current_price": flt(auction.current_price) if closed and auction.current_price else None,
			"your_offer": flt(latest.amount) if latest else None,
			"your_offer_status": latest.offer_status if latest else None,
		}
	else:
		state |= {
			"current_price": flt(dutch_price(auction, min(max(now, starts), ends))),
			"floor_price": flt(auction.floor_price),
			"price_step": flt(auction.price_step),
			"step_interval_seconds": cint(auction.step_interval_seconds),
			"seconds_to_next_step": _seconds_to_next_step(auction, now),
			"units_left": flt(units_left(auction)),
			"you_bought": sum(flt(row.qty) for row in own),
		}
	price = _optional_money(auction.buy_now_price)
	if state["open"] and price and auction.auction_type != rules.BEST_OFFER:
		available = rules.buy_now_available(
			auction.auction_type, price, standing, _optional_money(auction.reserve_price)
		)
		state["buy_now_price"] = flt(price) if available else None
	if closed and auction.auction_type in (rules.ENGLISH, rules.SEALED, rules.BEST_OFFER):
		state["you_won"] = signed_in and auction.winning_bidder == user
	if signed_in and holds_payment(auction):
		state["your_hold"] = _latest_hold(auction, user)
	return state


def _latest_hold(auction: Auction, user: str) -> dict[str, Any] | None:
	"""The visitor's newest row's hold, counted or not, so they see one that is waiting or failed."""
	latest = frappe.get_all(
		"Auction Bid",
		filters={"auction": auction.name, "bidder": user},
		fields=["hold_status", "hold_error"],
		order_by="placed_at desc, creation desc",
		limit=1,
	)
	return {"status": latest[0].hold_status, "error": latest[0].hold_error} if latest else None


def _seconds_to_next_step(auction: Auction, now: datetime) -> int | None:
	starts = get_datetime(auction.starts_at)
	interval = cint(auction.step_interval_seconds)
	if not interval or now < starts:
		return None
	return interval - int((now - starts).total_seconds()) % interval
