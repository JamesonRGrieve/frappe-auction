# SPDX-License-Identifier: AGPL-3.0-or-later
from . import __version__ as _version

app_name = "auction"
app_title = "Auction"
app_publisher = "Zephyrex Technologies Limited"
app_description = "English, Dutch and sealed-bid auctions for ERPNext items, sold through the website"
app_email = "jameson@zephyrex.ca"
app_license = "AGPL-3.0-or-later"
app_version = _version

required_apps = ["frappe", "erpnext"]

# Opens auctions whose start has come and settles those whose end has passed.
scheduler_events = {"cron": {"* * * * *": ["auction.engine.open_and_close_due"]}}
