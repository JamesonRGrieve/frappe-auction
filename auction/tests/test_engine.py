# SPDX-License-Identifier: AGPL-3.0-or-later
"""The engine against the real ERPNext test DB: bids, Dutch purchases, closing and settlement into submitted
Sales Orders, the scheduled open and close, and what a visitor sees. ``open_and_close_due`` commits, so
every test works on its own lot item and clears that item's auctions first. ``ERPNextTestSuite`` is the base
because importing it builds ERPNext's test records (company, customer group, territory, tax template)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import frappe
from erpnext.tests.utils import ERPNextTestSuite
from frappe.utils import now_datetime

from auction import engine, rules
from auction.rules import BidRejected

COMPANY = "_Test Company"
LOT = "_Test Auction Lot"
TAX_TEMPLATE = "_Test Sales Taxes and Charges Template - _TC"
GATEWAY = "_Test Auction Gateway"
GATEWAY_PAYMENT_ACCOUNT = "_Test Bank - _TC"
BUYERS = ("ann", "bob", "cat")
HOUR = timedelta(hours=1)


def user(buyer: str) -> str:
	return f"{buyer}.auction@example.com"


def customer(buyer: str) -> str:
	return f"_Test Auction Buyer {buyer}"


def ensure_buyer(buyer: str) -> None:
	email = user(buyer)
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": buyer.title(),
				"user_type": "Website User",
				"send_welcome_email": 0,
				"roles": [{"role": "Customer"}],
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Customer", customer(buyer)):
		frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": customer(buyer),
				"customer_group": "_Test Customer Group",
				"territory": "_Test Territory",
				"portal_users": [{"user": email}],
			}
		).insert(ignore_permissions=True)


class AuctionTestCase(ERPNextTestSuite):
	def setUp(self) -> None:
		self.now = now_datetime().replace(microsecond=0)
		for name in frappe.get_all("Auction", filters={"item": LOT}, pluck="name"):
			frappe.db.delete("Auction Bid", {"auction": name})
			frappe.db.delete("Auction", {"name": name})
		if not frappe.db.exists("Item", LOT):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": LOT,
					"item_name": "Auction Lot",
					"item_group": "_Test Item Group",
					"stock_uom": "_Test UOM",
					"is_stock_item": 0,
				}
			).insert(ignore_permissions=True)
		frappe.db.set_value("Item", LOT, "disabled", 0)
		for buyer in BUYERS:
			ensure_buyer(buyer)
		frappe.db.commit()

	def make(self, auction_type: str, **values: Any) -> Any:
		doc: dict[str, Any] = {
			"doctype": "Auction",
			"title": f"Test {auction_type} lot",
			"item": LOT,
			"company": COMPANY,
			"quantity": 1,
			"auction_type": auction_type,
			"start_price": 10,
			"starts_at": self.now - HOUR,
			"ends_at": self.now + HOUR,
			"published": 1,
		}
		if auction_type == rules.ENGLISH:
			doc |= {"bid_increment": 1, "extend_window_minutes": 2}
		elif auction_type == rules.SEALED:
			doc |= {"sealed_pricing": rules.SECOND_PRICE}
		elif auction_type == rules.UNIFORM:
			doc |= {"quantity": 5}
		elif auction_type == rules.BEST_OFFER:
			doc |= {"buy_now_price": 100}
			doc.pop("start_price")
		else:
			doc |= {"start_price": 100, "floor_price": 40, "price_step": 5, "step_interval_seconds": 60}
		doc |= values
		return frappe.get_doc(doc).insert(ignore_permissions=True)

	def bid(self, auction: Any, buyer: str, amount: str, at: datetime | None = None) -> dict[str, Any]:
		return engine.place_bid(auction.name, amount, user(buyer), at or self.now)

	def reload(self, auction: Any) -> Any:
		return frappe.get_doc("Auction", auction.name)

	def order(self, name: str) -> Any:
		order = frappe.get_doc("Sales Order", name)
		self.assertEqual(order.docstatus, 1)
		self.assertEqual(len(order.items), 1)
		self.assertEqual(order.items[0].item_code, LOT)
		return order


class TestEnglish(AuctionTestCase):
	def test_the_winner_pays_one_increment_over_the_runner_up(self) -> None:
		auction = self.make(rules.ENGLISH)
		self.bid(auction, "ann", "50")
		state = self.bid(auction, "bob", "20")
		self.assertEqual(state["current_price"], 21)
		self.bid(auction, "bob", "30")
		with self.assertRaisesRegex(BidRejected, "32"):
			self.bid(auction, "cat", "31")
		state = engine.public_state(self.reload(auction), user("ann"), self.now)
		self.assertEqual((state["current_price"], state["minimum_bid"], state["bid_count"]), (31, 32, 3))
		self.assertTrue(state["you_lead"])
		self.assertEqual(state["your_max"], 50)
		self.assertFalse(engine.public_state(self.reload(auction), user("bob"), self.now)["you_lead"])

		self.assertEqual(engine.close(auction.name, self.now + 2 * HOUR), engine.SOLD)
		auction = self.reload(auction)
		self.assertEqual((auction.winning_bidder, auction.winning_customer), (user("ann"), customer("ann")))
		self.assertEqual(auction.current_price, 31)
		order = self.order(auction.sales_order)
		self.assertEqual((order.customer, order.items[0].rate, order.items[0].qty), (customer("ann"), 31, 1))
		winners = frappe.get_all("Auction Bid", filters={"auction": auction.name, "is_winner": 1}, pluck="bidder")
		self.assertEqual(winners, [user("ann")])
		self.assertTrue(engine.public_state(auction, user("ann"), self.now + 2 * HOUR)["you_won"])

	def test_a_late_bid_moves_the_end(self) -> None:
		auction = self.make(rules.ENGLISH)
		late = self.now + HOUR - timedelta(seconds=30)
		self.bid(auction, "ann", "15", late)
		self.assertEqual(self.reload(auction).ends_at, late + timedelta(minutes=2))

	def test_a_reserve_nobody_reaches_leaves_the_lot_unsold(self) -> None:
		auction = self.make(rules.ENGLISH, reserve_price=100)
		self.bid(auction, "ann", "60")
		self.assertIs(engine.public_state(self.reload(auction), user("ann"), self.now)["reserve_met"], False)
		self.assertEqual(engine.close(auction.name, self.now + 2 * HOUR), engine.UNSOLD)
		self.assertFalse(self.reload(auction).sales_order)

	def test_an_auction_nobody_bid_on_closes_unsold(self) -> None:
		# Regression: closing wrote NULL into the NOT NULL current_price and the close failed.
		auction = self.make(rules.ENGLISH)
		self.assertEqual(engine.close(auction.name, self.now + 2 * HOUR), engine.UNSOLD)
		auction = self.reload(auction)
		self.assertEqual(auction.status, engine.UNSOLD)
		self.assertFalse(auction.settlement_error)

	def test_the_winners_order_carries_the_auctions_taxes(self) -> None:
		auction = self.make(rules.ENGLISH, taxes_and_charges=TAX_TEMPLATE)
		self.bid(auction, "ann", "40")
		engine.close(auction.name, self.now + 2 * HOUR)
		order = self.order(self.reload(auction).sales_order)
		self.assertEqual(order.taxes_and_charges, TAX_TEMPLATE)
		self.assertTrue(order.taxes)
		self.assertGreater(order.grand_total, order.net_total)

	def test_terms_are_locked_once_bidding_starts(self) -> None:
		auction = self.make(rules.ENGLISH)
		self.bid(auction, "ann", "15")
		auction = self.reload(auction)
		auction.start_price = 5
		with self.assertRaises(frappe.ValidationError):
			auction.save(ignore_permissions=True)


class TestSealed(AuctionTestCase):
	def test_second_price_pays_the_runner_up_bid_and_hides_bids_until_the_close(self) -> None:
		auction = self.make(rules.SEALED)
		self.bid(auction, "ann", "50")
		self.bid(auction, "bob", "40")
		state = engine.public_state(self.reload(auction), user("bob"), self.now)
		self.assertIsNone(state["current_price"])
		self.assertEqual((state["your_bid"], state["bid_count"]), (40, 2))
		self.assertNotIn("you_lead", state)

		engine.close(auction.name, self.now + 2 * HOUR)
		auction = self.reload(auction)
		self.assertEqual(auction.winning_bidder, user("ann"))
		self.assertEqual(self.order(auction.sales_order).items[0].rate, 40)
		self.assertEqual(engine.public_state(auction, "Guest", self.now + 2 * HOUR)["current_price"], 40)

	def test_first_price_pays_the_winning_bid(self) -> None:
		auction = self.make(rules.SEALED, sealed_pricing=rules.FIRST_PRICE)
		self.bid(auction, "ann", "50")
		self.bid(auction, "bob", "40")
		engine.close(auction.name, self.now + 2 * HOUR)
		self.assertEqual(self.order(self.reload(auction).sales_order).items[0].rate, 50)

	def test_a_bidder_may_only_raise_their_sealed_bid(self) -> None:
		auction = self.make(rules.SEALED)
		self.bid(auction, "ann", "30")
		with self.assertRaises(BidRejected):
			self.bid(auction, "ann", "25")


class TestDutch(AuctionTestCase):
	def test_buyers_take_units_at_the_clock_price_until_sold_out(self) -> None:
		auction = self.make(rules.DUTCH, quantity=3, starts_at=self.now - timedelta(seconds=125))
		state = engine.public_state(auction, user("ann"), self.now)
		self.assertEqual((state["current_price"], state["units_left"]), (90, 3))

		state = engine.accept_price(auction.name, "2", user("ann"), self.now)
		self.assertEqual((state["units_left"], state["you_bought"]), (1, 2))
		order = self.order(state["sales_order"])
		self.assertEqual((order.customer, order.items[0].rate, order.items[0].qty), (customer("ann"), 90, 2))

		with self.assertRaisesRegex(BidRejected, "Only 1 left"):
			engine.accept_price(auction.name, "2", user("bob"), self.now)
		later = self.now + timedelta(minutes=10)
		state = engine.accept_price(auction.name, "1", user("bob"), later)
		self.assertEqual(self.order(state["sales_order"]).items[0].rate, 40)
		auction = self.reload(auction)
		self.assertEqual((auction.status, auction.units_sold), (engine.SOLD, 3))
		with self.assertRaises(BidRejected):
			engine.accept_price(auction.name, "1", user("cat"), later)

	def test_a_dutch_auction_takes_no_bids(self) -> None:
		auction = self.make(rules.DUTCH)
		with self.assertRaises(BidRejected):
			self.bid(auction, "ann", "90")


class TestBuyNow(AuctionTestCase):
	def test_english_buy_now_sells_at_once_until_the_first_bid(self) -> None:
		auction = self.make(rules.ENGLISH, buy_now_price=80)
		self.assertEqual(engine.public_state(auction, user("ann"), self.now)["buy_now_price"], 80)
		state = engine.buy_now(auction.name, user("ann"), self.now)
		auction = self.reload(auction)
		self.assertEqual((auction.status, auction.winning_bidder), (engine.SOLD, user("ann")))
		self.assertEqual(self.order(state["sales_order"]).items[0].rate, 80)
		self.assertTrue(state["you_won"])

	def test_english_buy_now_goes_away_after_a_bid(self) -> None:
		auction = self.make(rules.ENGLISH, buy_now_price=80)
		self.bid(auction, "bob", "15")
		self.assertIsNone(engine.public_state(self.reload(auction), user("ann"), self.now)["buy_now_price"])
		with self.assertRaisesRegex(BidRejected, "Buy It Now"):
			engine.buy_now(auction.name, user("ann"), self.now)

	def test_with_a_reserve_buy_now_lasts_until_the_reserve_is_met(self) -> None:
		auction = self.make(rules.ENGLISH, buy_now_price=80, reserve_price=50)
		self.bid(auction, "bob", "20")
		engine.buy_now(auction.name, user("ann"), self.now)
		self.assertEqual(self.reload(auction).winning_bidder, user("ann"))

	def test_sealed_buy_now_beats_the_bids_in(self) -> None:
		auction = self.make(rules.SEALED, buy_now_price=80)
		self.bid(auction, "bob", "70")
		engine.buy_now(auction.name, user("ann"), self.now)
		auction = self.reload(auction)
		self.assertEqual(
			(auction.status, auction.winning_bidder, auction.current_price), (engine.SOLD, user("ann"), 80)
		)

	def test_a_buy_now_price_must_sit_above_the_start(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			self.make(rules.ENGLISH, buy_now_price=5)


class TestUniform(AuctionTestCase):
	def test_winners_take_units_and_all_pay_the_lowest_winning_bid(self) -> None:
		auction = self.make(rules.UNIFORM)
		engine.place_bid(auction.name, "30", user("ann"), self.now, qty="2")
		engine.place_bid(auction.name, "25", user("bob"), self.now + timedelta(seconds=1), qty="2")
		engine.place_bid(auction.name, "20", user("cat"), self.now + timedelta(seconds=2), qty="3")
		state = engine.public_state(self.reload(auction), user("bob"), self.now)
		self.assertIsNone(state["current_price"])
		self.assertEqual((state["your_bid"], state["your_qty"], state["bid_count"]), (25, 2, 3))

		self.assertEqual(engine.close(auction.name, self.now + 2 * HOUR), engine.SOLD)
		auction = self.reload(auction)
		self.assertEqual((auction.current_price, auction.units_sold), (20, 5))
		won = frappe.get_all(
			"Auction Bid",
			filters={"auction": auction.name, "is_winner": 1},
			fields=["bidder", "units_won", "sales_order"],
			order_by="amount desc",
		)
		self.assertEqual([(w.bidder, w.units_won) for w in won], [(user("ann"), 2), (user("bob"), 2), (user("cat"), 1)])
		for winner in won:
			order = self.order(winner.sales_order)
			self.assertEqual((order.items[0].rate, order.items[0].qty), (20, winner.units_won))
		self.assertEqual(engine.public_state(auction, user("cat"), self.now + 2 * HOUR)["you_won_units"], 1)

	def test_a_bid_asks_for_whole_units_on_offer(self) -> None:
		auction = self.make(rules.UNIFORM)
		with self.assertRaisesRegex(BidRejected, "whole number"):
			engine.place_bid(auction.name, "20", user("ann"), self.now, qty="1.5")
		with self.assertRaisesRegex(BidRejected, "Only 5"):
			engine.place_bid(auction.name, "20", user("ann"), self.now, qty="6")

	def test_no_bid_over_the_reserve_leaves_it_unsold(self) -> None:
		auction = self.make(rules.UNIFORM, reserve_price=50)
		engine.place_bid(auction.name, "30", user("ann"), self.now, qty="2")
		self.assertEqual(engine.close(auction.name, self.now + 2 * HOUR), engine.UNSOLD)


class TestBestOffer(AuctionTestCase):
	def offers(self, auction: Any) -> dict[str, str]:
		rows = frappe.get_all(
			"Auction Bid",
			filters={"auction": auction.name, "bid_type": engine.OFFER},
			fields=["bidder", "amount", "offer_status"],
			order_by="placed_at asc, creation asc",
		)
		return {f"{row.bidder.split('.')[0]} {int(row.amount)}": row.offer_status for row in rows}

	def test_offers_are_decided_by_thresholds_or_left_for_the_seller(self) -> None:
		auction = self.make(rules.BEST_OFFER, auto_accept_offer=90, auto_decline_offer=50)
		engine.make_offer(auction.name, "40", user("bob"), self.now)
		state = engine.make_offer(auction.name, "70", user("cat"), self.now)
		self.assertEqual((state["your_offer"], state["your_offer_status"]), (70, rules.OFFER_PENDING))
		engine.make_offer(auction.name, "95", user("ann"), self.now)
		self.assertEqual(
			self.offers(auction),
			{"bob 40": rules.OFFER_DECLINED, "cat 70": rules.OFFER_DECLINED, "ann 95": rules.OFFER_ACCEPTED},
		)
		auction = self.reload(auction)
		self.assertEqual(
			(auction.status, auction.winning_bidder, auction.current_price), (engine.SOLD, user("ann"), 95)
		)
		self.assertEqual(self.order(auction.sales_order).items[0].rate, 95)

	def test_the_seller_accepts_or_declines_a_pending_offer(self) -> None:
		auction = self.make(rules.BEST_OFFER)
		engine.make_offer(auction.name, "60", user("bob"), self.now)
		engine.make_offer(auction.name, "70", user("cat"), self.now)
		bob, cat = (
			frappe.get_all("Auction Bid", filters={"auction": auction.name, "bidder": user(b)}, pluck="name")[0]
			for b in ("bob", "cat")
		)
		engine.decline_offer(bob)
		with self.assertRaisesRegex(BidRejected, "pending"):
			engine.accept_offer(bob, self.now)
		sales_order = engine.accept_offer(cat, self.now)
		self.assertEqual(self.order(sales_order).customer, customer("cat"))
		self.assertEqual(self.reload(auction).winning_bidder, user("cat"))

	def test_a_new_offer_replaces_the_buyers_pending_one(self) -> None:
		auction = self.make(rules.BEST_OFFER)
		engine.make_offer(auction.name, "60", user("bob"), self.now)
		engine.make_offer(auction.name, "65", user("bob"), self.now + timedelta(seconds=1))
		self.assertEqual(self.offers(auction), {"bob 60": rules.OFFER_REPLACED, "bob 65": rules.OFFER_PENDING})

	def test_buying_at_the_asking_price_declines_the_pending_offers(self) -> None:
		auction = self.make(rules.BEST_OFFER)
		engine.make_offer(auction.name, "60", user("bob"), self.now)
		with self.assertRaisesRegex(BidRejected, "buy it now"):
			engine.make_offer(auction.name, "100", user("ann"), self.now)
		engine.buy_now(auction.name, user("ann"), self.now)
		self.assertEqual(self.offers(auction), {"bob 60": rules.OFFER_DECLINED})
		self.assertEqual(self.reload(auction).current_price, 100)

	def test_pending_offers_expire_at_the_close_and_bids_are_refused(self) -> None:
		auction = self.make(rules.BEST_OFFER)
		with self.assertRaisesRegex(BidRejected, "offers"):
			self.bid(auction, "ann", "60")
		engine.make_offer(auction.name, "60", user("bob"), self.now)
		self.assertEqual(engine.close(auction.name, self.now + 2 * HOUR), engine.UNSOLD)
		self.assertEqual(self.offers(auction), {"bob 60": rules.OFFER_EXPIRED})

	def test_a_listing_needs_its_asking_price(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			self.make(rules.BEST_OFFER, buy_now_price=0)


class FakeGateway:
	"""A payment gateway controller that can hold payments, kept in memory: what a real gateway's
	authorize/release/capture would do, recorded so tests can check which holds were let go or taken."""

	max_hold_days = 7

	def __init__(self) -> None:
		self.requested: dict[str, dict[str, Any]] = {}
		self.released: list[str] = []
		self.captured: list[tuple[str, float]] = []

	def get_hold_url(self, **kwargs: Any) -> str:
		self.requested[kwargs["reference_docname"]] = kwargs
		return f"https://gateway.test/hold/{kwargs['reference_docname']}"

	def release_hold(self, hold_reference: str) -> None:
		self.released.append(hold_reference)

	def capture_hold(self, hold_reference: str, amount: float, currency: str) -> str:
		self.captured.append((hold_reference, amount))
		return f"txn-{hold_reference}"


class TestHolds(AuctionTestCase):
	def setUp(self) -> None:
		super().setUp()
		if not frappe.db.exists("Payment Gateway", GATEWAY):
			frappe.get_doc({"doctype": "Payment Gateway", "gateway": GATEWAY}).insert(ignore_permissions=True)
		account = frappe.db.get_value("Payment Gateway Account", {"payment_gateway": GATEWAY})
		if not account:
			account = (
				frappe.get_doc(
					{
						"doctype": "Payment Gateway Account",
						"payment_gateway": GATEWAY,
						"payment_account": GATEWAY_PAYMENT_ACCOUNT,
						"company": COMPANY,
					}
				)
				.insert(ignore_permissions=True)
				.name
			)
		self.account = account
		frappe.db.commit()
		self.gateway = FakeGateway()
		patcher = patch("auction.holds.provider_for", return_value=self.gateway)
		patcher.start()
		self.addCleanup(patcher.stop)

	def held(self, auction_type: str, **values: Any) -> Any:
		return self.make(
			auction_type, payment_timing=engine.AUTHORIZE_AT_BID, payment_gateway_account=self.account, **values
		)

	def row(self, auction: Any, buyer: str) -> Any:
		names = frappe.get_all(
			"Auction Bid",
			filters={"auction": auction.name, "bidder": user(buyer)},
			pluck="name",
			order_by="placed_at desc, creation desc",
		)
		return frappe.get_doc("Auction Bid", names[0])

	def authorize(self, auction: Any, buyer: str, at: datetime | None = None) -> Any:
		row = self.row(auction, buyer)
		engine.hold_authorized(row.name, f"ref-{buyer}", at or self.now)
		return frappe.get_doc("Auction Bid", row.name)

	def test_a_bid_counts_once_its_hold_is_authorized_and_the_winner_is_charged(self) -> None:
		auction = self.held(rules.ENGLISH)
		state = self.bid(auction, "ann", "50")
		row = self.row(auction, "ann")
		self.assertEqual(state["authorize_url"], f"https://gateway.test/hold/{row.name}")
		self.assertEqual((row.hold_status, row.hold_amount), (engine.AWAITING, 50))
		self.assertEqual(self.gateway.requested[row.name]["amount"], 50)
		self.assertEqual(engine.public_state(self.reload(auction), user("ann"), self.now)["bid_count"], 0)

		self.assertEqual(self.authorize(auction, "ann").hold_status, engine.AUTHORIZED)
		self.bid(auction, "bob", "60")
		self.authorize(auction, "bob")
		self.assertEqual(self.gateway.released, ["ref-ann"])
		self.assertEqual(self.row(auction, "ann").hold_status, engine.RELEASED)

		engine.close(auction.name, self.now + 2 * HOUR)
		auction = self.reload(auction)
		self.assertEqual((auction.winning_bidder, auction.current_price), (user("bob"), 51))
		self.assertEqual(self.gateway.captured, [("ref-bob", 51.0)])
		self.assertEqual(self.row(auction, "bob").hold_status, engine.CAPTURED)
		order = self.order(auction.sales_order)
		self.assertEqual(order.advance_paid, 51)
		entry = frappe.get_all(
			"Payment Entry Reference",
			filters={"reference_name": order.name, "docstatus": 1},
			fields=["parent", "allocated_amount"],
		)
		self.assertEqual([e.allocated_amount for e in entry], [51])

	def test_a_bid_overtaken_while_authorizing_fails_and_its_hold_is_released(self) -> None:
		auction = self.held(rules.ENGLISH)
		# The start price is a valid first bid; once bob's bid counts, the lowest bid is 11.
		self.bid(auction, "ann", "10")
		self.bid(auction, "bob", "30")
		self.authorize(auction, "bob")
		ann = self.authorize(auction, "ann")
		self.assertEqual(ann.hold_status, engine.FAILED)
		self.assertIn("lowest bid", ann.hold_error)
		self.assertEqual(self.gateway.released, ["ref-ann"])
		state = engine.public_state(self.reload(auction), user("ann"), self.now)
		self.assertEqual(state["your_hold"]["status"], engine.FAILED)
		self.assertEqual(state["bid_count"], 1)

	def test_a_refused_card_leaves_the_bid_uncounted(self) -> None:
		auction = self.held(rules.SEALED)
		self.bid(auction, "ann", "40")
		engine.hold_failed(self.row(auction, "ann").name, "Card declined")
		self.assertEqual(self.row(auction, "ann").hold_status, engine.FAILED)
		self.assertEqual(engine.close(auction.name, self.now + 2 * HOUR), engine.UNSOLD)

	def test_a_gateway_reporting_twice_changes_nothing_the_second_time(self) -> None:
		auction = self.held(rules.ENGLISH)
		self.bid(auction, "ann", "20")
		self.authorize(auction, "ann")
		self.authorize(auction, "ann")
		self.assertEqual(self.reload(auction).bid_count, 1)

	def test_losing_sealed_bids_are_released_at_the_close(self) -> None:
		auction = self.held(rules.SEALED)
		for buyer, amount in (("ann", "50"), ("bob", "40"), ("cat", "30")):
			self.bid(auction, buyer, amount)
			self.authorize(auction, buyer)
		engine.close(auction.name, self.now + 2 * HOUR)
		self.assertEqual(sorted(self.gateway.released), ["ref-bob", "ref-cat"])
		self.assertEqual(self.gateway.captured, [("ref-ann", 40.0)])

	def test_buy_now_takes_effect_when_authorized(self) -> None:
		auction = self.held(rules.ENGLISH, buy_now_price=80)
		engine.buy_now(auction.name, user("ann"), self.now)
		self.assertEqual(self.reload(auction).status, engine.SCHEDULED)
		self.authorize(auction, "ann")
		self.assertEqual(self.reload(auction).status, engine.SOLD)
		self.assertEqual(self.gateway.captured, [("ref-ann", 80.0)])

	def test_the_hold_page_confirm_checks_with_the_gateway(self) -> None:
		from auction import api
		from auction.gateways.stripe import API, StripeHolds
		from auction.tests.gateway_replay import Controller, Replay, exchange

		auction = self.held(rules.ENGLISH)
		self.bid(auction, "ann", "50")
		row = self.row(auction, "ann")
		row.db_set("hold_reference", "pi_ann")
		held = {"id": "pi_ann", "amount": 5000, "status": "requires_capture", "metadata": {"auction_bid": row.name}}
		unfinished = held | {"status": "requires_payment_method"}
		replay = Replay(
			exchange("GET", f"{API}/payment_intents/pi_ann", unfinished),
			exchange("GET", f"{API}/payment_intents/pi_ann", held),
		)
		stripe = StripeHolds(Controller("Stripe Settings", {"secret_key": "sk"}), transport=replay)
		with patch("auction.holds.provider_for", return_value=stripe):
			frappe.set_user(user("ann"))
			try:
				refused = api.confirm_hold(row.name)
				self.assertFalse(refused["ok"])
				self.assertEqual(self.row(auction, "ann").hold_status, engine.FAILED)
				self.bid(auction, "ann", "50")
				second = self.row(auction, "ann")
				second.db_set("hold_reference", "pi_ann")
				replay.pending[0]["response"]["metadata"]["auction_bid"] = second.name
				self.assertTrue(api.confirm_hold(second.name)["ok"])
			finally:
				frappe.set_user("Administrator")
		self.assertEqual(self.row(auction, "ann").hold_status, engine.AUTHORIZED)
		self.assertEqual(self.reload(auction).bid_count, 1)
		replay.assert_done()

	def test_an_auction_longer_than_the_gateway_holds_is_refused(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "7 days"):
			self.held(rules.ENGLISH, ends_at=self.now + timedelta(days=8))

	def test_a_gateway_that_cant_hold_is_refused(self) -> None:
		with patch("auction.holds.provider_for", return_value=None):
			with self.assertRaisesRegex(frappe.ValidationError, "can't hold"):
				self.held(rules.ENGLISH)


class TestSettleAs(AuctionTestCase):
	def test_a_win_can_end_in_a_submitted_sales_invoice(self) -> None:
		auction = self.make(rules.ENGLISH, settle_as="Sales Invoice")
		self.bid(auction, "ann", "40")
		engine.close(auction.name, self.now + 2 * HOUR)
		auction = self.reload(auction)
		self.assertFalse(auction.sales_order)
		invoice = frappe.get_doc("Sales Invoice", auction.sales_invoice)
		self.assertEqual((invoice.docstatus, invoice.customer), (1, customer("ann")))
		self.assertEqual((invoice.items[0].item_code, invoice.items[0].rate), (LOT, 10))
		winner = frappe.get_all("Auction Bid", filters={"auction": auction.name, "is_winner": 1}, pluck="sales_invoice")
		self.assertEqual(winner, [invoice.name])

	def test_dutch_purchases_return_their_invoice(self) -> None:
		auction = self.make(rules.DUTCH, settle_as="Sales Invoice")
		state = engine.accept_price(auction.name, "1", user("ann"), self.now)
		self.assertEqual(frappe.db.get_value("Sales Invoice", state["sales_invoice"], "docstatus"), 1)


class TestOpenAndClose(AuctionTestCase):
	def test_bids_are_refused_outside_the_auction_and_from_guests(self) -> None:
		auction = self.make(rules.ENGLISH)
		with self.assertRaises(BidRejected):
			self.bid(auction, "ann", "15", self.now - 2 * HOUR)
		with self.assertRaises(BidRejected):
			self.bid(auction, "ann", "15", self.now + 2 * HOUR)
		with self.assertRaisesRegex(BidRejected, "Sign in"):
			engine.place_bid(auction.name, "15", "Guest", self.now)
		auction.db_set("published", 0)
		with self.assertRaises(BidRejected):
			self.bid(auction, "ann", "15")

	def test_the_schedule_opens_started_auctions_and_settles_ended_ones(self) -> None:
		started = self.make(rules.ENGLISH)
		ended = self.make(rules.ENGLISH, starts_at=self.now - 2 * HOUR, ends_at=self.now + HOUR)
		self.bid(ended, "ann", "25")
		frappe.db.commit()
		engine.open_and_close_due(self.now + timedelta(minutes=30))
		self.assertEqual(self.reload(started).status, engine.OPEN)
		engine.open_and_close_due(self.now + 2 * HOUR)
		ended = self.reload(ended)
		self.assertEqual(ended.status, engine.SOLD)
		self.assertEqual(self.order(ended.sales_order).items[0].rate, 10)
		self.assertEqual(self.reload(started).status, engine.UNSOLD)

	def test_a_failed_settlement_is_recorded_and_not_retried(self) -> None:
		auction = self.make(rules.ENGLISH)
		self.bid(auction, "ann", "25")
		frappe.db.set_value("Item", LOT, "disabled", 1)
		frappe.db.commit()
		engine.open_and_close_due(self.now + 2 * HOUR)
		auction = self.reload(auction)
		self.assertEqual(auction.status, engine.OPEN)
		self.assertTrue(auction.settlement_error)
		modified = auction.modified
		engine.open_and_close_due(self.now + 3 * HOUR)
		self.assertEqual(self.reload(auction).modified, modified)

	def test_auctions_with_the_same_title_get_different_routes(self) -> None:
		first = self.make(rules.ENGLISH, title="Booster box")
		second = self.make(rules.ENGLISH, title="Booster box")
		self.assertTrue(first.route.startswith("auctions/booster-box-"))
		self.assertNotEqual(first.route, second.route)


class TestMoney(AuctionTestCase):
	def test_amounts_are_parsed_strictly(self) -> None:
		self.assertEqual(engine.money("12.346"), Decimal("12.35"))
		for bad in ("", "abc", "NaN", "Infinity"):
			with self.assertRaises(BidRejected):
				engine.money(bad)
