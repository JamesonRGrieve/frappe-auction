# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which webshop store's auctions a request may see. With the frappe-webshop fork (several stores per site) and a
``webshop_store_resolver`` (frappe-public-site-router names each host's store), a store's host lists and serves only
that store's auctions; a request no store claims (the ERP's own host) sees every auction.

``Auction.webshop_store`` is a Custom Field, made at install and migrate once a Webshop Store DocType exists, so the
app installs on a site without the webshop and gains the field when the webshop arrives."""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

FIELD = "webshop_store"
STORE_DOCTYPE = "Webshop Store"


def available() -> bool:
	return "webshop" in frappe.get_installed_apps() and bool(frappe.db.exists("DocType", STORE_DOCTYPE))


def current_store() -> str | None:
	"""The enabled store serving this request, or None."""
	if not available():
		return None
	from webshop.webshop.store import get_current_store

	return get_current_store()


def filters() -> dict[str, Any]:
	"""Auction list filters for this request: its store's auctions only, when a store serves it."""
	store = current_store()
	return {FIELD: store} if store else {}


def visible(auction: Any) -> bool:
	store = current_store()
	return not store or auction.get(FIELD) == store


def require_visible(auction: Any) -> None:
	"""A page or call for another store's auction is a 404 here, as if it didn't exist."""
	if not visible(auction):
		raise frappe.DoesNotExistError(_("Auction {0} not found").format(auction.name))


def validate(auction: Any) -> None:
	"""An auction sells for its store's own company."""
	store = auction.get(FIELD)
	if not store:
		return
	company = frappe.db.get_value(STORE_DOCTYPE, store, "company")
	if company != auction.company:
		frappe.throw(
			_("Webshop Store {0} sells for {1}, not {2}.").format(store, company or _("no company"), auction.company)
		)


def ensure_field() -> None:
	"""after_install / after_migrate: add ``Auction.webshop_store`` once the webshop's stores exist."""
	if not available() or frappe.db.exists("Custom Field", {"dt": "Auction", "fieldname": FIELD}):
		return
	from frappe.custom.doctype.custom_field.custom_field import create_custom_field

	create_custom_field(
		"Auction",
		{
			"fieldname": FIELD,
			"label": "Webshop Store",
			"fieldtype": "Link",
			"options": STORE_DOCTYPE,
			"insert_after": "company",
			"in_standard_filter": 1,
			"description": "The store whose website lists and serves this auction. Blank: only the ERP's own host shows it.",
		},
	)
