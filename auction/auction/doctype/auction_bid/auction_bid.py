# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import frappe
from frappe.model.document import Document


class AuctionBid(Document):
	"""Written only by ``auction.engine``, which validates the bid against the auction's rules."""


def on_doctype_update() -> None:
	frappe.db.add_index("Auction Bid", ["auction", "placed_at"])
