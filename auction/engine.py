# SPDX-License-Identifier: AGPL-3.0-or-later
"""Applies ``auction.rules`` to auctions in the database: bids, Dutch and Buy It Now purchases, Best Offer
offers, the scheduled open and close, and settlement (a submitted Sales Order for each buyer, and a Payment
Request when the auction names a gateway).

Every write to an auction first locks its row (``SELECT … FOR UPDATE``), so two bids arriving together are
judged one after the other, each against the bids before it. The lock is held until the request's
transaction commits.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, now_datetime, nowdate

from auction import rules
from auction.rules import Bid, BidRejected

if TYPE_CHECKING:
	from auction.auction.doctype.auction.auction import Auction

SCHEDULED = "Scheduled"
OPEN = "Open"
SOLD = "Sold"
UNSOLD = "Unsold"
CLOSED_STATUSES = (SOLD, UNSOLD)

# Auction Bid types.
BID = "Bid"
BUY_NOW = "Buy Now"
OFFER = "Offer"
PURCHASE = "Purchase"
# The Auction Bid rows a format's page reads; the rest read bids.
ROW_TYPE = {rules.DUTCH: PURCHASE, rules.BEST_OFFER: OFFER}
DEFAULT_CURRENCY_PRECISION = 2


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


def _locked(name: str) -> Auction:
	if not frappe.db.sql("select name from `tabAuction` where name = %s for update", name):
		raise frappe.DoesNotExistError(_("Auction {0} not found").format(name))
	return frappe.get_doc("Auction", name)


def _qty(value: Any) -> Decimal:
	return Decimal(str(flt(value)))


def _bid_rows(name: str, bid_type: str = BID) -> list[Any]:
	return frappe.get_all(
		"Auction Bid",
		filters={"auction": name, "bid_type": bid_type},
		fields=["name", "bidder", "customer", "amount", "qty", "placed_at", "offer_status", "is_winner", "units_won"],
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
		"start": money(auction.start_price),
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


def _insert_bid(
	auction: Auction,
	user: str,
	customer: str,
	amount: Decimal,
	qty: Decimal,
	now: datetime,
	bid_type: str = BID,
	**values: Any,
) -> Any:
	return frappe.get_doc(
		{
			"doctype": "Auction Bid",
			"auction": auction.name,
			"bid_type": bid_type,
			"bidder": user,
			"customer": customer,
			"amount": amount,
			"qty": qty,
			"placed_at": now,
			**values,
		}
	).insert(ignore_permissions=True)


def _units(value: Any) -> Decimal:
	try:
		return Decimal(str(value).strip())
	except (InvalidOperation, AttributeError) as error:
		raise BidRejected(_("Choose at least one unit.")) from error


def place_bid(name: str, amount: Any, user: str, now: datetime | None = None, qty: Any = None) -> dict[str, Any]:
	"""An English bid (the bidder's maximum), a Sealed bid, or a Uniform Price bid for `qty` units."""
	now = now or now_datetime()
	auction = _locked(name)
	_require_open(auction, now)
	if auction.auction_type == rules.DUTCH:
		raise BidRejected(_("This is a Dutch auction: accept the price instead of bidding."))
	if auction.auction_type == rules.BEST_OFFER:
		raise BidRejected(_("This listing takes offers, not bids."))
	customer = customer_for(user)
	units = _units(qty) if auction.auction_type == rules.UNIFORM else _qty(auction.quantity)
	bid = Bid(bidder=user, amount=money(amount), placed=now, qty=units)
	bids = _bids(auction.name)
	if auction.auction_type == rules.ENGLISH:
		terms = _terms(auction)
		rules.check_english_bid(bids, bid, terms["start"], terms["increment"], terms["reserve"])
	elif auction.auction_type == rules.UNIFORM:
		rules.check_uniform_bid(bids, bid, money(auction.start_price), _qty(auction.quantity))
	else:
		rules.check_sealed_bid(bids, bid, money(auction.start_price))
	_insert_bid(auction, user, customer, bid.amount, units, now)
	bids.append(bid)
	values: dict[str, Any] = {"bid_count": len(bids), "status": OPEN}
	if auction.auction_type == rules.ENGLISH:
		values["current_price"] = english_standing(auction, bids).price
		window = timedelta(minutes=cint(auction.extend_window_minutes))
		values["ends_at"] = rules.extended_end(get_datetime(auction.ends_at), now, window)
	_update(auction, **values)
	return public_state(auction, user, now)


def accept_price(name: str, qty: Any, user: str, now: datetime | None = None) -> dict[str, Any]:
	"""A Dutch purchase: `qty` units at the clock price of this moment, ordered at once."""
	now = now or now_datetime()
	auction = _locked(name)
	_require_open(auction, now)
	if auction.auction_type != rules.DUTCH:
		raise BidRejected(_("Only a Dutch auction can be bought at the current price."))
	units = _units(qty)
	rules.check_dutch_accept(units_left(auction), units)
	customer = customer_for(user)
	price = dutch_price(auction, now)
	purchase = _insert_bid(auction, user, customer, price, units, now, PURCHASE)
	sales_order = settle(auction, customer, units, price)
	purchase.db_set({"is_winner": 1, "units_won": units, "sales_order": sales_order})
	sold = _qty(auction.units_sold) + units
	values: dict[str, Any] = {
		"units_sold": sold,
		"current_price": price,
		"bid_count": cint(auction.bid_count) + 1,
		"status": OPEN,
	}
	if sold >= _qty(auction.quantity):
		values.update(status=SOLD, closed_at=now)
	_update(auction, **values)
	return public_state(auction, user, now) | {"sales_order": sales_order}


def buy_now(name: str, user: str, now: datetime | None = None) -> dict[str, Any]:
	"""Ends an English, Sealed or Best Offer auction by buying the lot at its Buy It Now price."""
	now = now or now_datetime()
	auction = _locked(name)
	_require_open(auction, now)
	standing = (
		english_standing(auction, _bids(auction.name)) if auction.auction_type == rules.ENGLISH else rules.NO_BIDS
	)
	price = _optional_money(auction.buy_now_price)
	if price is None or not rules.buy_now_available(
		auction.auction_type, price, standing, _optional_money(auction.reserve_price)
	):
		raise BidRejected(_("Buy It Now isn't available on this auction now."))
	customer = customer_for(user)
	purchase = _insert_bid(auction, user, customer, price, _qty(auction.quantity), now, BUY_NOW)
	sales_order = _sell_lot(auction, purchase.name, user, customer, price, now)
	return public_state(auction, user, now) | {"sales_order": sales_order}


def _sell_lot(auction: Auction, bid_name: str, user: str, customer: str, price: Decimal, now: datetime) -> str:
	"""Sells an English, Sealed or Best Offer lot to one buyer at `price`; any offers still pending are
	declined."""
	sales_order = settle(auction, customer, _qty(auction.quantity), price)
	frappe.db.set_value("Auction Bid", bid_name, {"is_winner": 1, "sales_order": sales_order})
	_set_pending_offers(auction, rules.OFFER_DECLINED)
	_update(
		auction,
		status=SOLD,
		closed_at=now,
		current_price=price,
		winning_bidder=user,
		winning_customer=customer,
		sales_order=sales_order,
	)
	return sales_order


def _set_pending_offers(auction: Auction, status: str, bidder: str | None = None) -> None:
	filters: dict[str, Any] = {"auction": auction.name, "bid_type": OFFER, "offer_status": rules.OFFER_PENDING}
	if bidder:
		filters["bidder"] = bidder
	for pending in frappe.get_all("Auction Bid", filters=filters, pluck="name"):
		frappe.db.set_value("Auction Bid", pending, "offer_status", status)


def make_offer(name: str, amount: Any, user: str, now: datetime | None = None) -> dict[str, Any]:
	"""An offer below a Best Offer listing's asking price. It replaces the buyer's own pending offer, and is
	accepted or declined at once when it crosses the seller's thresholds."""
	now = now or now_datetime()
	auction = _locked(name)
	_require_open(auction, now)
	if auction.auction_type != rules.BEST_OFFER:
		raise BidRejected(_("This auction doesn't take offers."))
	customer = customer_for(user)
	offer = money(amount)
	decision = rules.offer_decision(
		offer,
		money(auction.buy_now_price),
		_optional_money(auction.auto_accept_offer),
		_optional_money(auction.auto_decline_offer),
	)
	_set_pending_offers(auction, rules.OFFER_REPLACED, bidder=user)
	row = _insert_bid(auction, user, customer, offer, _qty(auction.quantity), now, OFFER, offer_status=decision)
	_update(auction, bid_count=cint(auction.bid_count) + 1, status=OPEN)
	if decision == rules.OFFER_ACCEPTED:
		row.db_set("offer_status", rules.OFFER_ACCEPTED)
		_sell_lot(auction, row.name, user, customer, offer, now)
	return public_state(auction, user, now)


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
	"""The seller accepts a pending offer: the lot is sold to that buyer at the offer. Returns the Sales
	Order."""
	now = now or now_datetime()
	auction, row = _pending_offer(bid_name)
	row.db_set("offer_status", rules.OFFER_ACCEPTED)
	return _sell_lot(auction, row.name, row.bidder, row.customer, money(row.amount), now)


def decline_offer(bid_name: str) -> None:
	_auction, row = _pending_offer(bid_name)
	row.db_set("offer_status", rules.OFFER_DECLINED)


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
		_update(auction, status=UNSOLD, closed_at=now)
		return UNSOLD
	if auction.auction_type == rules.UNIFORM:
		return _close_uniform(auction, now)
	rows = _bid_rows(auction.name)
	bids = [_as_bid(row) for row in rows]
	if auction.auction_type == rules.ENGLISH:
		standing = english_standing(auction, bids)
	else:
		standing = sealed_standing(auction, bids)
	if not standing.sells or standing.leader is None or standing.price is None:
		unsold: dict[str, Any] = {"status": UNSOLD, "closed_at": now}
		if standing.price is not None:
			# Currency columns are NOT NULL: an auction with no bids keeps its price as it was.
			unsold["current_price"] = standing.price
		_update(auction, **unsold)
		return UNSOLD
	winning = next(row for row in rows if row.bidder == standing.leader and money(row.amount) == standing.leader_amount)
	_sell_lot(auction, winning.name, winning.bidder, winning.customer, standing.price, now)
	return SOLD


def _close_uniform(auction: Auction, now: datetime) -> str:
	rows = _bid_rows(auction.name)
	price, won = rules.uniform_result(
		[_as_bid(row) for row in rows],
		_qty(auction.quantity),
		money(auction.start_price),
		_optional_money(auction.reserve_price),
	)
	if price is None:
		_update(auction, status=UNSOLD, closed_at=now)
		return UNSOLD
	for allocation in won:
		row = next(
			r
			for r in rows
			if r.bidder == allocation.bid.bidder
			and money(r.amount) == allocation.bid.amount
			and get_datetime(r.placed_at) == allocation.bid.placed
		)
		sales_order = settle(auction, row.customer, allocation.units, price)
		frappe.db.set_value(
			"Auction Bid", row.name, {"is_winner": 1, "units_won": allocation.units, "sales_order": sales_order}
		)
	_update(
		auction,
		status=SOLD,
		closed_at=now,
		current_price=price,
		units_sold=sum((a.units for a in won), Decimal(0)),
	)
	return SOLD


def _as_administrator[T](action: Callable[[], T]) -> T:
	"""Settlement creates accounting documents the buyer may not create, whoever triggered it (a Dutch
	buyer's request, or the scheduler)."""
	user = frappe.session.user
	frappe.set_user("Administrator")
	try:
		return action()
	finally:
		frappe.set_user(user)


def settle(auction: Auction, customer: str, qty: Decimal, rate: Decimal) -> str:
	"""A submitted Sales Order for the buyer at the auction price, and a Payment Request when the auction
	names a gateway. Returns the Sales Order's name."""

	def make() -> str:
		today = nowdate()
		order = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"company": auction.company,
				"customer": customer,
				"currency": auction.currency,
				"order_type": "Shopping Cart",
				"transaction_date": today,
				"delivery_date": today,
				"ignore_pricing_rule": 1,
				"items": [
					{
						"item_code": auction.item,
						"qty": flt(qty),
						"rate": flt(rate),
						"price_list_rate": flt(rate),
						"delivery_date": today,
						"warehouse": auction.warehouse,
					}
				],
			}
		)
		if auction.taxes_and_charges:
			from erpnext.controllers.accounts_controller import get_taxes_and_charges

			order.taxes_and_charges = auction.taxes_and_charges
			order.set("taxes", get_taxes_and_charges("Sales Taxes and Charges Template", auction.taxes_and_charges))
		order.insert()
		order.submit()
		if auction.payment_gateway_account:
			from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request

			make_payment_request(
				dt="Sales Order",
				dn=order.name,
				payment_gateway_account=auction.payment_gateway_account,
				submit_doc=1,
				mute_email=0,
				order_type="Shopping Cart",
			)
		return order.name

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


def public_state(auction: Auction, user: str, now: datetime | None = None) -> dict[str, Any]:
	"""What a visitor may see of an auction now. Sealed bids stay hidden until the close; nobody's identity
	or maximum is shown except the visitor's own."""
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
		"signed_in": bool(user and user != "Guest"),
	}
	signed_in = state["signed_in"]
	rows = _bid_rows(auction.name, ROW_TYPE.get(auction.auction_type, BID))
	own = [row for row in rows if row.bidder == user] if signed_in else []
	your_best = flt(max(row.amount for row in own)) if own else None
	standing = rules.NO_BIDS
	if auction.auction_type == rules.ENGLISH:
		bids = [_as_bid(row) for row in rows]
		standing = english_standing(auction, bids)
		terms = _terms(auction)
		state |= {
			"current_price": flt(standing.price) if standing.price is not None else None,
			"minimum_bid": flt(rules.english_minimum_bid(standing, terms["start"], terms["increment"])),
			"you_lead": state["signed_in"] and standing.leader == user,
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
	return state


def _seconds_to_next_step(auction: Auction, now: datetime) -> int | None:
	starts = get_datetime(auction.starts_at)
	interval = cint(auction.step_interval_seconds)
	if not interval or now < starts:
		return None
	return interval - int((now - starts).total_seconds()) % interval
