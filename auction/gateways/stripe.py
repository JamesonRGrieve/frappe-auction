# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stripe holds: a PaymentIntent with ``capture_method=manual``, confirmed in the browser with Stripe.js, then
captured (up to the authorized amount) or cancelled. Stripe keeps a card authorization for 7 days.
https://docs.stripe.com/payments/place-a-hold-on-a-payment-method"""

from __future__ import annotations

from typing import Any

from auction.gateways.base import HoldAdapter, HoldRefused, HoldRequest, minor_units

API = "https://api.stripe.com/v1"
HELD = "requires_capture"
# A PaymentIntent the buyer can still complete; any other status needs a fresh one.
REUSABLE = ("requires_payment_method", "requires_confirmation", "requires_action")


class StripeHolds(HoldAdapter):
	max_hold_days = 7
	page_template = "templates/includes/auction_hold/stripe.html"

	def _headers(self) -> dict[str, str]:
		return {"Authorization": f"Bearer {self.secret('secret_key')}"}

	def _intent(self, intent_id: str) -> dict[str, Any]:
		return self.call("GET", f"{API}/payment_intents/{intent_id}", headers=self._headers())

	def page_context(self, hold: HoldRequest) -> tuple[dict[str, Any], str | None]:
		intent = self._intent(hold.reference) if hold.reference else None
		if not intent or intent.get("status") not in REUSABLE:
			intent = self.call(
				"POST",
				f"{API}/payment_intents",
				headers=self._headers(),
				data={
					"amount": minor_units(hold.amount, hold.currency),
					"currency": hold.currency.lower(),
					"capture_method": "manual",
					"payment_method_types[]": "card",
					"description": hold.description,
					"receipt_email": hold.payer_email,
					"metadata[auction_bid]": hold.name,
				},
			)
		return {"publishable_key": self.controller.publishable_key, "client_secret": intent["client_secret"]}, intent[
			"id"
		]

	def confirm(self, hold: HoldRequest, payload: dict[str, Any]) -> str:
		if not hold.reference:
			raise HoldRefused("No card authorization was started.")
		intent = self._intent(hold.reference)
		if intent.get("status") != HELD:
			raise HoldRefused("Your card didn't authorize the amount.")
		if (
			intent.get("amount") != minor_units(hold.amount, hold.currency)
			or (intent.get("metadata") or {}).get("auction_bid") != hold.name
		):
			raise HoldRefused("The authorization doesn't match this bid.")
		return intent["id"]

	def release_hold(self, hold_reference: str) -> None:
		self.call("POST", f"{API}/payment_intents/{hold_reference}/cancel", headers=self._headers())

	def capture_hold(self, hold_reference: str, amount: float, currency: str) -> str:
		intent = self.call(
			"POST",
			f"{API}/payment_intents/{hold_reference}/capture",
			headers=self._headers(),
			data={"amount_to_capture": minor_units(amount, currency)},
		)
		return intent.get("latest_charge") or intent["id"]
