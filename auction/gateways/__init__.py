# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hold adapters for the site's payment gateways, so "Authorize at Bid" works with every gateway whose API can
authorize a card payment and capture it later. Keyed by the gateway's settings DocType (what
``payments.utils.get_payment_gateway_controller`` returns). A gateway that implements the hold contract itself
(see ``auction.holds``) is used directly and needs no entry here."""

from __future__ import annotations

from typing import Any

ADAPTERS: dict[str, str] = {
	"Stripe Settings": "auction.gateways.stripe.StripeHolds",
	"Braintree Settings": "auction.gateways.braintree.BraintreeHolds",
	"PayPal Settings": "auction.gateways.paypal.PayPalHolds",
	"Razorpay Settings": "auction.gateways.razorpay.RazorpayHolds",
}

# Gateways whose API can't hold a payment; auctions through them settle After Close.
CANT_HOLD: dict[str, str] = {
	"Mpesa Settings": "M-Pesa asks the buyer's phone to send a payment; there is nothing to hold.",
	"GoCardless Settings": "GoCardless collects by direct debit; there is no card authorization to hold.",
}


def adapter_for(controller: Any) -> Any | None:
	import frappe

	path = ADAPTERS.get(controller.doctype)
	return frappe.get_attr(path)(controller) if path else None
