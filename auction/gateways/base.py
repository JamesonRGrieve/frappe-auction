# SPDX-License-Identifier: AGPL-3.0-or-later
"""What every hold adapter shares: the gateway's settings document, an HTTP transport bounded by a timeout,
amounts in the currency's minor units, and errors that never carry a credential."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from urllib.parse import quote

import requests

TIMEOUT_SECONDS = 30
HOLD_PAGE = "/auction-hold"
# Currencies with no minor unit (ISO 4217 exponent 0) that card gateways price in whole units.
ZERO_DECIMAL_CURRENCIES = frozenset(
	{
		"BIF",
		"CLP",
		"DJF",
		"GNF",
		"ISK",
		"JPY",
		"KMF",
		"KRW",
		"MGA",
		"PYG",
		"RWF",
		"UGX",
		"VND",
		"VUV",
		"XAF",
		"XOF",
		"XPF",
	}
)

Transport = Callable[..., Any]


class HoldRefused(Exception):
	"""The gateway didn't hold the amount (declined, unfinished, or not what was asked). Safe to show the buyer."""


class GatewayError(Exception):
	"""The gateway answered with an error. The message names the call and status, never a credential."""


@dataclass(frozen=True)
class HoldRequest:
	"""What an adapter needs to know about the row whose amount it holds."""

	name: str
	amount: Decimal
	currency: str
	description: str
	payer_email: str
	reference: str | None
	return_url: str
	hold_url: str


def hold_page_url(bid_name: str) -> str:
	from frappe.utils import get_url

	return get_url(f"{HOLD_PAGE}?bid={quote(bid_name)}")


def minor_units(amount: Decimal | float, currency: str) -> int:
	exponent = Decimal(1) if currency.upper() in ZERO_DECIMAL_CURRENCIES else Decimal("0.01")
	return int((Decimal(str(amount)) / exponent).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def decimal_text(amount: Decimal | float, currency: str) -> str:
	"""An amount as a decimal string in major units, as NVP- and JSON-decimal APIs take it."""
	places = Decimal(1) if currency.upper() in ZERO_DECIMAL_CURRENCIES else Decimal("0.01")
	return str(Decimal(str(amount)).quantize(places, rounding=ROUND_HALF_UP))


class HoldAdapter:
	"""A gateway's hold support, built over its settings document (`controller`). Subclasses implement the
	gateway's calls; the auction's hold page (`/auction-hold`) renders `page_template` with `page_context`."""

	max_hold_days: int
	page_template: str

	def __init__(self, controller: Any, transport: Transport = requests.request) -> None:
		self.controller = controller
		self.transport = transport

	def get_hold_url(self, **kwargs: Any) -> str:
		return hold_page_url(kwargs["reference_docname"])

	def secret(self, fieldname: str) -> str:
		return self.controller.get_password(fieldname=fieldname, raise_exception=False) or ""

	def call(self, method: str, url: str, **kwargs: Any) -> Any:
		response = self.transport(method, url, timeout=TIMEOUT_SECONDS, **kwargs)
		if response.status_code >= 400:
			raise GatewayError(f"{type(self).__name__}: {method} {url.split('?')[0]} answered {response.status_code}")
		return response.json() if response.content else {}

	# The gateway's calls.

	def page_context(self, hold: HoldRequest) -> tuple[dict[str, Any], str | None]:
		"""What the hold page's template needs, and a reference to remember for `confirm` (or None)."""
		raise NotImplementedError

	def confirm(self, hold: HoldRequest, payload: dict[str, Any]) -> str:
		"""Checks with the gateway that the amount is held for this row; returns the hold's reference."""
		raise NotImplementedError

	def release_hold(self, hold_reference: str) -> None:
		raise NotImplementedError

	def capture_hold(self, hold_reference: str, amount: float, currency: str) -> str:
		"""Takes `amount` (at most what is held); returns the gateway's transaction id."""
		raise NotImplementedError
