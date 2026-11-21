# SPDX-License-Identifier: AGPL-3.0-or-later
"""PayPal holds through the NVP Express Checkout API the payments app's PayPal Settings use: an Authorization
(``PAYMENTREQUEST_0_PAYMENTACTION=Authorization``), approved by the buyer on PayPal, then ``DoCapture`` on a win
or ``DoVoid`` on a loss. PayPal honours an authorization for 3 days.
https://developer.paypal.com/api/nvp-soap/do-capture-nvp/"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlencode

from auction.gateways.base import TIMEOUT_SECONDS, GatewayError, HoldAdapter, HoldRefused, HoldRequest, decimal_text

VERSION = "98"
PRODUCTION = ("https://api-3t.paypal.com/nvp", "https://www.paypal.com/cgi-bin/webscr")
SANDBOX = ("https://api-3t.sandbox.paypal.com/nvp", "https://www.sandbox.paypal.com/cgi-bin/webscr")
SUCCESS = ("Success", "SuccessWithWarning")


class PayPalHolds(HoldAdapter):
	max_hold_days = 3
	page_template = "templates/includes/auction_hold/paypal.html"

	def _endpoints(self) -> tuple[str, str]:
		return SANDBOX if self.controller.paypal_sandbox else PRODUCTION

	def _nvp(self, method: str, **fields: str) -> dict[str, str]:
		response = self.transport(
			"POST",
			self._endpoints()[0],
			timeout=TIMEOUT_SECONDS,
			data={
				"METHOD": method,
				"VERSION": VERSION,
				"USER": self.controller.api_username,
				"PWD": self.secret("api_password"),
				"SIGNATURE": self.controller.signature,
				**fields,
			},
		)
		if response.status_code >= 400:
			raise GatewayError(f"PayPalHolds: {method} answered {response.status_code}")
		reply = {key: values[0] for key, values in parse_qs(response.text).items()}
		if reply.get("ACK") not in SUCCESS:
			raise GatewayError(f"PayPalHolds: {method} failed ({reply.get('L_ERRORCODE0', '?')})")
		return reply

	def page_context(self, hold: HoldRequest) -> tuple[dict[str, Any], str | None]:
		back = f"{hold.hold_url}&paypal=return"
		reply = self._nvp(
			"SetExpressCheckout",
			PAYMENTREQUEST_0_PAYMENTACTION="Authorization",
			PAYMENTREQUEST_0_AMT=decimal_text(hold.amount, hold.currency),
			PAYMENTREQUEST_0_CURRENCYCODE=hold.currency.upper(),
			PAYMENTREQUEST_0_DESC=hold.description,
			PAYMENTREQUEST_0_INVNUM=hold.name,
			RETURNURL=back,
			CANCELURL=hold.return_url,
			NOSHIPPING="1",
		)
		approve = f"{self._endpoints()[1]}?{urlencode({'cmd': '_express-checkout', 'token': reply['TOKEN']})}"
		return {"approve_url": approve}, reply["TOKEN"]

	def confirm(self, hold: HoldRequest, payload: dict[str, Any]) -> str:
		token, payer = payload.get("token"), payload.get("PayerID")
		if not token or not payer or token != hold.reference:
			raise HoldRefused("PayPal didn't approve this hold.")
		try:
			reply = self._nvp(
				"DoExpressCheckoutPayment",
				TOKEN=token,
				PAYERID=payer,
				PAYMENTREQUEST_0_PAYMENTACTION="Authorization",
				PAYMENTREQUEST_0_AMT=decimal_text(hold.amount, hold.currency),
				PAYMENTREQUEST_0_CURRENCYCODE=hold.currency.upper(),
				PAYMENTREQUEST_0_INVNUM=hold.name,
			)
		except GatewayError as error:
			raise HoldRefused("PayPal didn't authorize the amount.") from error
		if reply.get("PAYMENTINFO_0_PENDINGREASON") != "authorization":
			raise HoldRefused("PayPal didn't authorize the amount.")
		return reply["PAYMENTINFO_0_TRANSACTIONID"]

	def release_hold(self, hold_reference: str) -> None:
		self._nvp("DoVoid", AUTHORIZATIONID=hold_reference)

	def capture_hold(self, hold_reference: str, amount: float, currency: str) -> str:
		reply = self._nvp(
			"DoCapture",
			AUTHORIZATIONID=hold_reference,
			AMT=decimal_text(amount, currency),
			CURRENCYCODE=currency.upper(),
			COMPLETETYPE="Complete",
		)
		return reply["TRANSACTIONID"]
