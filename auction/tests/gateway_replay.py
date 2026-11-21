# SPDX-License-Identifier: AGPL-3.0-or-later
"""A fake gateway API for the adapter tests: it serves recorded exchanges (the request a call must make and the
response the gateway documents for it), each at most once, in any order, and fails on any call none expects.
Response bodies follow each gateway's published API reference; ids and amounts are made up."""

from __future__ import annotations

import json
from typing import Any


class Response:
	def __init__(self, status: int, body: Any) -> None:
		self.status_code = status
		self._body = body
		# NVP gateways answer with a form-encoded string, the rest with JSON.
		self.text = body if isinstance(body, str) else json.dumps(body)
		self.content = self.text.encode() if body is not None else b""

	def json(self) -> Any:
		return self._body


def exchange(method: str, url: str, response: Any, status: int = 200, **expected: Any) -> dict[str, Any]:
	"""`expected` holds the request parts the call must carry (data, json, params, headers, auth)."""
	return {"method": method, "url": url, "status": status, "response": response, "expected": expected}


class Replay:
	def __init__(self, *exchanges: dict[str, Any]) -> None:
		self.pending = list(exchanges)
		self.sent: list[dict[str, Any]] = []

	def __call__(self, method: str, url: str, timeout: float | None = None, **request: Any) -> Response:
		assert timeout, "every gateway call is bounded by a timeout"
		self.sent.append({"method": method, "url": url, **request})
		for index, candidate in enumerate(self.pending):
			if candidate["method"] != method or candidate["url"] != url:
				continue
			if all(request.get(key) == value for key, value in candidate["expected"].items()):
				del self.pending[index]
				return Response(candidate["status"], candidate["response"])
		raise AssertionError(f"Unexpected gateway call: {method} {url} {request}")

	def assert_done(self) -> None:
		assert not self.pending, f"Gateway calls expected but never made: {self.pending}"


class Controller:
	"""A gateway settings document as an adapter reads it: plain fields, and passwords through get_password."""

	def __init__(self, doctype: str, passwords: dict[str, str], **fields: Any) -> None:
		self.doctype = doctype
		self._passwords = passwords
		self.__dict__.update(fields)

	def get_password(self, fieldname: str, raise_exception: bool = True) -> str | None:
		return self._passwords.get(fieldname)
