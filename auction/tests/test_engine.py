# SPDX-License-Identifier: AGPL-3.0-or-later
"""The engine against the real ERPNext test DB: bids, Dutch purchases, closing and settlement into submitted
Sales Orders, the scheduled open and close, and what a visitor sees. ``open_and_close_due`` commits, so
every test works on its own lot item and clears that item's auctions first. ``ERPNextTestSuite`` is the base
because importing it builds ERPNext's test records (company, customer group, territory, tax template)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import frappe
from erpnext.tests.utils import ERPNextTestSuite
from frappe.utils import now_datetime

from auction import engine, rules
from auction.rules import BidRejected

COMPANY = "_Test Company"
LOT = "_Test Auction Lot"
TAX_TEMPLATE = "_Test Sales Taxes and Charges Template - _TC"
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
		self.assertEqual(state["units_left"], 1)
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
