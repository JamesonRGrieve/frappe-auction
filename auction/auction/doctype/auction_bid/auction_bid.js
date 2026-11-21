// SPDX-License-Identifier: AGPL-3.0-or-later
// A pending Best Offer offer gets Accept and Decline buttons for the seller.
frappe.ui.form.on("Auction Bid", {
	refresh(frm) {
		if (frm.doc.bid_type !== "Offer" || frm.doc.offer_status !== "Pending") return;
		const answer = (method, prompt) =>
			frappe.confirm(prompt, () =>
				frappe.call({ method, args: { bid: frm.doc.name } }).then(() => frm.reload_doc()),
			);
		frm.add_custom_button(__("Accept Offer"), () =>
			answer(
				"auction.api.accept_offer",
				__("Sell the lot to this buyer at {0}? Their Sales Order is created at once.", [
					format_currency(frm.doc.amount),
				]),
			),
		);
		frm.add_custom_button(__("Decline Offer"), () =>
			answer("auction.api.decline_offer", __("Decline this offer?")),
		);
	},
});
