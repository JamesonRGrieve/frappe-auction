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


if __name__ == "__main__":
	unittest.main()
