# SPDX-License-Identifier: AGPL-3.0-or-later
"""Braintree holds through its GraphQL API: Drop-in turns the card into a single-use nonce, the server
authorizes it (``authorizePaymentMethod``), and the win captures it (``captureTransaction``) or the loss
voids it (``reverseTransaction``). Card authorizations last about 7 days.
https://graphql.braintreepayments.com/guides/payment_method_authorization/"""

from __future__ import annotations

import base64
from typing import Any

from auction.gateways.base import GatewayError, HoldAdapter, HoldRefused, HoldRequest, decimal_text

PRODUCTION = "https://payments.braintree-api.com/graphql"
SANDBOX = "https://payments.sandbox.braintree-api.com/graphql"
API_VERSION = "2019-01-01"
AUTHORIZED = "AUTHORIZED"

CLIENT_TOKEN = "mutation { createClientToken(input: {}) { clientToken } }"
AUTHORIZE = """mutation Authorize($input: AuthorizePaymentMethodInput!) {
  authorizePaymentMethod(input: $input) { transaction { id status orderId amount { value currencyCode } } }
}"""
CAPTURE = """mutation Capture($input: CaptureTransactionInput!) {
  captureTransaction(input: $input) { transaction { id status } }
}"""
REVERSE = """mutation Reverse($input: ReverseTransactionInput!) {
  reverseTransaction(input: $input) { reversal { ... on Transaction { id status } } }
}"""


class BraintreeHolds(HoldAdapter):
	max_hold_days = 7
	page_template = "templates/includes/auction_hold/braintree.html"

	def _query(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
		keys = f"{self.controller.public_key}:{self.secret('private_key')}".encode()
		body = self.call(
			"POST",
			SANDBOX if self.controller.use_sandbox else PRODUCTION,
			headers={"Authorization": f"Basic {base64.b64encode(keys).decode()}", "Braintree-Version": API_VERSION},
			json={"query": query, "variables": variables or {}},
		)
		if body.get("errors"):
			raise GatewayError(f"BraintreeHolds: {body['errors'][0].get('message', 'error')}")
		return body["data"]

	def page_context(self, hold: HoldRequest) -> tuple[dict[str, Any], str | None]:
		return {"client_token": self._query(CLIENT_TOKEN)["createClientToken"]["clientToken"]}, None

	def confirm(self, hold: HoldRequest, payload: dict[str, Any]) -> str:
		nonce = payload.get("nonce")
		if not nonce:
			raise HoldRefused("No card was given.")
		try:
			data = self._query(
				AUTHORIZE,
				{
					"input": {
						"paymentMethodId": nonce,
						"transaction": {"amount": decimal_text(hold.amount, hold.currency), "orderId": hold.name},
					}
				},
			)
		except GatewayError as error:
			raise HoldRefused("Your card didn't authorize the amount.") from error
		transaction = data["authorizePaymentMethod"]["transaction"]
		if transaction["status"] != AUTHORIZED:
			raise HoldRefused("Your card didn't authorize the amount.")
		return transaction["id"]

	def release_hold(self, hold_reference: str) -> None:
		self._query(REVERSE, {"input": {"referenceId": hold_reference}})

	def capture_hold(self, hold_reference: str, amount: float, currency: str) -> str:
		data = self._query(
			CAPTURE,
			{"input": {"transactionId": hold_reference, "transaction": {"amount": decimal_text(amount, currency)}}},
		)
		return data["captureTransaction"]["transaction"]["id"]
