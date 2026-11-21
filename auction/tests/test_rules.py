# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pure auction rules. Runs without a bench: ``python3 -m unittest auction.tests.test_rules``."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from auction import rules
from auction.rules import Bid, BidRejected

T0 = datetime(2026, 11, 21, 12, 0, 0)
START = Decimal("10.00")
INCREMENT = Decimal("1.00")


def at(seconds: int) -> datetime:
	return T0 + timedelta(seconds=seconds)


def bid(bidder: str, amount: str, seconds: int) -> Bid:
	return Bid(bidder=bidder, amount=Decimal(amount), placed=at(seconds))


class EnglishTest(unittest.TestCase):
	def standing(self, bids: list[Bid], reserve: str | None = None) -> rules.Standing:
		return rules.english_standing(bids, START, INCREMENT, Decimal(reserve) if reserve else None)

	def test_no_bids_has_no_leader_and_asks_the_start_price(self) -> None:
		standing = self.standing([])
		self.assertIsNone(standing.leader)
		self.assertFalse(standing.sells)
		self.assertEqual(rules.english_minimum_bid(standing, START, INCREMENT), START)

	def test_a_lone_bidder_pays_the_start_price_whatever_their_maximum(self) -> None:
		standing = self.standing([bid("ann", "50", 1)])
		self.assertEqual((standing.leader, standing.price), ("ann", START))
		self.assertTrue(standing.sells)

	def test_the_leader_pays_one_increment_over_the_runner_up(self) -> None:
		standing = self.standing([bid("ann", "50", 1), bid("bob", "20", 2)])
		self.assertEqual((standing.leader, standing.price), ("ann", Decimal("21.00")))

	def test_the_price_never_exceeds_the_leaders_maximum(self) -> None:
		standing = self.standing([bid("ann", "20.50", 1), bid("bob", "20", 2)])
		self.assertEqual((standing.leader, standing.price), ("ann", Decimal("20.50")))

	def test_equal_maximums_go_to_the_earlier_bid(self) -> None:
		standing = self.standing([bid("bob", "30", 5), bid("ann", "30", 1)])
		self.assertEqual((standing.leader, standing.price), ("ann", Decimal("30")))

	def test_a_bidders_lower_later_bid_does_not_lower_their_maximum(self) -> None:
		standing = self.standing([bid("ann", "50", 1), bid("ann", "15", 3), bid("bob", "40", 2)])
		self.assertEqual((standing.leader, standing.price), ("ann", Decimal("41.00")))

	def test_a_reserve_below_the_leaders_maximum_lifts_the_price_to_it(self) -> None:
		standing = self.standing([bid("ann", "50", 1), bid("bob", "20", 2)], reserve="35")
		self.assertEqual(standing.price, Decimal("35"))
		self.assertTrue(standing.sells)

	def test_a_reserve_above_every_maximum_is_not_met(self) -> None:
		standing = self.standing([bid("ann", "30", 1)], reserve="35")
		self.assertEqual(standing.leader, "ann")
		self.assertFalse(standing.sells)

	def test_a_bid_below_the_minimum_is_refused(self) -> None:
		bids = [bid("ann", "50", 1), bid("bob", "20", 2)]
		with self.assertRaisesRegex(BidRejected, "22.00"):
			rules.check_english_bid(bids, bid("cat", "21.99", 3), START, INCREMENT, None)
		rules.check_english_bid(bids, bid("cat", "22.00", 3), START, INCREMENT, None)

	def test_the_first_bid_must_reach_the_start_price(self) -> None:
		with self.assertRaises(BidRejected):
			rules.check_english_bid([], bid("ann", "9.99", 1), START, INCREMENT, None)
		rules.check_english_bid([], bid("ann", "10", 1), START, INCREMENT, None)

	def test_the_leader_may_only_raise_their_own_maximum(self) -> None:
		bids = [bid("ann", "50", 1)]
		with self.assertRaisesRegex(BidRejected, "already the highest"):
			rules.check_english_bid(bids, bid("ann", "50", 2), START, INCREMENT, None)
		rules.check_english_bid(bids, bid("ann", "60", 2), START, INCREMENT, None)

	def test_a_late_bid_pushes_the_close_back(self) -> None:
		ends = at(600)
		window = timedelta(minutes=2)
		self.assertEqual(rules.extended_end(ends, at(500), window), at(620))
		self.assertEqual(rules.extended_end(ends, at(400), window), ends)
		self.assertEqual(rules.extended_end(ends, at(500), timedelta(0)), ends)


class DutchTest(unittest.TestCase):
	START = Decimal("100")
	FLOOR = Decimal("40")
	STEP = Decimal("5")
	INTERVAL = timedelta(seconds=60)

	def price(self, seconds: int) -> Decimal:
		return rules.dutch_price_at(self.START, self.FLOOR, self.STEP, self.INTERVAL, T0, at(seconds))

	def test_the_price_starts_at_the_start_price(self) -> None:
		self.assertEqual(self.price(-30), self.START)
		self.assertEqual(self.price(0), self.START)
		self.assertEqual(self.price(59), self.START)

	def test_the_price_drops_one_step_per_whole_interval(self) -> None:
		self.assertEqual(self.price(60), Decimal("95"))
		self.assertEqual(self.price(299), Decimal("80"))

	def test_the_price_never_drops_below_the_floor(self) -> None:
		self.assertEqual(self.price(60 * 12), self.FLOOR)
		self.assertEqual(self.price(60 * 1000), self.FLOOR)

	def test_an_acceptance_takes_at_most_the_units_left(self) -> None:
		rules.check_dutch_accept(Decimal(3), Decimal(3))
		with self.assertRaisesRegex(BidRejected, "Only 3 left"):
			rules.check_dutch_accept(Decimal(3), Decimal(4))
		with self.assertRaises(BidRejected):
			rules.check_dutch_accept(Decimal(3), Decimal(0))

	def test_units_left_read_as_people_write_them(self) -> None:
		# Quantities come from Float fields as 1.0; the message must not say "Only 1.0 left".
		with self.assertRaisesRegex(BidRejected, r"^Only 1 left\.$"):
			rules.check_dutch_accept(Decimal("1.0"), Decimal(2))
		self.assertEqual(rules.units_text(Decimal("10.000")), "10")
		self.assertEqual(rules.units_text(Decimal("2.50")), "2.5")


class SealedTest(unittest.TestCase):
	def standing(self, bids: list[Bid], pricing: str, reserve: str | None = None) -> rules.Standing:
		return rules.sealed_standing(bids, START, Decimal(reserve) if reserve else None, pricing)

	def test_first_price_pays_the_winning_bid(self) -> None:
		standing = self.standing([bid("ann", "50", 1), bid("bob", "40", 2)], rules.FIRST_PRICE)
		self.assertEqual((standing.leader, standing.price), ("ann", Decimal("50")))

	def test_second_price_pays_the_runner_up_bid(self) -> None:
		standing = self.standing([bid("ann", "50", 1), bid("bob", "40", 2)], rules.SECOND_PRICE)
		self.assertEqual((standing.leader, standing.price), ("ann", Decimal("40")))

	def test_second_price_with_one_bidder_pays_the_start_price(self) -> None:
		standing = self.standing([bid("ann", "50", 1)], rules.SECOND_PRICE)
		self.assertEqual(standing.price, START)

	def test_second_price_never_falls_below_a_met_reserve(self) -> None:
		standing = self.standing([bid("ann", "50", 1), bid("bob", "20", 2)], rules.SECOND_PRICE, reserve="30")
		self.assertEqual(standing.price, Decimal("30"))
		self.assertTrue(standing.sells)

	def test_a_winning_bid_under_the_reserve_does_not_sell(self) -> None:
		standing = self.standing([bid("ann", "25", 1)], rules.FIRST_PRICE, reserve="30")
		self.assertFalse(standing.sells)

	def test_ties_go_to_the_earlier_bid(self) -> None:
		standing = self.standing([bid("bob", "50", 9), bid("ann", "50", 1)], rules.SECOND_PRICE)
		self.assertEqual((standing.leader, standing.price), ("ann", Decimal("50")))

	def test_a_bid_must_reach_the_start_price_and_beat_the_bidders_own(self) -> None:
		with self.assertRaises(BidRejected):
			rules.check_sealed_bid([], bid("ann", "9", 1), START)
		bids = [bid("ann", "30", 1)]
		with self.assertRaisesRegex(BidRejected, "must be higher"):
			rules.check_sealed_bid(bids, bid("ann", "30", 2), START)
		rules.check_sealed_bid(bids, bid("ann", "31", 2), START)
		rules.check_sealed_bid(bids, bid("bob", "11", 2), START)

	def test_unknown_pricing_fails_loudly(self) -> None:
		with self.assertRaises(ValueError):
			self.standing([bid("ann", "50", 1)], "Third Price")


def unit_bid(bidder: str, amount: str, qty: int, seconds: int) -> Bid:
	return Bid(bidder=bidder, amount=Decimal(amount), placed=at(seconds), qty=Decimal(qty))


class UniformTest(unittest.TestCase):
	UNITS = Decimal(5)

	def result(self, bids: list[Bid], reserve: str | None = None) -> tuple[Decimal | None, list[tuple[str, int]]]:
		price, won = rules.uniform_result(bids, self.UNITS, START, Decimal(reserve) if reserve else None)
		return price, [(a.bid.bidder, int(a.units)) for a in won]

	def test_highest_bids_fill_first_and_all_pay_the_lowest_winning_bid(self) -> None:
		bids = [unit_bid("ann", "30", 2, 1), unit_bid("bob", "25", 2, 2), unit_bid("cat", "20", 3, 3)]
		self.assertEqual(self.result(bids), (Decimal("20"), [("ann", 2), ("bob", 2), ("cat", 1)]))

	def test_bids_below_the_cut_win_nothing(self) -> None:
		bids = [unit_bid("ann", "30", 5, 1), unit_bid("bob", "25", 2, 2)]
		self.assertEqual(self.result(bids), (Decimal("30"), [("ann", 5)]))

	def test_equal_bids_fill_in_the_order_placed(self) -> None:
		bids = [unit_bid("bob", "20", 4, 5), unit_bid("ann", "20", 4, 1)]
		self.assertEqual(self.result(bids), (Decimal("20"), [("ann", 4), ("bob", 1)]))

	def test_bids_under_the_reserve_are_left_out(self) -> None:
		bids = [unit_bid("ann", "30", 2, 1), unit_bid("bob", "15", 2, 2)]
		self.assertEqual(self.result(bids, reserve="20"), (Decimal("30"), [("ann", 2)]))
		self.assertEqual(self.result([unit_bid("bob", "15", 2, 2)], reserve="20"), (None, []))

	def test_a_bidders_highest_bid_counts_with_its_units(self) -> None:
		bids = [unit_bid("ann", "12", 5, 1), unit_bid("ann", "18", 1, 2), unit_bid("bob", "15", 5, 3)]
		self.assertEqual(self.result(bids), (Decimal("15"), [("ann", 1), ("bob", 4)]))

	def test_a_bid_must_be_whole_units_on_offer_and_beat_the_bidders_own(self) -> None:
		with self.assertRaisesRegex(BidRejected, "whole number"):
			rules.check_uniform_bid([], Bid("ann", Decimal(20), at(1), Decimal("1.5")), START, self.UNITS)
		with self.assertRaisesRegex(BidRejected, "Only 5"):
			rules.check_uniform_bid([], unit_bid("ann", "20", 6, 1), START, self.UNITS)
		with self.assertRaises(BidRejected):
			rules.check_uniform_bid([], unit_bid("ann", "9", 1, 1), START, self.UNITS)
		with self.assertRaisesRegex(BidRejected, "must be higher"):
			rules.check_uniform_bid([unit_bid("ann", "20", 1, 1)], unit_bid("ann", "20", 3, 2), START, self.UNITS)
		rules.check_uniform_bid([unit_bid("ann", "20", 1, 1)], unit_bid("ann", "21", 3, 2), START, self.UNITS)


class BuyNowTest(unittest.TestCase):
	PRICE = Decimal("80")

	def available(self, auction_type: str, bids: list[Bid], reserve: str | None = None) -> bool:
		reserve_amount = Decimal(reserve) if reserve else None
		standing = rules.english_standing(bids, START, INCREMENT, reserve_amount)
		return rules.buy_now_available(auction_type, self.PRICE, standing, reserve_amount)

	def test_english_buy_now_lasts_until_the_first_bid(self) -> None:
		self.assertTrue(self.available(rules.ENGLISH, []))
		self.assertFalse(self.available(rules.ENGLISH, [bid("ann", "15", 1)]))

	def test_with_a_reserve_english_buy_now_lasts_until_the_reserve_is_met(self) -> None:
		self.assertTrue(self.available(rules.ENGLISH, [bid("ann", "15", 1)], reserve="50"))
		self.assertFalse(self.available(rules.ENGLISH, [bid("ann", "60", 1)], reserve="50"))

	def test_sealed_and_best_offer_keep_buy_now_and_others_never_have_it(self) -> None:
		self.assertTrue(self.available(rules.SEALED, [bid("ann", "15", 1)]))
		self.assertTrue(self.available(rules.BEST_OFFER, []))
		self.assertFalse(self.available(rules.DUTCH, []))
		self.assertFalse(self.available(rules.UNIFORM, []))

	def test_no_price_means_no_buy_now(self) -> None:
		self.assertFalse(rules.buy_now_available(rules.ENGLISH, None, rules.NO_BIDS, None))


class OfferTest(unittest.TestCase):
	ASKING = Decimal("100")

	def decide(self, amount: str, accept: str | None = None, decline: str | None = None) -> str:
		return rules.offer_decision(
			Decimal(amount), self.ASKING, Decimal(accept) if accept else None, Decimal(decline) if decline else None
		)

	def test_an_offer_waits_for_the_seller_by_default(self) -> None:
		self.assertEqual(self.decide("70"), rules.OFFER_PENDING)

	def test_thresholds_accept_or_decline_at_once(self) -> None:
		self.assertEqual(self.decide("90", accept="90", decline="60"), rules.OFFER_ACCEPTED)
		self.assertEqual(self.decide("59.99", accept="90", decline="60"), rules.OFFER_DECLINED)
		self.assertEqual(self.decide("60", accept="90", decline="60"), rules.OFFER_PENDING)

	def test_an_offer_at_the_asking_price_is_sent_to_buy_now(self) -> None:
		with self.assertRaisesRegex(BidRejected, "buy it now"):
			self.decide("100")
		with self.assertRaises(BidRejected):
			self.decide("0")


if __name__ == "__main__":
	unittest.main()
