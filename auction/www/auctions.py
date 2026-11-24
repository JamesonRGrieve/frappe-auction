# SPDX-License-Identifier: AGPL-3.0-or-later
"""/auctions: the published auctions that are open or coming up, soonest to end first."""

from __future__ import annotations

import frappe
from frappe import _

from auction import stores
from auction.engine import CLOSED_STATUSES

no_cache = 1
FIELDS = ["title", "route", "image", "auction_type", "status", "starts_at", "ends_at"]


def get_context(context: frappe._dict) -> None:
	context.title = _("Auctions")
	context.auctions = frappe.get_all(
		"Auction",
		filters={"published": 1, "status": ["not in", CLOSED_STATUSES], **stores.filters()},
		fields=FIELDS,
		order_by="ends_at asc",
	)
