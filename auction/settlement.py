# SPDX-License-Identifier: AGPL-3.0-or-later
"""The accounting documents a sale makes: a submitted Sales Order or Sales Invoice (the auction's "Settle As")
for the buyer at the auction price, a Payment Entry for a captured card hold, and a Payment Request for what is
still owed when the auction names a gateway."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import frappe
from frappe.utils import flt, nowdate

SALES_ORDER = "Sales Order"
SALES_INVOICE = "Sales Invoice"
SETTLE_AS = (SALES_ORDER, SALES_INVOICE)


@dataclass(frozen=True)
class Settlement:
	doctype: str
	name: str

	@property
	def field(self) -> str:
		"""The Link field on Auction and Auction Bid that holds this document."""
		return "sales_invoice" if self.doctype == SALES_INVOICE else "sales_order"


def create(auction: Any, customer: str, qty: Decimal, rate: Decimal) -> tuple[Settlement, Any]:
	"""The buyer's submitted order or invoice for `qty` of the auction's item at `rate`."""
	doctype = auction.settle_as or SALES_ORDER
	if doctype not in SETTLE_AS:
		raise ValueError(f"Unknown Settle As {doctype!r}")
	today = nowdate()
	item: dict[str, Any] = {
		"item_code": auction.item,
		"qty": flt(qty),
		"rate": flt(rate),
		"price_list_rate": flt(rate),
		"warehouse": auction.warehouse,
	}
	doc: dict[str, Any] = {
		"doctype": doctype,
		"company": auction.company,
		"customer": customer,
		"currency": auction.currency,
		"ignore_pricing_rule": 1,
		"items": [item],
	}
	if doctype == SALES_ORDER:
		item["delivery_date"] = today
		doc |= {"order_type": "Shopping Cart", "transaction_date": today, "delivery_date": today}
	else:
		# Stock leaves with a Delivery Note, as it does after a Sales Order.
		doc |= {"posting_date": today, "due_date": today, "update_stock": 0}
	document = frappe.get_doc(doc)
	if auction.taxes_and_charges:
		from erpnext.controllers.accounts_controller import get_taxes_and_charges

		document.taxes_and_charges = auction.taxes_and_charges
		document.set("taxes", get_taxes_and_charges("Sales Taxes and Charges Template", auction.taxes_and_charges))
	document.insert()
	document.submit()
	return Settlement(doctype, document.name), document


def book_payment(auction: Any, settlement: Settlement, amount: Decimal, transaction_id: str) -> str:
	"""A submitted Payment Entry for a captured hold, into the gateway account's payment account."""
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	payment_account = frappe.db.get_value("Payment Gateway Account", auction.payment_gateway_account, "payment_account")
	entry = get_payment_entry(
		settlement.doctype, settlement.name, party_amount=flt(amount), bank_account=payment_account
	)
	entry.reference_no = transaction_id
	entry.reference_date = nowdate()
	entry.insert()
	entry.submit()
	return entry.name


def outstanding(settlement: Settlement) -> float:
	if settlement.doctype == SALES_INVOICE:
		return flt(frappe.db.get_value(SALES_INVOICE, settlement.name, "outstanding_amount"))
	grand_total, advance = frappe.db.get_value(SALES_ORDER, settlement.name, ["grand_total", "advance_paid"])
	return flt(grand_total) - flt(advance)


def request_payment(auction: Any, settlement: Settlement) -> str | None:
	"""A Payment Request emailed to the buyer for what is still owed, when the auction names a gateway."""
	if not auction.payment_gateway_account or outstanding(settlement) <= 0:
		return None
	from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request

	request = make_payment_request(
		dt=settlement.doctype,
		dn=settlement.name,
		payment_gateway_account=auction.payment_gateway_account,
		submit_doc=1,
		mute_email=0,
		order_type="Shopping Cart",
		return_doc=1,
	)
	return request.name
