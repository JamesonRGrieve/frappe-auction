# SPDX-License-Identifier: AGPL-3.0-or-later
"""The methods an auction's web page calls, and the seller's answers to offers. Bidding, buying and offering
need a signed-in user; reading the state does not. A refusal from the rules reaches the caller as its message,
and the request's transaction rolls back."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import frappe
from frappe import _

from auction import engine, holds, stores
from auction.gateways.base import HoldRefused
from auction.rules import BidRejected

SELLER_ROLES = ("Sales Manager", "System Manager")


def _published(name: str) -> Any:
	auction = frappe.get_doc("Auction", name)
	if not auction.published:
		raise frappe.DoesNotExistError
	stores.require_visible(auction)
	return auction


@contextmanager
def _refusals(title: str) -> Iterator[None]:
	try:
		yield
	except BidRejected as rejected:
		frappe.throw(str(rejected), title=title)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_state(auction: str) -> dict[str, Any]:
	return engine.public_state(_published(auction), frappe.session.user)


@frappe.whitelist(methods=["POST"])
def place_bid(auction: str, amount: str, qty: str | None = None) -> dict[str, Any]:
	_published(auction)
	with _refusals(_("Bid not accepted")):
		return engine.place_bid(auction, amount, frappe.session.user, qty=qty)


@frappe.whitelist(methods=["POST"])
def accept_price(auction: str, qty: str = "1") -> dict[str, Any]:
	_published(auction)
	with _refusals(_("Purchase not accepted")):
		return engine.accept_price(auction, qty, frappe.session.user)


@frappe.whitelist(methods=["POST"])
def buy_now(auction: str) -> dict[str, Any]:
	_published(auction)
	with _refusals(_("Purchase not accepted")):
		return engine.buy_now(auction, frappe.session.user)


@frappe.whitelist(methods=["POST"])
def make_offer(auction: str, amount: str) -> dict[str, Any]:
	_published(auction)
	with _refusals(_("Offer not accepted")):
		return engine.make_offer(auction, amount, frappe.session.user)


@frappe.whitelist(methods=["POST"])
def accept_offer(bid: str) -> str:
	frappe.only_for(SELLER_ROLES)
	with _refusals(_("Offer not accepted")):
		return engine.accept_offer(bid)


@frappe.whitelist(methods=["POST"])
def decline_offer(bid: str) -> None:
	frappe.only_for(SELLER_ROLES)
	with _refusals(_("Offer not declined")):
		engine.decline_offer(bid)


@frappe.whitelist(methods=["POST"])
def confirm_hold(bid: str, **payload: Any) -> dict[str, Any]:
	"""The buyer's gateway form finished: check with the gateway that the amount is held, then let the row
	count (or fail it, with the gateway's reason)."""
	payload.pop("cmd", None)
	row = frappe.get_doc("Auction Bid", bid)
	if row.bidder != frappe.session.user:
		raise frappe.PermissionError
	auction = frappe.get_doc("Auction", row.auction)
	stores.require_visible(auction)
	adapter = holds.require_provider(auction.payment_gateway_account)
	hold = holds.hold_request(auction, row)
	try:
		reference = adapter.confirm(hold, payload)
	except HoldRefused as refused:
		engine.hold_failed(bid, str(refused))
		return {"ok": False, "error": str(refused), "return_url": hold.return_url}
	engine.hold_authorized(bid, reference)
	return {"ok": True, "return_url": hold.return_url}
