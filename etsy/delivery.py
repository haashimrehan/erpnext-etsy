"""Decisions for turning a shipped Etsy receipt into a Delivery Note. Pure functions, no database access."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class ShipInfo:
	shipped_at: datetime | None
	carrier: str
	tracking: str


def ship_info(receipt) -> ShipInfo:
	"""When and how a receipt shipped: its latest shipment notice, else its latest transaction ship time."""
	shipments = sorted(
		(s for s in (receipt.shipments or []) if s.shipment_notification_timestamp),
		key=lambda s: s.shipment_notification_timestamp,
	)
	if shipments:
		last = shipments[-1]
		return ShipInfo(last.shipment_notification_timestamp, last.carrier_name or "", last.tracking_code or "")
	times = [t.shipped_timestamp for t in receipt.transactions if t.shipped_timestamp]
	return ShipInfo(max(times) if times else None, "", "")


def tracking_label(info: ShipInfo) -> str:
	"""Carrier and tracking code for the Delivery Note's transport reference, e.g. 'USPS 9400111...'."""
	return " ".join(p for p in (info.carrier, info.tracking) if p)[:140]


def stock_shortfall(needed: dict[tuple[str, str], float], on_hand: dict[tuple[str, str], float]) -> dict:
	"""Quantity per (item, warehouse) that has to be received before the delivery can go out."""
	return {key: qty - max(on_hand.get(key, 0.0), 0.0) for key, qty in needed.items() if qty > on_hand.get(key, 0.0)}
