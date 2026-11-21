# SPDX-License-Identifier: AGPL-3.0-or-later
"""/auction-hold?bid=<Auction Bid> (this module is auction_hold.py: Frappe finds a www page's controller by its
name with "-" turned into "_"): where a buyer authorizes the hold for their own bid, purchase or offer,
through the auction gateway's card form (an ``auction.gateways`` adapter)."""

from __future__ import annotations

import frappe
from frappe import _

from auction import engine, holds

no_cache = 1


def get_context(context: frappe._dict) -> None:
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = f"/login?redirect-to=/auction-hold?bid={frappe.form_dict.bid}"
		raise frappe.Redirect
	row = frappe.get_doc("Auction Bid", frappe.form_dict.bid)
	if row.bidder != frappe.session.user:
		raise frappe.PermissionError
	auction = frappe.get_doc("Auction", row.auction)
	context.title = _("Authorize your card")
	context.auction = auction
	context.row = row
	context.waiting = row.hold_status == engine.AWAITING
	if not context.waiting:
		return
	adapter = holds.require_provider(auction.payment_gateway_account)
	hold = holds.hold_request(auction, row)
	page, reference = adapter.page_context(hold)
	if reference and reference != row.hold_reference:
		row.db_set("hold_reference", reference)
	context.hold = hold
	context.page = page
	context.hold_template = adapter.page_template
