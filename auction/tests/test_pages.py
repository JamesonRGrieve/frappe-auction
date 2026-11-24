# SPDX-License-Identifier: AGPL-3.0-or-later
"""The web pages render through Frappe's website stack, as a visitor's request would: /auctions, each format's
auction page for a guest and for a bidder, and /auction-hold for a held bid. A template error shows here as a
500, before any site serves it."""

from __future__ import annotations

from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

import frappe
from frappe.utils import set_request
from frappe.website.serve import get_response

from auction import engine, rules
from auction.tests.test_engine import HOUR, AuctionTestCase, ensure_gateway_account, user

OK = 200


def render_page(path: str, as_user: str = "Guest") -> tuple[int, str]:
	"""(status, body) of a GET of `path` through Frappe's website stack, as `as_user`."""
	frappe.set_user(as_user)
	try:
		set_request(method="GET", path=path)
		# A real request fills form_dict from the query string; the test request doesn't.
		frappe.local.form_dict = frappe._dict(parse_qsl(urlsplit(path).query))
		response = get_response(urlsplit(path).path)
		return response.status_code, response.get_data(as_text=True)
	finally:
		frappe.set_user("Administrator")


class TestPages(AuctionTestCase):
	def render(self, path: str, as_user: str = "Guest") -> str:
		status, body = render_page(path, as_user)
		self.assertEqual(status, OK, f"{path} as {as_user}: {body[:2000]}")
		return body

	def test_the_list_shows_open_auctions(self) -> None:
		auction = self.make(rules.ENGLISH, title="Listed lot")
		frappe.db.commit()
		body = self.render("/auctions")
		self.assertIn("Listed lot", body)
		self.assertIn(auction.route, body)

	def test_every_formats_page_renders_for_guests_and_bidders(self) -> None:
		formats = {
			rules.ENGLISH: {"buy_now_price": 80},
			rules.DUTCH: {"quantity": 3},
			rules.SEALED: {"buy_now_price": 80},
			rules.UNIFORM: {},
			rules.BEST_OFFER: {},
		}
		for auction_type, values in formats.items():
			auction = self.make(auction_type, title=f"Page {auction_type}", **values)
			frappe.db.commit()
			guest = self.render(f"/{auction.route}")
			self.assertIn(f"Page {auction_type}", guest)
			self.assertIn("auction-panel", guest)
			self.assertIn('"signed_in": true', self.render(f"/{auction.route}", user("ann")))

	def test_a_closed_auction_page_renders(self) -> None:
		auction = self.make(rules.SEALED, title="Closed lot")
		self.bid(auction, "ann", "40")
		engine.close(auction.name, self.now + 2 * HOUR)
		frappe.db.commit()
		self.assertIn("Closed lot", self.render(f"/{auction.route}", user("ann")))

	def test_the_hold_page_renders_the_gateway_form(self) -> None:
		from auction.gateways.stripe import API, StripeHolds
		from auction.tests.gateway_replay import Controller, Replay, exchange

		gateway = ensure_gateway_account()
		intent = {"id": "pi_page", "client_secret": "pi_page_secret", "status": "requires_payment_method"}
		stripe = StripeHolds(
			Controller("Stripe Settings", {"secret_key": "sk"}, publishable_key="pk_page"),
			transport=Replay(exchange("POST", f"{API}/payment_intents", intent)),
		)
		with patch("auction.holds.provider_for", return_value=stripe):
			auction = self.make(rules.ENGLISH, payment_timing=engine.AUTHORIZE_AT_BID, payment_gateway_account=gateway)
			self.bid(auction, "ann", "50")
			row = frappe.get_all("Auction Bid", filters={"auction": auction.name}, pluck="name")[0]
			frappe.db.commit()
			body = self.render(f"/auction-hold?bid={row}", user("ann"))
		self.assertIn("pk_page", body)
		self.assertIn("pi_page_secret", body)
		self.assertEqual(frappe.db.get_value("Auction Bid", row, "hold_reference"), "pi_page")
