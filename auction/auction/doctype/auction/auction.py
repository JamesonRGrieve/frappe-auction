# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import get_datetime
from frappe.website.website_generator import WebsiteGenerator

from auction import engine, holds, rules, settlement

# Once anyone has bid, these are part of the deal the bidders took and can't change.
TERMS_FIELDS = (
	"item",
	"company",
	"quantity",
	"auction_type",
	"sealed_pricing",
	"start_price",
	"reserve_price",
	"buy_now_price",
	"bid_increment",
	"floor_price",
	"price_step",
	"step_interval_seconds",
	"starts_at",
	"settle_as",
	"payment_timing",
	"payment_gateway_account",
)
SECONDS_PER_DAY = 86400
ROUTE_SUFFIX_LENGTH = 6


class Auction(WebsiteGenerator):
	website = frappe._dict(condition_field="published", page_title_field="title")

	def validate(self) -> None:
		self.validate_terms()
		self.validate_locked_terms()
		super().validate()

	def validate_terms(self) -> None:
		if self.auction_type not in rules.FORMATS:
			frappe.throw(_("Unknown auction type {0}.").format(self.auction_type))
		if get_datetime(self.ends_at) <= get_datetime(self.starts_at):
			frappe.throw(_("Ends At must be after Starts At."))
		if self.quantity <= 0:
			frappe.throw(_("Quantity must be more than 0."))
		if self.settle_as not in settlement.SETTLE_AS:
			frappe.throw(_("Settle As must be Sales Order or Sales Invoice."))
		if self.payment_timing not in engine.PAYMENT_TIMINGS:
			frappe.throw(_("Unknown payment timing {0}.").format(self.payment_timing))
		if self.payment_timing == engine.AUTHORIZE_AT_BID:
			self.validate_holds()
		if self.auction_type == rules.BEST_OFFER:
			self.validate_best_offer()
		elif (self.start_price or 0) <= 0:
			frappe.throw(_("Start Price must be more than 0."))
		if self.buy_now_price and self.auction_type not in (rules.ENGLISH, rules.SEALED, rules.BEST_OFFER):
			self.buy_now_price = 0
		if self.buy_now_price and self.auction_type != rules.BEST_OFFER:
			if self.buy_now_price <= self.start_price or self.buy_now_price < (self.reserve_price or 0):
				frappe.throw(_("Buy It Now Price must be above the Start Price and at least the Reserve Price."))
		if self.auction_type == rules.UNIFORM and self.quantity != int(self.quantity):
			frappe.throw(_("A Uniform Price auction offers a whole number of units."))
		if self.auction_type == rules.ENGLISH and (self.bid_increment or 0) <= 0:
			frappe.throw(_("Bid Increment must be more than 0."))
		if self.auction_type == rules.SEALED and self.sealed_pricing not in rules.SEALED_PRICING:
			frappe.throw(_("Choose the sealed pricing."))
		if self.auction_type == rules.DUTCH:
			if self.floor_price is None or self.floor_price < 0 or self.floor_price >= self.start_price:
				frappe.throw(_("Floor Price must be at least 0 and below the Start Price."))
			if (self.price_step or 0) <= 0:
				frappe.throw(_("Price Drop per Step must be more than 0."))
			if (self.step_interval_seconds or 0) <= 0:
				frappe.throw(_("Step Interval must be more than 0 seconds."))
			self.reserve_price = 0

	def validate_holds(self) -> None:
		provider = holds.require_provider(self.payment_gateway_account)
		days = (get_datetime(self.ends_at) - get_datetime(self.starts_at)).total_seconds() / SECONDS_PER_DAY
		if days > provider.max_hold_days:
			frappe.throw(
				_("This gateway keeps a hold for {0} days; an Authorize at Bid auction can't run longer.").format(
					provider.max_hold_days
				)
			)

	def validate_best_offer(self) -> None:
		if (self.buy_now_price or 0) <= 0:
			frappe.throw(_("A Best Offer listing needs its asking price in Buy It Now Price."))
		accept, decline = self.auto_accept_offer or 0, self.auto_decline_offer or 0
		if accept and accept >= self.buy_now_price:
			frappe.throw(_("Auto-Accept Offers From must be below the asking price."))
		if accept and decline and decline > accept:
			frappe.throw(_("Auto-Decline Offers Below can't be above Auto-Accept Offers From."))
		self.start_price = 0
		self.reserve_price = 0

	def validate_locked_terms(self) -> None:
		if self.is_new() or not self.bid_count:
			return
		changed = [field for field in TERMS_FIELDS if self.has_value_changed(field)]
		if changed:
			labels = ", ".join(self.meta.get_label(field) for field in changed)
			frappe.throw(_("{0} can't change once bidding has started.").format(labels))

	def make_route(self) -> str:
		# Titles repeat ("Pokemon booster box"), so the route carries part of the random name.
		return f"{super().make_route()}-{self.name[:ROUTE_SUFFIX_LENGTH]}"

	def get_context(self, context: frappe._dict) -> None:
		context.no_cache = 1
		context.state = engine.public_state(self, frappe.session.user)
		context.parents = [{"route": "auctions", "title": _("Auctions")}]
