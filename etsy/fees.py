"""
Classification and aggregation of Etsy payment account ledger entries into expense categories.

This module is intentionally free of Frappe imports so it can be unit tested without a site.
The Frappe-specific part (building the Journal Entry) lives in ``etsy_shop.py``.

Etsy's ledger (``getShopPaymentAccountLedgerEntries``) contains every movement on the
seller's payment account: sales, refunds, deposits (payouts) and all the fees Etsy charges.
Only the fee-like entries are relevant for the monthly expense Journal Entry.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
	from .datastruct import LedgerEntry


class LedgerLike(Protocol):
	"""The subset of ``LedgerEntry`` this module needs - handy for tests."""

	entry_id: int
	amount: int
	currency: str
	description: str
	ledger_type: str | None
	reference_type: str | None


### Categories

CATEGORY_FEES = "Fees"
CATEGORY_MARKETING = "Marketing"
CATEGORY_SHIPPING = "Shipping"
CATEGORY_OTHER = "Other"

CATEGORIES = (CATEGORY_FEES, CATEGORY_MARKETING, CATEGORY_SHIPPING, CATEGORY_OTHER)

# Sentinel for ledger entries that only move money and are never an expense.
TRANSFER = "__transfer__"

# Etsy reports ledger amounts as integers in the smallest currency unit (cents)
LEDGER_DIVISOR = 100


### Ledger type rules

# Etsy returns machine tokens rather than prose. In a real shop ledger ``description`` is identical
# to ``ledger_type`` for every entry ("prolist", "PAYMENT_GROSS", "vat_seller_services", ...), so
# ``ledger_type`` is the primary signal and the description keywords further below are only a
# fallback for shops or locales that do return prose.
#
# Matching is by substring, first match wins, so the more specific patterns come first.
LEDGER_TYPE_RULES: tuple[tuple[str, str], ...] = (
	### specific fees - these contain substrings used by the broader rules below, so they come first
	("payment_processing_fee", CATEGORY_FEES),
	("processing_fee", CATEGORY_FEES),
	("shipping_transaction", CATEGORY_FEES),  # Etsy's fee on the shipping part of an order
	("shipping_label", CATEGORY_SHIPPING),
	("shipping_purchase", CATEGORY_SHIPPING),
	("postage", CATEGORY_SHIPPING),
	### money movement - never an expense of the seller
	("payment_gross", TRANSFER),  # a buyer payment landing on the Etsy balance
	("payment_net", TRANSFER),
	("billing_payment", TRANSFER),  # the seller settling an Etsy bill by card
	("bill_payment", TRANSFER),
	("sales_tax", TRANSFER),  # marketplace facilitator tax Etsy collects from buyers and remits
	("disburse", TRANSFER),  # DISBURSE2 / disbursement - booked separately as payouts
	("payout", TRANSFER),
	("deposit", TRANSFER),
	("recoupment", TRANSFER),  # Etsy charging the card on file for a negative balance
	("reversal", TRANSFER),
	("payment", TRANSFER),
	("sale", TRANSFER),
	### marketing
	("prolist", CATEGORY_MARKETING),  # Etsy Ads
	("offsite_ad", CATEGORY_MARKETING),
	("etsy_ad", CATEGORY_MARKETING),
	("advertis", CATEGORY_MARKETING),
	("marketing", CATEGORY_MARKETING),
	("promoted", CATEGORY_MARKETING),
	### fees
	("regulatory", CATEGORY_FEES),
	("transaction", CATEGORY_FEES),
	("listing", CATEGORY_FEES),
	("renew", CATEGORY_FEES),
	("seller_services", CATEGORY_FEES),
	("subscription", CATEGORY_FEES),
	("etsy_plus", CATEGORY_FEES),
	("operating_fee", CATEGORY_FEES),
)

# Applied only after the description keywords, so a generic "fee" ledger type never overrides a
# description that names a more specific category.
GENERIC_FEE_LEDGER_TYPES = ("fee",)

# ledger types that mark a tax charged on top of a fee (e.g. "vat_seller_services")
TAX_PREFIXES = ("vat", "tax", "gst", "hst", "pst", "qst", "sales tax")

# ordered keyword rules on the description - first match wins
KEYWORD_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
	(
		CATEGORY_MARKETING,
		(
			"etsy ads",
			"etsy ad ",
			"offsite ad",
			"off-site ad",
			"offsite_ad",
			"advertis",
			"promoted",
			"marketing",
			"ads bill",
			"ads credit",
			"ad credit",
			"ads fee",
			"ad fee",
		),
	),
	(
		CATEGORY_SHIPPING,
		(
			"postage",
			"shipping label",
			"shipping-label",
			"shipping_label",
			"delivery label",
			"label",
		),
	),
	(
		CATEGORY_FEES,
		(
			"listing fee",
			"transaction fee",
			"processing fee",
			"payment processing",
			"regulatory",
			"operating fee",
			"share & save",
			"share and save",
			"subscription",
			"etsy plus",
			"pattern",
			"set-up fee",
			"setup fee",
			"seller fee",
			"fee",
		),
	),
)

REFERENCE_TYPE_RULES: dict[str, str] = {
	"ads": CATEGORY_MARKETING,
	"ad": CATEGORY_MARKETING,
	"advertising": CATEGORY_MARKETING,
	"offsite_ads": CATEGORY_MARKETING,
	"etsy_ads": CATEGORY_MARKETING,
	"prolist": CATEGORY_MARKETING,
	"shipping_label": CATEGORY_SHIPPING,
	"label": CATEGORY_SHIPPING,
	"postage": CATEGORY_SHIPPING,
	"listing": CATEGORY_FEES,
	"transaction": CATEGORY_FEES,
	"processing_fee": CATEGORY_FEES,
	"fee": CATEGORY_FEES,
}


### Payouts

# Etsy moves money to the seller's bank with a "disbursement" (the live API returns the token
# ``DISBURSE2``). A payout the bank sends back arrives as a positive entry of the same type.
PAYOUT_PATTERNS = ("disburse", "payout")
PAYOUT_DESCRIPTION_PREFIXES = (
	"deposit",
	"payout",
	"disburse",
	"returned deposit",
	"returned disbursement",
)


def is_payout(entry: LedgerLike | LedgerEntry) -> bool:
	"""
	True for payouts from the Etsy balance to the seller's bank account, and for payouts that the
	bank returned (positive amount, same ledger type).
	"""
	if not entry.amount:
		return False

	ledger_type = (entry.ledger_type or "").strip().lower()
	if any(pattern in ledger_type for pattern in PAYOUT_PATTERNS):
		return True
	if "deposit" in ledger_type:
		# "deposit" is ambiguous across shops: only treat it as a payout when money actually left
		# the Etsy balance, so an incoming deposit is never booked as a bank transfer.
		return entry.amount < 0

	description = (entry.description or "").strip().lower()
	return description.startswith(PAYOUT_DESCRIPTION_PREFIXES)


### Data classes


@dataclass(frozen=True)
class FeeClassification:
	category: str
	is_tax: bool = False

	@property
	def key(self) -> tuple[str, bool]:
		return (self.category, self.is_tax)

	@property
	def label(self) -> str:
		if self.is_tax:
			return f"Tax on {self.category}"
		return self.category


@dataclass
class FeeBucket:
	"""All ledger entries of one classification, netted."""

	classification: FeeClassification
	total: float = 0.0  # in currency units; negative = charge, positive = credit / refund of a fee
	count: int = 0
	breakdown: dict[str, float] = field(default_factory=dict)  # by ledger description

	@property
	def label(self) -> str:
		return self.classification.label

	def add(self, description: str, amount: float):
		self.total = round(self.total + amount, 2)
		self.count += 1
		self.breakdown[description] = round(self.breakdown.get(description, 0.0) + amount, 2)


### Classification


def _normalise(value: str | None) -> str:
	return (value or "").strip().lower()


def _category_from_ledger_type(ledger_type: str) -> str | None:
	"""Return a category, ``TRANSFER`` for pure money movement, or ``None`` when nothing matches."""
	for pattern, category in LEDGER_TYPE_RULES:
		if pattern in ledger_type:
			return category
	return None


def _category_from_keywords(description: str) -> str | None:
	for category, keywords in KEYWORD_RULES:
		if any(keyword in description for keyword in keywords):
			return category
	return None


def _strip_tax_prefix(description: str) -> str:
	"""``"vat: etsy ads"`` -> ``"etsy ads"``, ``"tax on seller fees"`` -> ``"seller fees"``."""
	if ":" in description:
		return description.split(":", 1)[1].strip()
	for prefix in TAX_PREFIXES:
		if description.startswith(prefix):
			remainder = description[len(prefix) :].strip()
			for joiner in ("on ", "for "):
				if remainder.startswith(joiner):
					remainder = remainder[len(joiner) :]
			return remainder.strip()
	return description


def _is_tax(description: str, ledger_type: str) -> bool:
	"""
	True for a tax Etsy charges the seller on its own fees (``vat_seller_services``,
	``vat_on_processing_fees``, "VAT: Etsy Ads", ...).

	Tax the buyer paid on an order is not a seller tax; those entries carry the ``sales_tax``
	ledger type and are filtered out as transfers before this is called.
	"""
	return ledger_type.startswith(TAX_PREFIXES) or description.startswith(TAX_PREFIXES)


def classify_ledger_entry(entry: LedgerLike | LedgerEntry) -> FeeClassification | None:
	"""
	Return the expense classification of a ledger entry, or ``None`` when the entry is not an expense
	(sales, payouts, buyer refunds, buyer sales tax, zero amounts).
	"""
	if not entry.amount:
		return None

	description = _normalise(entry.description)
	ledger_type = _normalise(entry.ledger_type)
	reference_type = _normalise(entry.reference_type)

	category = _category_from_ledger_type(ledger_type)
	if category == TRANSFER:
		return None

	is_tax = _is_tax(description, ledger_type)

	if category is None:
		# fall back to the description, then to the object the entry refers to, and only then to a
		# generic "fee" ledger type - a specific description must win over a generic type
		haystack = _strip_tax_prefix(description) if is_tax else description
		category = _category_from_keywords(haystack) or REFERENCE_TYPE_RULES.get(reference_type)

	if category is None and any(t in ledger_type for t in GENERIC_FEE_LEDGER_TYPES):
		category = CATEGORY_FEES

	if category is None:
		if "refund" in ledger_type:
			# a refund to a buyer without any fee signal - not an expense of the seller
			return None
		category = CATEGORY_FEES if is_tax else CATEGORY_OTHER

	return FeeClassification(category=category, is_tax=is_tax)


### Aggregation


def ledger_amount(entry: LedgerLike | LedgerEntry) -> float:
	return round(entry.amount / LEDGER_DIVISOR, 2)


def aggregate_fees(entries: list[LedgerLike] | list[LedgerEntry]) -> list[FeeBucket]:
	"""
	Group all expense-like ledger entries into buckets by (category, is_tax).
	The result is ordered by category (Fees, Marketing, Shipping, Other), taxes after their category.
	"""
	buckets: dict[tuple[str, bool], FeeBucket] = {}

	for entry in entries:
		classification = classify_ledger_entry(entry)
		if classification is None:
			continue

		bucket = buckets.get(classification.key)
		if bucket is None:
			bucket = buckets[classification.key] = FeeBucket(classification=classification)

		bucket.add((entry.description or "").strip() or classification.label, ledger_amount(entry))

	def sort_key(bucket: FeeBucket):
		return (CATEGORIES.index(bucket.classification.category), bucket.classification.is_tax)

	return sorted((b for b in buckets.values() if b.total), key=sort_key)


def format_breakdown(bucket: FeeBucket, currency: str = "") -> str:
	"""Human readable one-liner used as remark on the Journal Entry rows."""
	parts = [
		f"{description}: {amount:.2f} {currency}".strip()
		for description, amount in sorted(bucket.breakdown.items(), key=lambda kv: kv[1])
	]
	return f"Etsy {bucket.label} ({bucket.count} entries) - " + "; ".join(parts)


### Journal Entry rows


@dataclass(frozen=True)
class LedgerRow:
	"""One line of a Journal Entry, in the currency of the Etsy ledger. Positive = debit."""

	account: str
	amount: float
	remark: str = ""


class CurrencyMismatch(ValueError):
	pass


class UnbalancedRows(ValueError):
	pass


def _round(value: float) -> float:
	return round(value + 0.0, 2)


def build_journal_rows(
	rows: list[LedgerRow],
	ledger_currency: str,
	company_currency: str,
	exchange_rate: float,
	account_currency_of: Callable[[str], str],
	rounding_account: str | None = None,
) -> list[dict]:
	"""
	Convert ``rows`` (balanced in ``ledger_currency``) into Journal Entry Account dicts.

	Every account may be held in the ledger currency or in the company currency. ERPNext computes
	the company currency amount of each row as ``amount_in_account_currency * exchange_rate`` rounded
	to the cent, so converting several rows independently can leave the entry off by a cent. That
	difference is absorbed into the largest company currency row, or booked to ``rounding_account``
	(e.g. the company's Exchange Gain / Loss account) when every row is in the ledger currency.

	Each returned dict carries an extra ``company_amount`` key (signed, positive = debit) that is
	not a Journal Entry field and must be popped by the caller.
	"""
	if _round(sum(r.amount for r in rows)):
		raise UnbalancedRows(f"Ledger rows do not balance: {rows}")

	result: list[dict] = []
	for row in rows:
		account_currency = account_currency_of(row.account) or company_currency
		if account_currency == ledger_currency:
			amount_ac, rate = _round(row.amount), exchange_rate
		elif account_currency == company_currency:
			amount_ac, rate = _round(row.amount * exchange_rate), 1.0
		else:
			raise CurrencyMismatch(
				f"Account {row.account} is held in {account_currency}, but Etsy amounts are in {ledger_currency}."
			)
		result.append(
			{
				"account": row.account,
				"account_currency": account_currency,
				"exchange_rate": rate,
				"debit_in_account_currency": amount_ac if amount_ac > 0 else 0,
				"credit_in_account_currency": -amount_ac if amount_ac < 0 else 0,
				"user_remark": row.remark,
				"company_amount": _round(amount_ac * rate),
			}
		)

	difference = _round(sum(r["company_amount"] for r in result))
	if difference:
		company_rows = [r for r in result if r["account_currency"] == company_currency]
		if company_rows:
			target = max(company_rows, key=lambda r: abs(r["company_amount"]))
			signed = target["debit_in_account_currency"] - target["credit_in_account_currency"] - difference
			target["debit_in_account_currency"] = signed if signed > 0 else 0
			target["credit_in_account_currency"] = -signed if signed < 0 else 0
			target["company_amount"] = _round(signed)
		elif rounding_account:
			result.append(
				{
					"account": rounding_account,
					"account_currency": company_currency,
					"exchange_rate": 1.0,
					"debit_in_account_currency": -difference if difference < 0 else 0,
					"credit_in_account_currency": difference if difference > 0 else 0,
					"user_remark": "Exchange rate rounding difference",
					"company_amount": _round(-difference),
				}
			)
		else:
			raise UnbalancedRows(
				f"Rounding difference of {difference} {company_currency} cannot be absorbed - "
				"set an Exchange Gain / Loss account on the company."
			)

	return result
