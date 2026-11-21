# SPDX-License-Identifier: AGPL-3.0-or-later
"""The hold adapters against recorded gateway exchanges. Runs without a bench:
``python3 -m unittest auction.tests.test_gateways`` (needs ``requests``)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import unittest
from decimal import Decimal
from urllib.parse import urlencode

from auction.gateways import braintree, paypal, razorpay
from auction.gateways.base import GatewayError, HoldRefused, HoldRequest, decimal_text, minor_units
from auction.gateways.stripe import API as STRIPE
from auction.gateways.stripe import StripeHolds
from auction.tests.gateway_replay import Controller, Replay, exchange

STRIPE_SECRET = "sk_test_auction"
STRIPE_AUTH = {"Authorization": f"Bearer {STRIPE_SECRET}"}


def hold(reference: str | None = None, amount: str = "50.00", currency: str = "CAD") -> HoldRequest:
	return HoldRequest(
		name="bid-1",
		amount=Decimal(amount),
		currency=currency,
		description="Held for your bid on Booster box",
		payer_email="ann@example.com",
		reference=reference,
		return_url="https://shop.example/auctions/booster-box-abc123",
		hold_url="https://shop.example/auction-hold?bid=bid-1",
	)


class AmountsTest(unittest.TestCase):
	def test_minor_units_follow_the_currency(self) -> None:
		self.assertEqual(minor_units(Decimal("50.00"), "CAD"), 5000)
		self.assertEqual(minor_units(Decimal("12.345"), "usd"), 1235)
		self.assertEqual(minor_units(Decimal("5000"), "JPY"), 5000)

	def test_decimal_text_keeps_the_currency_places(self) -> None:
		self.assertEqual(decimal_text(Decimal("50"), "CAD"), "50.00")
		self.assertEqual(decimal_text(Decimal("5000"), "JPY"), "5000")


def intent(status: str, amount: int = 5000, bid: str = "bid-1", **extra: object) -> dict[str, object]:
	return {
		"id": "pi_3Auction",
		"object": "payment_intent",
		"amount": amount,
		"currency": "cad",
		"capture_method": "manual",
		"client_secret": "pi_3Auction_secret_x",
		"status": status,
		"metadata": {"auction_bid": bid},
		**extra,
	}


class StripeTest(unittest.TestCase):
	def adapter(self, *exchanges: dict[str, object]) -> tuple[StripeHolds, Replay]:
		replay = Replay(*exchanges)
		controller = Controller("Stripe Settings", {"secret_key": STRIPE_SECRET}, publishable_key="pk_test_auction")
		return StripeHolds(controller, transport=replay), replay

	def test_the_page_starts_a_manual_capture_intent_for_the_held_amount(self) -> None:
		adapter, replay = self.adapter(
			exchange(
				"POST",
				f"{STRIPE}/payment_intents",
				intent("requires_payment_method"),
				headers=STRIPE_AUTH,
				data={
					"amount": 5000,
					"currency": "cad",
					"capture_method": "manual",
					"payment_method_types[]": "card",
					"description": "Held for your bid on Booster box",
					"receipt_email": "ann@example.com",
					"metadata[auction_bid]": "bid-1",
				},
			)
		)
		page, reference = adapter.page_context(hold())
		self.assertEqual(page, {"publishable_key": "pk_test_auction", "client_secret": "pi_3Auction_secret_x"})
		self.assertEqual(reference, "pi_3Auction")
		replay.assert_done()

	def test_a_reloaded_page_reuses_an_unfinished_intent_and_replaces_a_dead_one(self) -> None:
		adapter, replay = self.adapter(
			exchange("GET", f"{STRIPE}/payment_intents/pi_3Auction", intent("requires_action"), headers=STRIPE_AUTH)
		)
		self.assertEqual(adapter.page_context(hold("pi_3Auction"))[1], "pi_3Auction")
		replay.assert_done()
		adapter, replay = self.adapter(
			exchange(
				"GET", f"{STRIPE}/payment_intents/pi_old", intent("canceled") | {"id": "pi_old"}, headers=STRIPE_AUTH
			),
			exchange("POST", f"{STRIPE}/payment_intents", intent("requires_payment_method"), headers=STRIPE_AUTH),
		)
		self.assertEqual(adapter.page_context(hold("pi_old"))[1], "pi_3Auction")
		replay.assert_done()

	def test_confirm_accepts_only_this_bids_authorized_amount(self) -> None:
		get = f"{STRIPE}/payment_intents/pi_3Auction"
		adapter, _ = self.adapter(exchange("GET", get, intent("requires_capture"), headers=STRIPE_AUTH))
		self.assertEqual(adapter.confirm(hold("pi_3Auction"), {}), "pi_3Auction")
		for wrong in (
			intent("requires_payment_method"),
			intent("requires_capture", amount=4000),
			intent("requires_capture", bid="bid-2"),
		):
			adapter, _ = self.adapter(exchange("GET", get, wrong, headers=STRIPE_AUTH))
			with self.assertRaises(HoldRefused):
				adapter.confirm(hold("pi_3Auction"), {})
		with self.assertRaises(HoldRefused):
			self.adapter()[0].confirm(hold(None), {})

	def test_capture_takes_the_price_and_release_cancels(self) -> None:
		adapter, replay = self.adapter(
			exchange(
				"POST",
				f"{STRIPE}/payment_intents/pi_3Auction/capture",
				intent("succeeded", amount_received=4100, latest_charge="ch_3Auction"),
				headers=STRIPE_AUTH,
				data={"amount_to_capture": 4100},
			),
			exchange("POST", f"{STRIPE}/payment_intents/pi_other/cancel", intent("canceled"), headers=STRIPE_AUTH),
		)
		self.assertEqual(adapter.capture_hold("pi_3Auction", 41.0, "CAD"), "ch_3Auction")
		adapter.release_hold("pi_other")
		replay.assert_done()

	def test_a_gateway_error_names_the_call_but_not_the_key(self) -> None:
		adapter, _ = self.adapter(
			exchange(
				"POST",
				f"{STRIPE}/payment_intents/pi_3Auction/cancel",
				{"error": {"type": "invalid_request_error"}},
				status=400,
				headers=STRIPE_AUTH,
			)
		)
		with self.assertRaises(GatewayError) as raised:
			adapter.release_hold("pi_3Auction")
		self.assertIn("400", str(raised.exception))
		self.assertNotIn(STRIPE_SECRET, str(raised.exception))


BRAINTREE_AUTH = {
	"Authorization": "Basic " + base64.b64encode(b"bt_public:bt_private").decode(),
	"Braintree-Version": braintree.API_VERSION,
}


def graphql(query: str, variables: dict[str, object], data: dict[str, object]) -> dict[str, object]:
	return exchange(
		"POST",
		braintree.SANDBOX,
		{"data": data, "extensions": {"requestId": "req-1"}},
		headers=BRAINTREE_AUTH,
		json={"query": query, "variables": variables},
	)


class BraintreeTest(unittest.TestCase):
	def adapter(self, *exchanges: dict[str, object]) -> tuple[braintree.BraintreeHolds, Replay]:
		replay = Replay(*exchanges)
		controller = Controller(
			"Braintree Settings", {"private_key": "bt_private"}, public_key="bt_public", use_sandbox=1
		)
		return braintree.BraintreeHolds(controller, transport=replay), replay

	def test_the_page_gets_a_client_token(self) -> None:
		adapter, replay = self.adapter(
			graphql(braintree.CLIENT_TOKEN, {}, {"createClientToken": {"clientToken": "ct-abc"}})
		)
		self.assertEqual(adapter.page_context(hold()), ({"client_token": "ct-abc"}, None))
		replay.assert_done()

	def test_confirm_authorizes_the_nonce_for_the_held_amount(self) -> None:
		variables = {"input": {"paymentMethodId": "nonce-1", "transaction": {"amount": "50.00", "orderId": "bid-1"}}}
		authorized = {
			"authorizePaymentMethod": {
				"transaction": {
					"id": "dHJhbnNhY3Rpb25fYWJj",
					"status": "AUTHORIZED",
					"orderId": "bid-1",
					"amount": {"value": "50.00", "currencyCode": "CAD"},
				}
			}
		}
		adapter, replay = self.adapter(graphql(braintree.AUTHORIZE, variables, authorized))
		self.assertEqual(adapter.confirm(hold(), {"nonce": "nonce-1"}), "dHJhbnNhY3Rpb25fYWJj")
		replay.assert_done()
		declined = {"data": None, "errors": [{"message": "Do Not Honor"}]}
		adapter, _ = self.adapter(
			exchange(
				"POST",
				braintree.SANDBOX,
				declined,
				headers=BRAINTREE_AUTH,
				json={"query": braintree.AUTHORIZE, "variables": variables},
			)
		)
		with self.assertRaises(HoldRefused):
			adapter.confirm(hold(), {"nonce": "nonce-1"})
		with self.assertRaises(HoldRefused):
			self.adapter()[0].confirm(hold(), {})

	def test_capture_and_release(self) -> None:
		adapter, replay = self.adapter(
			graphql(
				braintree.CAPTURE,
				{"input": {"transactionId": "txn-1", "transaction": {"amount": "41.00"}}},
				{"captureTransaction": {"transaction": {"id": "txn-1", "status": "SUBMITTED_FOR_SETTLEMENT"}}},
			),
			graphql(
				braintree.REVERSE,
				{"input": {"referenceId": "txn-2"}},
				{"reverseTransaction": {"reversal": {"id": "txn-2", "status": "VOIDED"}}},
			),
		)
		self.assertEqual(adapter.capture_hold("txn-1", 41.0, "CAD"), "txn-1")
		adapter.release_hold("txn-2")
		replay.assert_done()


PAYPAL_CREDENTIALS = {"VERSION": paypal.VERSION, "USER": "api_user", "PWD": "api_pass", "SIGNATURE": "sig"}


def nvp(method: str, reply: dict[str, str], **fields: str) -> dict[str, object]:
	return exchange(
		"POST",
		paypal.SANDBOX[0],
		urlencode({"ACK": "Success", **reply}),
		data={"METHOD": method, **PAYPAL_CREDENTIALS, **fields},
	)


class PayPalTest(unittest.TestCase):
	def adapter(self, *exchanges: dict[str, object]) -> tuple[paypal.PayPalHolds, Replay]:
		replay = Replay(*exchanges)
		controller = Controller(
			"PayPal Settings", {"api_password": "api_pass"}, api_username="api_user", signature="sig", paypal_sandbox=1
		)
		return paypal.PayPalHolds(controller, transport=replay), replay

	def test_the_page_sends_the_buyer_to_approve_an_authorization(self) -> None:
		adapter, replay = self.adapter(
			nvp(
				"SetExpressCheckout",
				{"TOKEN": "EC-1AB"},
				PAYMENTREQUEST_0_PAYMENTACTION="Authorization",
				PAYMENTREQUEST_0_AMT="50.00",
				PAYMENTREQUEST_0_CURRENCYCODE="CAD",
				PAYMENTREQUEST_0_DESC="Held for your bid on Booster box",
				PAYMENTREQUEST_0_INVNUM="bid-1",
				RETURNURL="https://shop.example/auction-hold?bid=bid-1&paypal=return",
				CANCELURL="https://shop.example/auctions/booster-box-abc123",
				NOSHIPPING="1",
			)
		)
		page, reference = adapter.page_context(hold())
		self.assertEqual(reference, "EC-1AB")
		self.assertEqual(page["approve_url"], f"{paypal.SANDBOX[1]}?cmd=_express-checkout&token=EC-1AB")
		replay.assert_done()

	def test_confirm_completes_the_authorization_for_the_approved_token(self) -> None:
		adapter, replay = self.adapter(
			nvp(
				"DoExpressCheckoutPayment",
				{"PAYMENTINFO_0_TRANSACTIONID": "AUTH-9", "PAYMENTINFO_0_PENDINGREASON": "authorization"},
				TOKEN="EC-1AB",
				PAYERID="PAYER1",
				PAYMENTREQUEST_0_PAYMENTACTION="Authorization",
				PAYMENTREQUEST_0_AMT="50.00",
				PAYMENTREQUEST_0_CURRENCYCODE="CAD",
				PAYMENTREQUEST_0_INVNUM="bid-1",
			)
		)
		self.assertEqual(adapter.confirm(hold("EC-1AB"), {"token": "EC-1AB", "PayerID": "PAYER1"}), "AUTH-9")
		replay.assert_done()
		with self.assertRaises(HoldRefused):
			self.adapter()[0].confirm(hold("EC-1AB"), {"token": "EC-OTHER", "PayerID": "PAYER1"})

	def test_capture_and_void(self) -> None:
		adapter, replay = self.adapter(
			nvp(
				"DoCapture",
				{"TRANSACTIONID": "CAP-7"},
				AUTHORIZATIONID="AUTH-9",
				AMT="41.00",
				CURRENCYCODE="CAD",
				COMPLETETYPE="Complete",
			),
			nvp("DoVoid", {"AUTHORIZATIONID": "AUTH-8"}, AUTHORIZATIONID="AUTH-8"),
		)
		self.assertEqual(adapter.capture_hold("AUTH-9", 41.0, "CAD"), "CAP-7")
		adapter.release_hold("AUTH-8")
		replay.assert_done()

	def test_a_failed_ack_is_an_error(self) -> None:
		adapter, _ = self.adapter(
			exchange(
				"POST",
				paypal.SANDBOX[0],
				urlencode({"ACK": "Failure", "L_ERRORCODE0": "10600"}),
				data={"METHOD": "DoVoid", **PAYPAL_CREDENTIALS, "AUTHORIZATIONID": "AUTH-8"},
			)
		)
		with self.assertRaisesRegex(GatewayError, "10600"):
			adapter.release_hold("AUTH-8")


RAZORPAY_AUTH = ("rzp_test_key", "rzp_secret")


class RazorpayTest(unittest.TestCase):
	def adapter(self, *exchanges: dict[str, object]) -> tuple[razorpay.RazorpayHolds, Replay]:
		replay = Replay(*exchanges)
		controller = Controller("Razorpay Settings", {"api_secret": "rzp_secret"}, api_key="rzp_test_key")
		return razorpay.RazorpayHolds(controller, transport=replay), replay

	def test_the_page_creates_an_uncaptured_order(self) -> None:
		order = {"id": "order_A1", "entity": "order", "amount": 5000, "currency": "INR", "status": "created"}
		adapter, replay = self.adapter(
			exchange(
				"POST",
				f"{razorpay.API}/orders",
				order,
				auth=RAZORPAY_AUTH,
				json={
					"amount": 5000,
					"currency": "INR",
					"receipt": "bid-1",
					"payment_capture": 0,
					"notes": {"auction_bid": "bid-1"},
				},
			)
		)
		page, reference = adapter.page_context(hold(currency="INR"))
		self.assertEqual((reference, page["order_id"], page["amount"]), ("order_A1", "order_A1", 5000))
		replay.assert_done()

	def test_confirm_checks_the_signature_and_the_authorized_payment(self) -> None:
		signature = hmac.new(b"rzp_secret", b"order_A1|pay_B2", hashlib.sha256).hexdigest()
		payment = {"id": "pay_B2", "entity": "payment", "status": "authorized", "order_id": "order_A1", "amount": 5000}
		adapter, replay = self.adapter(exchange("GET", f"{razorpay.API}/payments/pay_B2", payment, auth=RAZORPAY_AUTH))
		payload = {"razorpay_payment_id": "pay_B2", "razorpay_order_id": "order_A1", "razorpay_signature": signature}
		self.assertEqual(adapter.confirm(hold("order_A1", currency="INR"), payload), "pay_B2")
		replay.assert_done()
		with self.assertRaisesRegex(HoldRefused, "match"):
			self.adapter()[0].confirm(hold("order_A1", currency="INR"), payload | {"razorpay_signature": "0" * 64})
		adapter, _ = self.adapter(
			exchange("GET", f"{razorpay.API}/payments/pay_B2", payment | {"status": "failed"}, auth=RAZORPAY_AUTH)
		)
		with self.assertRaises(HoldRefused):
			adapter.confirm(hold("order_A1", currency="INR"), payload)

	def test_capture_and_a_release_that_lets_it_lapse(self) -> None:
		adapter, replay = self.adapter(
			exchange(
				"POST",
				f"{razorpay.API}/payments/pay_B2/capture",
				{"id": "pay_B2", "status": "captured", "amount": 4100},
				auth=RAZORPAY_AUTH,
				json={"amount": 4100, "currency": "INR"},
			)
		)
		self.assertEqual(adapter.capture_hold("pay_B2", 41.0, "INR"), "pay_B2")
		adapter.release_hold("pay_C3")
		replay.assert_done()


if __name__ == "__main__":
	unittest.main()
