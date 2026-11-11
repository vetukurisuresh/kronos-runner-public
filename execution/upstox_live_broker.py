"""Guarded Upstox V2 live broker.

This module is intentionally small and explicit: every HTTP endpoint is visible,
the access token never appears in return payloads, and no order is sent unless
the caller has already passed the UI Submit Mode / confirmation guard in
:mod:`execution.order_router`.

Upstox order placement returns an order id, not an instant fill. ``wait_for_fill``
polls the order detail endpoint for a bounded period and exposes the exchange
average price when it becomes available.
"""

from __future__ import annotations

import time
from typing import Any

import requests

from market.upstox_health import load_access_token

API_BASE = "https://api.upstox.com"
FUNDS_URL = f"{API_BASE}/v2/user/get-funds-and-margin"
PLACE_ORDER_URL = f"{API_BASE}/v2/order/place"
MODIFY_ORDER_URL = f"{API_BASE}/v2/order/modify"
CANCEL_ORDER_URL = f"{API_BASE}/v2/order/cancel"
ORDER_DETAILS_URL = f"{API_BASE}/v2/order/details"


class UpstoxBrokerError(RuntimeError):
    """A rejected or malformed broker response."""


def _float(value: Any) -> float:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


class UpstoxLiveBroker:
    """Thin V2 REST adapter with no background threads or global state."""

    def __init__(
        self,
        access_token: str | None = None,
        *,
        timeout: float = 15.0,
        session: Any = None,
    ) -> None:
        self.access_token = (access_token or load_access_token()).strip()
        self.timeout = float(timeout)
        self.session = session or requests.Session()

    @property
    def token_present(self) -> bool:
        return bool(self.access_token)

    def snapshot(self) -> dict[str, Any]:
        """Safe state for the dashboard; never includes the token."""
        return {"token_present": self.token_present, "api": "UPSTOX V2", "timeout": self.timeout}

    def _headers(self) -> dict[str, str]:
        if not self.access_token:
            raise UpstoxBrokerError("UPSTOX_ACCESS_TOKEN is missing")
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self.session.request(
                method,
                url,
                headers=self._headers(),
                timeout=self.timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise UpstoxBrokerError(f"Upstox transport error: {type(exc).__name__}") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code >= 400:
            reason = (
                (payload.get("errors") or [{}])[0].get("message")
                if isinstance(payload.get("errors"), list)
                else payload.get("message")
            ) or f"HTTP {response.status_code}"
            raise UpstoxBrokerError(str(reason))
        if not isinstance(payload, dict):
            raise UpstoxBrokerError("Upstox returned a non-object response")
        return payload

    # -- account -----------------------------------------------------------
    def funds_and_margin(self, segment: str = "SEC") -> dict[str, Any]:
        """Normalize the available/used margin fields used by the Mega HUD."""
        payload = self._request(
            "GET",
            FUNDS_URL,
            params={"segment": segment},
        )
        data = payload.get("data") or {}
        equity = data.get("equity") or data
        available = (
            equity.get("available_margin")
            if equity.get("available_margin") is not None
            else equity.get("available_balance")
        )
        used = equity.get("used_margin") if equity.get("used_margin") is not None else equity.get("utilised_margin")
        return {
            "ok": True,
            "segment": segment,
            "available_margin": _float(available),
            "used_margin": _float(used),
            "status": payload.get("status") or "success",
        }

    # -- orders ------------------------------------------------------------
    def place_order(
        self,
        *,
        instrument_key: str,
        quantity: int,
        transaction_type: str,
        order_type: str = "LIMIT",
        price: float | None = None,
        trigger_price: float | None = None,
        product: str = "I",
        validity: str = "DAY",
        tag: str = "KRONOS",
    ) -> dict[str, Any]:
        if not instrument_key:
            raise UpstoxBrokerError("instrument_key is required")
        quantity = int(quantity)
        if quantity <= 0:
            raise UpstoxBrokerError("quantity must be positive")
        side = str(transaction_type).upper()
        if side not in {"BUY", "SELL"}:
            raise UpstoxBrokerError("transaction_type must be BUY or SELL")
        order_type = str(order_type).upper()
        if order_type not in {"MARKET", "LIMIT", "SL", "SL-M"}:
            raise UpstoxBrokerError("unsupported order type")
        body: dict[str, Any] = {
            "quantity": quantity,
            "product": product,
            "validity": validity,
            "instrument_token": instrument_key,
            "order_type": order_type,
            "transaction_type": side,
            "disclosed_quantity": 0,
            "trigger_price": round(float(trigger_price), 2) if trigger_price else 0,
            "is_amo": False,
            "slice": False,
            "tag": str(tag)[:20],
        }
        if order_type in {"LIMIT", "SL"}:
            if price is None or float(price) <= 0:
                raise UpstoxBrokerError("price is required for LIMIT/SL orders")
            body["price"] = round(float(price), 2)
        payload = self._request("POST", PLACE_ORDER_URL, json=body)
        data = payload.get("data") or {}
        order_id = data.get("order_id") or data.get("orderId")
        if not order_id:
            raise UpstoxBrokerError("Upstox accepted the request without an order id")
        return {
            "ok": True,
            "order_id": str(order_id),
            "status": payload.get("status") or "success",
            "request": {key: body[key] for key in ("instrument_token", "quantity", "transaction_type", "order_type", "price") if key in body},
        }

    def modify_order(
        self,
        order_id: str,
        *,
        quantity: int | None = None,
        order_type: str | None = None,
        price: float | None = None,
        trigger_price: float | None = None,
        validity: str | None = None,
    ) -> dict[str, Any]:
        if not order_id:
            raise UpstoxBrokerError("order_id is required")
        body: dict[str, Any] = {"order_id": str(order_id)}
        if quantity is not None:
            body["quantity"] = int(quantity)
        if order_type:
            body["order_type"] = str(order_type).upper()
        if price is not None:
            body["price"] = round(float(price), 2)
        if trigger_price is not None:
            body["trigger_price"] = round(float(trigger_price), 2)
        if validity:
            body["validity"] = validity
        payload = self._request("PUT", MODIFY_ORDER_URL, json=body)
        return {"ok": True, "order_id": str(order_id), "status": payload.get("status") or "success"}

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        if not order_id:
            raise UpstoxBrokerError("order_id is required")
        payload = self._request("DELETE", CANCEL_ORDER_URL, params={"order_id": str(order_id)})
        return {"ok": True, "order_id": str(order_id), "status": payload.get("status") or "success"}

    def order_details(self, order_id: str) -> dict[str, Any]:
        payload = self._request("GET", ORDER_DETAILS_URL, params={"order_id": str(order_id)})
        data = payload.get("data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        return {
            "ok": True,
            "order_id": str(order_id),
            "status": str(data.get("status") or "").upper(),
            "average_price": _float(data.get("average_price") or data.get("averagePrice")),
            "filled_quantity": int(_float(data.get("filled_quantity") or data.get("filledQuantity"))),
            "raw_status": payload.get("status"),
        }

    def wait_for_fill(
        self,
        order_id: str,
        *,
        timeout: float = 2.5,
        interval: float = 0.25,
    ) -> dict[str, Any]:
        """Bounded poll for an exchange fill; never sleeps forever."""
        deadline = time.time() + max(float(timeout), 0.0)
        last: dict[str, Any] = {"ok": False, "order_id": order_id, "status": "PENDING", "average_price": 0.0}
        while time.time() <= deadline:
            try:
                last = self.order_details(order_id)
            except UpstoxBrokerError as exc:
                last = {"ok": False, "order_id": order_id, "status": "UNKNOWN", "reason": str(exc)}
            if last.get("average_price", 0) > 0 or last.get("status") in {
                "COMPLETE", "COMPLETED", "FILLED", "REJECTED", "CANCELLED",
            }:
                return last
            if timeout <= 0:
                break
            time.sleep(interval)
        return last

    def market_exit(self, *, instrument_key: str, quantity: int, tag: str = "KRONOS-CLOSE") -> dict[str, Any]:
        """Sell the remaining open quantity at market and report the order id."""
        return self.place_order(
            instrument_key=instrument_key,
            quantity=quantity,
            transaction_type="SELL",
            order_type="MARKET",
            product="I",
            tag=tag,
        )
