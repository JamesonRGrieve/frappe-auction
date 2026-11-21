# SPDX-License-Identifier: AGPL-3.0-or-later
"""The methods an auction's web page calls. Bidding and buying need a signed-in user; reading the state
does not."""

from __future__ import annotations

from typing import Any

import frappe

from auction import engine
from auction.rules import BidRejected


def _published(name: str) -> Any:
	auction = frappe.get_doc("Auction", name)
	if not auction.published:
		raise frappe.DoesNotExistError
	return auction


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_state(auction: str) -> dict[str, Any]:
	return engine.public_state(_published(auction), frappe.session.user)


@frappe.whitelist(methods=["POST"])
def place_bid(auction: str, amount: str) -> dict[str, Any]:
	_published(auction)
	try:
		return engine.place_bid(auction, amount, frappe.session.user)
	except BidRejected as rejected:
		frappe.throw(str(rejected), title=frappe._("Bid not accepted"))


@frappe.whitelist(methods=["POST"])
def accept_price(auction: str, qty: str = "1") -> dict[str, Any]:
	_published(auction)
	try:
		return engine.accept_price(auction, qty, frappe.session.user)
	except BidRejected as rejected:
		frappe.throw(str(rejected), title=frappe._("Purchase not accepted"))
