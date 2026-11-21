# SPDX-License-Identifier: AGPL-3.0-or-later
"""Razorpay holds: an order with ``payment_capture`` off, paid in Razorpay Checkout, leaves the payment
``authorized``; a win captures it. Razorpay has no void: an authorized payment that is never captured is
refunded to the buyer automatically (by default after 5 days), so releasing a hold lets it lapse.
https://razorpay.com/docs/payments/payments/capture-settings/"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from auction.gateways.base import HoldAdapter, HoldRefused, HoldRequest, minor_units

API = "https://api.razorpay.com/v1"
AUTHORIZED = "authorized"


class RazorpayHolds(HoldAdapter):
	max_hold_days = 5
	page_template = "templates/includes/auction_hold/razorpay.html"

	def _auth(self) -> tuple[str, str]:
		return (self.controller.api_key, self.secret("api_secret"))

	def page_context(self, hold: HoldRequest) -> tuple[dict[str, Any], str | None]:
		order = self.call(
			"POST",
			f"{API}/orders",
			auth=self._auth(),
			json={
				"amount": minor_units(hold.amount, hold.currency),
				"currency": hold.currency.upper(),
				"receipt": hold.name,
				"payment_capture": 0,
				"notes": {"auction_bid": hold.name},
			},
		)
		page = {
			"key": self.controller.api_key,
			"order_id": order["id"],
			"amount": order["amount"],
			"currency": order["currency"],
			"description": hold.description,
			"email": hold.payer_email,
		}
		return page, order["id"]

	def confirm(self, hold: HoldRequest, payload: dict[str, Any]) -> str:
		payment_id = payload.get("razorpay_payment_id") or ""
		signature = payload.get("razorpay_signature") or ""
		if not hold.reference or not payment_id:
			raise HoldRefused("Razorpay didn't authorize the amount.")
		expected = hmac.new(
			self.secret("api_secret").encode(), f"{hold.reference}|{payment_id}".encode(), hashlib.sha256
		).hexdigest()
		if not hmac.compare_digest(expected, signature):
			raise HoldRefused("The authorization doesn't match this bid.")
		payment = self.call("GET", f"{API}/payments/{payment_id}", auth=self._auth())
		if (
			payment.get("status") != AUTHORIZED
			or payment.get("order_id") != hold.reference
			or payment.get("amount") != minor_units(hold.amount, hold.currency)
		):
			raise HoldRefused("Razorpay didn't authorize the amount.")
		return payment_id

	def release_hold(self, hold_reference: str) -> None:
		"""Nothing to call: Razorpay refunds an uncaptured authorization on its own."""

	def capture_hold(self, hold_reference: str, amount: float, currency: str) -> str:
		payment = self.call(
			"POST",
			f"{API}/payments/{hold_reference}/capture",
			auth=self._auth(),
			json={"amount": minor_units(amount, currency), "currency": currency.upper()},
		)
		return payment["id"]
