# SPDX-License-Identifier: AGPL-3.0-or-later
"""The auction rules, as pure functions over bids: no database, no clock of their own.

Three formats:

- **English**: open, ascending. A bid is the most the bidder will pay (a proxy maximum); the price shown is
  the least that keeps the leader ahead: the runner-up's maximum plus one increment, never above the leader's
  maximum. Equal maximums go to the earlier bid. A bid near the close pushes the close back.
- **Dutch**: descending. The price starts high and drops one step per interval down to a floor; the first
  buyer to accept takes units at the price of that moment. Nothing is bid, so there is no bid book.
- **Sealed**: blind. Each bidder's highest bid counts and nobody sees another's until the close. The highest
  wins and pays their own bid (first price) or the runner-up's (second price, a Vickrey auction).

A reserve is the least the seller will sell for. It is never shown. When an English leader's maximum reaches
it, the price rises to it; otherwise the lot goes unsold.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

ENGLISH = "English"
DUTCH = "Dutch"
SEALED = "Sealed"
FORMATS = (ENGLISH, DUTCH, SEALED)

FIRST_PRICE = "First Price"
SECOND_PRICE = "Second Price"
SEALED_PRICING = (FIRST_PRICE, SECOND_PRICE)


class BidRejected(ValueError):
	"""A bid or acceptance that the rules refuse. The message is safe to show the bidder."""


@dataclass(frozen=True)
class Bid:
	bidder: str
	amount: Decimal
	placed: datetime


@dataclass(frozen=True)
class Standing:
	"""Who would win if the auction closed now, and at what price."""

	leader: str | None
	price: Decimal | None
	leader_amount: Decimal | None
	reserve_met: bool

	@property
	def sells(self) -> bool:
		return self.leader is not None and self.reserve_met


NO_BIDS = Standing(leader=None, price=None, leader_amount=None, reserve_met=False)


def best_per_bidder(bids: Iterable[Bid]) -> list[Bid]:
	"""Each bidder's highest bid (their earliest bid at that amount), best first: highest amount, then
	earliest."""
	best: dict[str, Bid] = {}
	for bid in sorted(bids, key=lambda b: b.placed):
		held = best.get(bid.bidder)
		if held is None or bid.amount > held.amount:
			best[bid.bidder] = bid
	return sorted(best.values(), key=lambda b: (-b.amount, b.placed))


def _reserve_met(price: Decimal, reserve: Decimal | None) -> bool:
	return not reserve or price >= reserve


def english_standing(bids: Iterable[Bid], start: Decimal, increment: Decimal, reserve: Decimal | None) -> Standing:
	ranked = best_per_bidder(bids)
	if not ranked:
		return NO_BIDS
	top = ranked[0]
	price = start if len(ranked) == 1 else min(top.amount, ranked[1].amount + increment)
	price = max(price, start)
	if reserve and top.amount >= reserve:
		price = max(price, reserve)
	return Standing(leader=top.bidder, price=price, leader_amount=top.amount, reserve_met=_reserve_met(price, reserve))


def english_minimum_bid(standing: Standing, start: Decimal, increment: Decimal) -> Decimal:
	"""The least a bidder other than the leader may bid now."""
	if standing.price is None:
		return start
	return standing.price + increment


def check_english_bid(
	bids: Iterable[Bid], bid: Bid, start: Decimal, increment: Decimal, reserve: Decimal | None
) -> None:
	bids = list(bids)
	standing = english_standing(bids, start, increment, reserve)
	if bid.bidder == standing.leader:
		if standing.leader_amount is not None and bid.amount <= standing.leader_amount:
			raise BidRejected(f"You are already the highest bidder, up to {standing.leader_amount}.")
		return
	minimum = english_minimum_bid(standing, start, increment)
	if bid.amount < minimum:
		raise BidRejected(f"The lowest bid now is {minimum}.")


def extended_end(ends: datetime, placed: datetime, window: timedelta) -> datetime:
	"""Anti-sniping: a bid placed less than `window` before the close moves the close to `window` after the
	bid."""
	if window and ends - placed < window:
		return placed + window
	return ends


def dutch_price_at(
	start: Decimal, floor: Decimal, step: Decimal, interval: timedelta, opened: datetime, at: datetime
) -> Decimal:
	if at <= opened:
		return start
	steps = (at - opened) // interval
	return max(floor, start - steps * step)


def units_text(qty: Decimal) -> str:
	"""A unit count as people write it: 1, 2.5, 10 (never 1.0 or 1E+1)."""
	return format(qty.normalize(), "f")


def check_dutch_accept(units_left: Decimal, qty: Decimal) -> None:
	if qty <= 0:
		raise BidRejected("Choose at least one unit.")
	if qty > units_left:
		raise BidRejected(f"Only {units_text(units_left)} left.")


def check_sealed_bid(bids: Iterable[Bid], bid: Bid, start: Decimal) -> None:
	if bid.amount < start:
		raise BidRejected(f"The lowest bid is {start}.")
	own = [b.amount for b in bids if b.bidder == bid.bidder]
	if own and bid.amount <= max(own):
		raise BidRejected(f"Your bid of {max(own)} stands; a new bid must be higher.")


def sealed_standing(bids: Iterable[Bid], start: Decimal, reserve: Decimal | None, pricing: str) -> Standing:
	ranked = best_per_bidder(bids)
	if not ranked:
		return NO_BIDS
	top = ranked[0]
	if pricing == FIRST_PRICE:
		price = top.amount
	elif pricing == SECOND_PRICE:
		runner_up = ranked[1].amount if len(ranked) > 1 else start
		price = min(top.amount, max(runner_up, start, reserve or Decimal(0)))
	else:
		raise ValueError(f"Unknown sealed pricing {pricing!r}")
	reserve_met = not reserve or top.amount >= reserve
	return Standing(leader=top.bidder, price=price, leader_amount=top.amount, reserve_met=reserve_met)
