# SPDX-License-Identifier: AGPL-3.0-or-later
"""Auctions scoped to webshop stores (frappe-webshop fork): the field the install adds, an auction selling only
for its store's company, and, on a store's host, only that store's auctions listed and served (another store's
auction is a 404 page and a missing document to the API), while a request no store claims sees them all.

The store a request belongs to comes from frappe-public-site-router by host; here it is set through
``auction.stores.current_store``. The bench installs the webshop for these tests (BENCH_EXTRA_APPS)."""

from __future__ import annotations

from unittest.mock import patch

import frappe

from auction import api, rules, stores
from auction.tests.test_engine import COMPANY, AuctionTestCase
from auction.tests.test_pages import render_page

STORE = "_Test Auction Store"
OTHER_STORE = "_Test Auction Other Store"
OTHER_COMPANY = "_Test Company 1"
NOT_FOUND = 404


def ensure_store(name: str, company: str) -> None:
	if frappe.db.exists(stores.STORE_DOCTYPE, name):
		return
	currency = frappe.db.get_value("Company", company, "default_currency")
	price_list = frappe.db.get_value("Price List", {"selling": 1, "enabled": 1, "currency": currency}, "name")
	if not price_list:
		price_list = (
			frappe.get_doc(
				{
					"doctype": "Price List",
					"price_list_name": f"_Test Auction Selling {currency}",
					"currency": currency,
					"selling": 1,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)
	frappe.get_doc(
		{
			"doctype": stores.STORE_DOCTYPE,
			"store_name": name,
			"enabled": 1,
			"company": company,
			"price_list": price_list,
			"default_customer_group": "_Test Customer Group",
			"quotation_series": "SAL-QTN-.YYYY.-",
		}
	).insert(ignore_permissions=True)


class TestStores(AuctionTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.assertTrue(stores.available(), "these tests need the webshop installed on the bench")
		ensure_store(STORE, COMPANY)
		ensure_store(OTHER_STORE, OTHER_COMPANY)
		frappe.db.commit()

	def test_the_install_adds_the_store_field(self) -> None:
		self.assertTrue(frappe.get_meta("Auction").has_field(stores.FIELD))

	def test_an_auction_sells_for_its_stores_company(self) -> None:
		self.make(rules.ENGLISH, webshop_store=STORE)
		with self.assertRaisesRegex(frappe.ValidationError, OTHER_COMPANY):
			self.make(rules.ENGLISH, webshop_store=OTHER_STORE)

	def test_a_stores_host_shows_only_its_own_auctions(self) -> None:
		ours = self.make(rules.ENGLISH, title="Store lot", webshop_store=STORE)
		elsewhere = self.make(rules.ENGLISH, title="Unscoped lot")
		frappe.db.commit()
		with patch("auction.stores.current_store", return_value=STORE):
			status, listing = render_page("/auctions")
			self.assertEqual(status, 200)
			self.assertIn("Store lot", listing)
			self.assertNotIn("Unscoped lot", listing)
			self.assertEqual(render_page(f"/{ours.route}")[0], 200)
			self.assertEqual(render_page(f"/{elsewhere.route}")[0], NOT_FOUND)
			with self.assertRaises(frappe.DoesNotExistError):
				api.get_state(elsewhere.name)
			self.assertEqual(api.get_state(ours.name)["name"], ours.name)

	def test_a_request_no_store_claims_sees_every_auction(self) -> None:
		self.make(rules.ENGLISH, title="Store lot", webshop_store=STORE)
		self.make(rules.ENGLISH, title="Unscoped lot")
		frappe.db.commit()
		with patch("auction.stores.current_store", return_value=None):
			listing = render_page("/auctions")[1]
		self.assertIn("Store lot", listing)
		self.assertIn("Unscoped lot", listing)
