# SPDX-License-Identifier: AGPL-3.0-or-later
"""Card holds for auctions set to "Authorize at Bid": the buyer authorizes the amount when they bid, buy or
offer, the hold is captured when they win, and released when they are outbid or lose.

Holds go through the site's payment gateways (the ``payments`` app's Payment Gateway), and no gateway is
named here. A gateway supports holds when its controller (``payments.utils.get_payment_gateway_controller``,
the gateway's settings document) has these members:

``max_hold_days`` (int)
    How long the gateway keeps an authorization. An auction longer than this can't hold payments.
``get_hold_url(*, amount, currency, title, description, payer_email, reference_doctype, reference_docname,
on_authorized, on_failed, return_url) -> str``
    The page where the buyer authorizes ``amount``. Once the gateway holds it, the gateway calls
    ``frappe.get_attr(on_authorized)(reference_docname, hold_reference)``; if the card is refused,
    ``frappe.get_attr(on_failed)(reference_docname, reason)``. Then it sends the buyer to ``return_url``.
``release_hold(hold_reference) -> None``
    Lets the authorization go.
``capture_hold(hold_reference, amount) -> str``
    Takes up to the authorized amount; returns the gateway's transaction id.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import frappe
from frappe import _
from frappe.utils import flt, get_url

HOLD_METHODS = ("get_hold_url", "release_hold", "capture_hold")
ON_AUTHORIZED = "auction.engine.hold_authorized"
ON_FAILED = "auction.engine.hold_failed"


def provider_for(payment_gateway_account: str | None) -> Any | None:
	"""The gateway controller behind a Payment Gateway Account, if it can hold payments."""
	if not payment_gateway_account:
		return None
	gateway = frappe.db.get_value("Payment Gateway Account", payment_gateway_account, "payment_gateway")
	if not gateway or "payments" not in frappe.get_installed_apps():
		return None
	from payments.utils import get_payment_gateway_controller

	controller = get_payment_gateway_controller(gateway)
	if all(callable(getattr(controller, method, None)) for method in HOLD_METHODS):
		return controller
	return None


def require_provider(payment_gateway_account: str | None) -> Any:
	provider = provider_for(payment_gateway_account)
	if provider is None:
		frappe.throw(
			_("Payment Gateway Account {0} can't hold card payments; choose another, or After Close.").format(
				payment_gateway_account or _("(none)")
			)
		)
	return provider


def request(auction: Any, row: Any) -> str:
	"""The page where the buyer authorizes this row's hold."""
	provider = require_provider(auction.payment_gateway_account)
	return provider.get_hold_url(
		amount=flt(row.hold_amount),
		currency=auction.currency,
		title=auction.title,
		description=_("Held for your {0} on {1}").format(_(row.bid_type).lower(), auction.title),
		payer_email=row.bidder,
		reference_doctype="Auction Bid",
		reference_docname=row.name,
		on_authorized=ON_AUTHORIZED,
		on_failed=ON_FAILED,
		return_url=get_url(f"/{auction.route}"),
	)


def release(auction: Any, hold_reference: str) -> None:
	require_provider(auction.payment_gateway_account).release_hold(hold_reference)


def capture(auction: Any, hold_reference: str, amount: Decimal) -> str:
	return require_provider(auction.payment_gateway_account).capture_hold(hold_reference, flt(amount))
