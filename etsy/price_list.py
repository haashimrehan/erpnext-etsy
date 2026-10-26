"""Choose a selling Price List that ERPNext accepts for the buyer's country. Pure function, no database access."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PriceListOption:
	name: str
	currency: str
	countries: list[str] = field(default_factory=list)


def applies_to(option: PriceListOption, country: str | None) -> bool:
	"""A Price List without countries applies everywhere; otherwise only to the countries it lists."""
	return not option.countries or not country or country in option.countries


def pick_price_list(
	options: list[PriceListOption], country: str | None, currency: str, current: str | None
) -> str | None:
	"""
	Keep the current Price List while it applies to the country. Otherwise prefer one made for the
	country, then one without a country restriction; in each group a list in the order currency wins.
	"""
	by_name = {o.name: o for o in options}
	if current and current in by_name and applies_to(by_name[current], country):
		return current

	def best(group: list[PriceListOption]) -> str | None:
		group = sorted(group, key=lambda o: (o.currency != currency, o.name))
		return group[0].name if group else None

	return (
		best([o for o in options if country and country in o.countries])
		or best([o for o in options if not o.countries])
		or current
	)
