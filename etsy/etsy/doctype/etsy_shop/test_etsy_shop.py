import datetime
from itertools import count
from types import SimpleNamespace

import frappe
from frappe.utils import flt, get_last_day, today

try:
	from frappe.tests import UnitTestCase as FrappeTestCase  # Frappe v16+
except ImportError:
	from frappe.tests.utils import FrappeTestCase  # Frappe v15

from etsy.etsy.doctype.etsy_shop.etsy_shop import (
	book_ledger_summary,
	months_between,
	short_title,
)
from etsy.fees import (
	CATEGORY_FEES,
	CATEGORY_MARKETING,
	CATEGORY_OTHER,
	CATEGORY_SHIPPING,
	LedgerRow,
	UnbalancedRows,
	aggregate_fees,
	build_journal_rows,
	classify_ledger_entry,
	format_breakdown,
	is_payout,
)

_entry_ids = count(900_000_001)


def ledger(
	amount, description, ledger_type="fee", reference_type=None, currency="USD", created=None, entry_id=None
):
	"""Minimal stand-in for an Etsy ``LedgerEntry`` (amounts in cents, negative = charge)."""
	return SimpleNamespace(
		entry_id=entry_id or next(_entry_ids),
		amount=amount,
		description=description,
		ledger_type=ledger_type,
		reference_type=reference_type,
		currency=currency,
		created_timestamp=created or datetime.datetime.now(),
	)


# a typical month, modelled on the Etsy "Recent activities" and monthly statement views
SAMPLE_MONTH = [
	ledger(11119, "Payment", ledger_type="payment", reference_type="payment"),
	ledger(-9000, "Deposit", ledger_type="deposit", reference_type="deposit"),
	ledger(-60, "Listing fee", reference_type="listing"),
	ledger(-723, "Transaction fee", reference_type="transaction"),
	ledger(-403, "Processing fee", reference_type="payment"),
	ledger(-55, "Regulatory Operating fee"),
	ledger(-613, "VAT: Seller fees", ledger_type="vat"),
	ledger(-2605, "Etsy Ads", reference_type="bill"),
	ledger(-846, "Offsite Ads fee"),
	ledger(-20, "VAT: Etsy Ads", ledger_type="vat", reference_type="bill"),
	ledger(-350, "Postage label", ledger_type="shipping"),
	ledger(-1500, "Refund", ledger_type="refund", reference_type="refund"),
	ledger(75, "Transaction fee credit", ledger_type="refund", reference_type="transaction"),
	ledger(-99, "Manual adjustment", ledger_type="misc", reference_type="adjustment"),
	ledger(0, "Listing fee"),
]


def real_ledger(amount, ledger_type, reference_type, **kwargs):
	"""
	A ledger entry exactly as the live Etsy API returns it: ``description`` carries the same machine
	token as ``ledger_type`` ("prolist", "PAYMENT_GROSS", ...), never prose.
	"""
	return ledger(amount, ledger_type, ledger_type=ledger_type, reference_type=reference_type, **kwargs)


# One month of a live shop ledger. Tokens, reference types and totals are taken from a real Etsy
# account, so the expected sums below are the amounts Etsy actually charged.
REAL_MONTH = [
	### money movement, never an expense
	real_ledger(48947, "PAYMENT_GROSS", "shop_payment"),
	real_ledger(-33124, "DISBURSE2", "disbursement"),
	real_ledger(2008, "billing_payment", "billing_payment"),
	real_ledger(-2718, "sales_tax", "receipt"),
	### fees
	real_ledger(-1644, "PAYMENT_PROCESSING_FEE", "processing_fee"),
	real_ledger(-2352, "transaction", "transaction"),
	real_ledger(-20, "transaction_quantity", "transaction"),
	real_ledger(-674, "shipping_transaction", "receipt"),
	real_ledger(-233, "regulatory_operating_fee", "receipt"),
	real_ledger(-20, "listing", "listing"),
	real_ledger(-80, "auto_renew_expired", "listing"),
	real_ledger(-20, "renew_sold", "listing"),
	real_ledger(-120, "renew_sold_auto", "listing"),
	### marketing
	real_ledger(-18260, "prolist", "prolist"),
	real_ledger(-2761, "offsite_ads_fee", "receipt"),
	### tax on seller fees
	real_ledger(-214, "vat_on_processing_fees", "receipt"),
	real_ledger(-3197, "vat_seller_services", "etsy"),
]

REAL_TRANSFERS = (
	("PAYMENT_GROSS", "shop_payment"),
	("DISBURSE2", "disbursement"),
	("billing_payment", "billing_payment"),
	("sales_tax", "receipt"),
)

REAL_FEES = (
	("PAYMENT_PROCESSING_FEE", "processing_fee"),
	("transaction", "transaction"),
	("transaction_quantity", "transaction"),
	("shipping_transaction", "receipt"),
	("regulatory_operating_fee", "receipt"),
	("listing", "listing"),
	("auto_renew_expired", "listing"),
	("renew_sold", "listing"),
	("renew_sold_auto", "listing"),
)

REAL_MARKETING = (("prolist", "prolist"), ("offsite_ads_fee", "receipt"))

REAL_TAX_ON_FEES = (("vat_on_processing_fees", "receipt"), ("vat_seller_services", "etsy"))


class TestRealLedgerTokens(FrappeTestCase):
	"""
	Classification of the machine tokens the live Etsy API actually returns.

	These guard the accounting: a mistake here books sales revenue, payouts or the buyer's sales tax
	as an expense of the seller.
	"""

	def test_money_movement_is_never_an_expense(self):
		for token, reference_type in REAL_TRANSFERS:
			self.assertIsNone(classify_ledger_entry(real_ledger(-100, token, reference_type)), token)
			self.assertIsNone(classify_ledger_entry(real_ledger(100, token, reference_type)), token)

	def test_buyer_sales_tax_is_not_a_seller_expense(self):
		# Etsy collects this from the buyer and remits it as marketplace facilitator; the receipt
		# import leaves it off the Sales Order, so booking it here would invent a cost.
		self.assertIsNone(classify_ledger_entry(real_ledger(-2718, "sales_tax", "receipt")))

	def test_fees(self):
		for token, reference_type in REAL_FEES:
			c = classify_ledger_entry(real_ledger(-100, token, reference_type))
			self.assertEqual(c.category, CATEGORY_FEES, token)
			self.assertFalse(c.is_tax, token)

	def test_marketing(self):
		for token, reference_type in REAL_MARKETING:
			c = classify_ledger_entry(real_ledger(-100, token, reference_type))
			self.assertEqual(c.category, CATEGORY_MARKETING, token)

	def test_tax_on_seller_fees(self):
		for token, reference_type in REAL_TAX_ON_FEES:
			c = classify_ledger_entry(real_ledger(-100, token, reference_type))
			self.assertEqual(c.category, CATEGORY_FEES, token)
			self.assertTrue(c.is_tax, token)

	def test_payouts(self):
		self.assertTrue(is_payout(real_ledger(-33124, "DISBURSE2", "disbursement")))
		self.assertTrue(is_payout(real_ledger(33124, "DISBURSE2", "disbursement")))  # returned payout
		for token, reference_type in REAL_FEES + REAL_MARKETING + REAL_TAX_ON_FEES:
			self.assertFalse(is_payout(real_ledger(-100, token, reference_type)), token)
		self.assertFalse(is_payout(real_ledger(48947, "PAYMENT_GROSS", "shop_payment")))

	def test_nothing_falls_through_to_other(self):
		buckets = aggregate_fees(REAL_MONTH)
		self.assertNotIn(CATEGORY_OTHER, [b.classification.category for b in buckets])

	def test_real_month_totals(self):
		buckets = {b.classification.key: b for b in aggregate_fees(REAL_MONTH)}

		self.assertAlmostEqual(buckets[(CATEGORY_FEES, False)].total, -51.63)
		self.assertAlmostEqual(buckets[(CATEGORY_FEES, True)].total, -34.11)
		self.assertAlmostEqual(buckets[(CATEGORY_MARKETING, False)].total, -210.21)
		self.assertEqual(len(buckets), 3)

		self.assertAlmostEqual(sum(b.total for b in buckets.values()), -295.95)


class TestBookLedgerSummary(FrappeTestCase):
	"""The notification shown after a manual booking."""

	def summary(self, **kwargs):
		args = {
			"etsy_shop": "My Shop",
			"months": [(2026, 1)],
			"created_fees": [],
			"empty_months": [],
			"skipped": [],
			"payout_entries": [],
			"failed": [],
			"payouts": 1,
		}
		args.update(kwargs)
		return book_ledger_summary(**args)

	def test_single_month_is_named(self):
		self.assertIn("2026-01", self.summary())
		self.assertNotIn(" to ", self.summary())

	def test_range_names_both_ends(self):
		text = self.summary(months=[(2026, m) for m in range(1, 9)])
		self.assertIn("2026-01 to 2026-08", text)

	def test_created_entries_are_counted(self):
		text = self.summary(created_fees=[("2026-01", "JE-1"), ("2026-02", "JE-2")])
		self.assertIn("2", text)
		self.assertIn("JE-1", text)

	def test_empty_and_skipped_months_are_named(self):
		text = self.summary(empty_months=["2026-03"], skipped=[("2026-01", "JE-1")])
		self.assertIn("2026-03", text)
		self.assertIn("2026-01", text)

	def test_no_payout_line_when_payouts_were_not_requested(self):
		self.assertNotIn("payout", self.summary(payouts=0).lower())
		self.assertIn("payout", self.summary(payouts=1).lower())

	def test_failures_are_reported(self):
		self.assertIn("2026-01", self.summary(failed=["2026-01"]))

	def test_long_lists_are_truncated(self):
		created = [(f"2026-{m:02d}", f"JE-{m}") for m in range(1, 13)]
		text = self.summary(months=[(2026, m) for m in range(1, 13)], created_fees=created)
		self.assertIn("JE-1", text)
		self.assertNotIn("JE-99", text)


class TestMonthsBetween(FrappeTestCase):
	"""A backfill range selects whole months, one Journal Entry each."""

	def test_single_month(self):
		self.assertEqual(months_between("2026-01-17", "2026-01-17"), [(2026, 1)])

	def test_any_day_selects_the_whole_month(self):
		self.assertEqual(months_between("2026-01-31", "2026-02-01"), [(2026, 1), (2026, 2)])

	def test_backfill_range(self):
		self.assertEqual(
			months_between("2026-01-01", "2026-08-31"),
			[(2026, m) for m in range(1, 9)],
		)

	def test_crosses_the_year_boundary(self):
		self.assertEqual(
			months_between("2025-11-04", "2026-02-20"),
			[(2025, 11), (2025, 12), (2026, 1), (2026, 2)],
		)

	def test_reversed_range_is_accepted(self):
		self.assertEqual(
			months_between("2026-08-31", "2026-01-01"), months_between("2026-01-01", "2026-08-31")
		)

	def test_full_year(self):
		self.assertEqual(len(months_between("2026-01-01", "2026-12-31")), 12)


class TestLedgerRequestWindow(FrappeTestCase):
	"""Etsy rejects a ledger request spanning more than 31 days (2678400 seconds)."""

	ETSY_MAX_WINDOW_SECONDS = 2678400

	def window_seconds(self, days):
		# the request runs from 00:00:00 on the first day to 23:59:59 on the last
		return (days - 1) * 86400 + 86399

	def test_fetch_window_stays_inside_the_limit(self):
		from etsy.etsy.doctype.etsy_shop.etsy_shop import LEDGER_MAX_WINDOW_DAYS

		# + 3600 covers a daylight saving change that sets the clocks back inside the window
		window = self.window_seconds(LEDGER_MAX_WINDOW_DAYS) + 3600
		self.assertLess(window, self.ETSY_MAX_WINDOW_SECONDS)

	def test_payout_lookback_covers_the_longest_sync_interval(self):
		from etsy.api import PAYOUT_LOOKBACK_DAYS

		# Etsy Settings caps the payout sync interval at 30 days; a shorter lookback than that would
		# silently skip payouts whenever a scheduled run is missed.
		self.assertGreaterEqual(PAYOUT_LOOKBACK_DAYS, 30)

	def test_payout_lookback_is_split_into_legal_windows(self):
		from etsy.api import PAYOUT_LOOKBACK_DAYS
		from etsy.etsy.doctype.etsy_shop.etsy_shop import LEDGER_MAX_WINDOW_DAYS

		# the lookback is longer than one request may span, so every chunk must still be legal
		days = PAYOUT_LOOKBACK_DAYS + 1  # the range is inclusive on both ends
		while days > 0:
			chunk = min(LEDGER_MAX_WINDOW_DAYS, days)
			self.assertLess(self.window_seconds(chunk) + 3600, self.ETSY_MAX_WINDOW_SECONDS)
			days -= chunk

	def test_a_calendar_month_would_not_fit_on_its_own(self):
		# a 31 day month is one second inside the limit and a DST change pushes it over, which is
		# why fetch_ledger_entries() splits by LEDGER_MAX_WINDOW_DAYS instead of by month
		self.assertGreater(self.window_seconds(31) + 3600, self.ETSY_MAX_WINDOW_SECONDS)


class TestFeeClassification(FrappeTestCase):
	"""Tests for the pure classification / aggregation logic in etsy.fees."""

	def test_fee_keywords(self):
		for description in ("Listing fee", "Transaction fee", "Processing fee", "Regulatory Operating fee"):
			c = classify_ledger_entry(ledger(-100, description))
			self.assertEqual(c.category, CATEGORY_FEES, description)
			self.assertFalse(c.is_tax)

	def test_marketing_keywords(self):
		for description in ("Etsy Ads", "Offsite Ads fee", "Advertising credit"):
			self.assertEqual(classify_ledger_entry(ledger(-100, description)).category, CATEGORY_MARKETING)

	def test_shipping_keywords(self):
		self.assertEqual(classify_ledger_entry(ledger(-100, "Postage label")).category, CATEGORY_SHIPPING)
		self.assertEqual(classify_ledger_entry(ledger(-100, "Shipping label")).category, CATEGORY_SHIPPING)

	def test_tax_entries_follow_their_fee(self):
		c = classify_ledger_entry(ledger(-20, "VAT: Etsy Ads", ledger_type="vat"))
		self.assertEqual(c.category, CATEGORY_MARKETING)
		self.assertTrue(c.is_tax)

		c = classify_ledger_entry(ledger(-20, "Tax on seller fees", ledger_type="tax"))
		self.assertEqual(c.category, CATEGORY_FEES)
		self.assertTrue(c.is_tax)

		# tax detected by description alone, unknown remainder defaults to Fees
		c = classify_ledger_entry(ledger(-20, "VAT: Something new", ledger_type="misc"))
		self.assertEqual(c.category, CATEGORY_FEES)
		self.assertTrue(c.is_tax)

	def test_transfers_are_ignored(self):
		self.assertIsNone(classify_ledger_entry(ledger(11119, "Payment", ledger_type="payment")))
		self.assertIsNone(classify_ledger_entry(ledger(-9000, "Deposit", ledger_type="deposit")))
		self.assertIsNone(classify_ledger_entry(ledger(-1500, "Refund", ledger_type="refund")))
		self.assertIsNone(classify_ledger_entry(ledger(-500, "Recoupment", ledger_type="recoupment")))
		self.assertIsNone(classify_ledger_entry(ledger(0, "Listing fee")))

	def test_fee_refund_is_a_fee_credit(self):
		c = classify_ledger_entry(ledger(75, "Transaction fee credit", ledger_type="refund"))
		self.assertEqual(c.category, CATEGORY_FEES)

	def test_reference_type_fallback(self):
		self.assertEqual(
			classify_ledger_entry(ledger(-100, "Bill", reference_type="ads")).category, CATEGORY_MARKETING
		)

	def test_unknown_goes_to_other(self):
		c = classify_ledger_entry(ledger(-99, "Manual adjustment", ledger_type="misc"))
		self.assertEqual(c.category, CATEGORY_OTHER)

	def test_missing_types(self):
		c = classify_ledger_entry(ledger(-60, "Listing fee", ledger_type=None, reference_type=None))
		self.assertEqual(c.category, CATEGORY_FEES)

	def test_aggregate_sample_month(self):
		buckets = {b.classification.key: b for b in aggregate_fees(SAMPLE_MONTH)}

		self.assertAlmostEqual(buckets[(CATEGORY_FEES, False)].total, -11.66)  # 0.60+7.23+4.03+0.55-0.75
		self.assertAlmostEqual(buckets[(CATEGORY_FEES, True)].total, -6.13)
		self.assertAlmostEqual(buckets[(CATEGORY_MARKETING, False)].total, -34.51)
		self.assertAlmostEqual(buckets[(CATEGORY_MARKETING, True)].total, -0.20)
		self.assertAlmostEqual(buckets[(CATEGORY_SHIPPING, False)].total, -3.50)
		self.assertAlmostEqual(buckets[(CATEGORY_OTHER, False)].total, -0.99)
		self.assertEqual(len(buckets), 6)

		fees = buckets[(CATEGORY_FEES, False)]
		self.assertEqual(fees.count, 5)
		self.assertAlmostEqual(fees.breakdown["Transaction fee"], -7.23)
		self.assertIn("Transaction fee: -7.23 USD", format_breakdown(fees, "USD"))

	def test_aggregate_order_and_zero_buckets(self):
		buckets = aggregate_fees(
			[
				ledger(-100, "Etsy Ads"),
				ledger(-60, "Listing fee"),
				ledger(60, "Listing fee"),  # nets to zero -> dropped
				ledger(-10, "VAT: Listing fee", ledger_type="vat"),
			]
		)
		self.assertEqual([b.label for b in buckets], ["Tax on Fees", "Marketing"])

	def test_is_payout(self):
		self.assertTrue(is_payout(ledger(-9000, "Deposit", ledger_type="deposit")))
		self.assertTrue(is_payout(ledger(-9000, "Payout", ledger_type=None)))
		self.assertTrue(is_payout(ledger(9000, "Returned deposit", ledger_type="returned_disbursement")))
		self.assertFalse(is_payout(ledger(11119, "Payment", ledger_type="payment")))
		self.assertFalse(is_payout(ledger(-60, "Listing fee")))
		self.assertFalse(is_payout(ledger(0, "Deposit", ledger_type="deposit")))


class TestBuildJournalRows(FrappeTestCase):
	"""Currency conversion and rounding of Journal Entry rows (pure logic)."""

	def currency_of(self, account):
		return {
			"Fees - X": "CAD",
			"Ads - X": "CAD",
			"Etsy Clearing USD - X": "USD",
			"Wise USD - X": "USD",
		}[account]

	def signed(self, row):
		return flt(row["debit_in_account_currency"] - row["credit_in_account_currency"], 2)

	def test_single_currency(self):
		rows = build_journal_rows(
			[LedgerRow("Fees - X", 9.43), LedgerRow("Etsy Clearing USD - X", -9.43)],
			"CAD",
			"CAD",
			1.0,
			lambda a: "CAD",
		)
		self.assertEqual([self.signed(r) for r in rows], [9.43, -9.43])
		self.assertEqual([r["exchange_rate"] for r in rows], [1.0, 1.0])
		self.assertAlmostEqual(sum(r["company_amount"] for r in rows), 0)

	def test_usd_clearing_with_cad_expenses_absorbs_rounding(self):
		# per row: 7.83 -> 10.45, 26.25 -> 35.04 (sum 45.49); total 34.08 -> 45.50 => off by one cent
		rate = 1.335
		rows = build_journal_rows(
			[
				LedgerRow("Fees - X", 7.83),
				LedgerRow("Ads - X", 26.25),
				LedgerRow("Etsy Clearing USD - X", -34.08),
			],
			"USD",
			"CAD",
			rate,
			self.currency_of,
		)
		fees, ads, clearing = rows
		self.assertEqual(clearing["exchange_rate"], rate)
		self.assertEqual(self.signed(clearing), -34.08)  # stays exact in USD
		self.assertEqual(clearing["company_amount"], -45.50)
		self.assertEqual(self.signed(fees), 10.45)
		self.assertEqual(self.signed(ads), 35.05)  # largest CAD row absorbs the cent
		self.assertAlmostEqual(sum(r["company_amount"] for r in rows), 0)
		self.assertEqual(len(rows), 3)  # no extra rounding row

	def test_all_rows_in_ledger_currency_use_rounding_account(self):
		rate = 1.337
		rows = build_journal_rows(
			[
				LedgerRow("Wise USD - X", 33.33),
				LedgerRow("Etsy Clearing USD - X", -11.11),
				LedgerRow("Etsy Clearing USD - X", -11.11),
				LedgerRow("Etsy Clearing USD - X", -11.11),
			],
			"USD",
			"CAD",
			rate,
			self.currency_of,
			rounding_account="Exchange Gain/Loss - X",
		)
		self.assertAlmostEqual(sum(r["company_amount"] for r in rows), 0)
		extra = [r for r in rows if r["account"] == "Exchange Gain/Loss - X"]
		self.assertLessEqual(len(extra), 1)
		for r in extra:
			self.assertLess(abs(self.signed(r)), 0.05)

	def test_payout_two_rows_always_balance(self):
		rows = build_journal_rows(
			[LedgerRow("Wise USD - X", 90.0), LedgerRow("Etsy Clearing USD - X", -90.0)],
			"USD",
			"CAD",
			1.3579,
			self.currency_of,
		)
		self.assertEqual(len(rows), 2)
		self.assertAlmostEqual(sum(r["company_amount"] for r in rows), 0)

	def test_unbalanced_input_rejected(self):
		with self.assertRaises(UnbalancedRows):
			build_journal_rows([LedgerRow("Fees - X", 1.0)], "CAD", "CAD", 1.0, lambda a: "CAD")


class TestFeeJournalEntry(FrappeTestCase):
	"""Creates a real (draft) Journal Entry from sample ledger entries."""

	SHOP_NAME = "_Test Etsy Shop Fees"

	def setUp(self):
		self.company = frappe.db.get_value(
			"Company", {"company_name": "Wind Power LLC"}
		) or frappe.db.get_value("Company", {}, "name")
		self.currency = frappe.get_cached_value("Company", self.company, "default_currency")

		self.bank_account = frappe.db.get_value(
			"Account",
			{"company": self.company, "account_type": ("in", ["Bank", "Cash"]), "is_group": 0},
			"name",
		)
		expense_accounts = [
			a.name
			for a in frappe.get_all(
				"Account",
				filters={"company": self.company, "root_type": "Expense", "is_group": 0},
				fields=["name", "account_type"],
				order_by="name",
			)
			if (a.account_type or "") in ("", "Expense Account")
		]
		self.assertGreaterEqual(len(expense_accounts), 2, "test company needs at least two expense accounts")
		self.fees_account, self.marketing_account = expense_accounts[:2]

		self.payout_account = frappe.db.get_value(
			"Account",
			{
				"company": self.company,
				"account_type": ("in", ["Bank", "Cash"]),
				"is_group": 0,
				"name": ("!=", self.bank_account),
			},
			"name",
		)
		if not self.payout_account:
			parent = frappe.db.get_value("Account", self.bank_account, "parent_account")
			self.payout_account = (
				frappe.get_doc(
					{
						"doctype": "Account",
						"account_name": "_Test Etsy Payout Bank",
						"company": self.company,
						"parent_account": parent,
						"account_type": "Bank",
						"is_group": 0,
					}
				)
				.insert(ignore_if_duplicate=True)
				.name
			)

		if not frappe.db.exists("Etsy Shop", self.SHOP_NAME):
			frappe.get_doc(
				{
					"doctype": "Etsy Shop",
					"shop_name": self.SHOP_NAME,
					"company": self.company,
					"shipping_income_account": self.bank_account,
					"bank_account": self.bank_account,
					"fees_expense_account": self.fees_account,
					"marketing_expense_account": self.marketing_account,
					"payout_account": self.payout_account,
				}
			).insert(ignore_permissions=True)
		self.shop = frappe.get_doc("Etsy Shop", self.SHOP_NAME)

		period = frappe.utils.getdate(today())
		self.year, self.month = period.year, period.month

	def tearDown(self):
		for name in frappe.get_all("Journal Entry", filters={"etsy_shop": self.SHOP_NAME}, pluck="name"):
			je = frappe.get_doc("Journal Entry", name)
			if je.docstatus == 1:
				je.cancel()
			frappe.db.delete("GL Entry", {"voucher_type": "Journal Entry", "voucher_no": name})
			frappe.delete_doc("Journal Entry", name, force=True, ignore_permissions=True)
		frappe.delete_doc("Etsy Shop", self.SHOP_NAME, force=True, ignore_permissions=True)

	def entries(self):
		return [
			ledger(11119, "Payment", ledger_type="payment", currency=self.currency),
			ledger(-60, "Listing fee", currency=self.currency),
			ledger(-723, "Transaction fee", currency=self.currency),
			ledger(-2605, "Etsy Ads", currency=self.currency),
			ledger(-20, "VAT: Etsy Ads", ledger_type="vat", currency=self.currency),
		]

	def test_create_journal_entry(self):
		name = self.shop.create_fee_journal_entry(self.year, self.month, ledger_entries=self.entries())
		self.assertTrue(name)

		je = frappe.get_doc("Journal Entry", name)
		self.assertEqual(je.docstatus, 0)  # draft by default
		self.assertEqual(je.company, self.company)
		self.assertEqual(je.etsy_shop, self.SHOP_NAME)
		self.assertEqual(je.etsy_fee_period, f"{self.year:04d}-{self.month:02d}")
		self.assertEqual(str(je.posting_date), str(get_last_day(today())))

		debits = {}
		credits = {}
		for row in je.accounts:
			debits[row.account] = flt(debits.get(row.account, 0) + row.debit, 2)
			credits[row.account] = flt(credits.get(row.account, 0) + row.credit, 2)

		self.assertAlmostEqual(debits[self.fees_account], 7.83)
		self.assertAlmostEqual(debits[self.marketing_account], 26.25)  # ads + VAT on ads (no tax account set)
		self.assertAlmostEqual(credits[self.bank_account], 34.08)
		self.assertAlmostEqual(je.total_debit, je.total_credit)

	def test_journal_entry_is_idempotent(self):
		first = self.shop.create_fee_journal_entry(self.year, self.month, ledger_entries=self.entries())
		second = self.shop.create_fee_journal_entry(self.year, self.month, ledger_entries=self.entries())
		self.assertEqual(first, second)
		self.assertEqual(self.shop.get_fee_journal_entry(self.SHOP_NAME, self.year, self.month), first)

	def test_no_fees_no_journal_entry(self):
		entries = [ledger(11119, "Payment", ledger_type="payment", currency=self.currency)]
		self.assertIsNone(self.shop.create_fee_journal_entry(self.year, self.month, ledger_entries=entries))

	def test_tax_account_and_submit(self):
		tax_account = frappe.db.get_value(
			"Account", {"company": self.company, "account_type": "Tax", "is_group": 0}, "name"
		)
		self.shop.fee_tax_account = tax_account
		self.shop.fee_journal_entry_submit = 1
		self.shop.save(ignore_permissions=True)

		name = self.shop.create_fee_journal_entry(self.year, self.month, ledger_entries=self.entries())
		je = frappe.get_doc("Journal Entry", name)
		self.assertEqual(je.docstatus, 1)

		tax_rows = [row for row in je.accounts if row.account == tax_account]
		self.assertEqual(len(tax_rows), 1)
		self.assertAlmostEqual(tax_rows[0].debit, 0.20)

	def test_payout_journal_entries(self):
		created = datetime.datetime.combine(frappe.utils.getdate(today()), datetime.time(10, 0))
		entries = [
			*self.entries(),
			ledger(-9000, "Deposit", ledger_type="deposit", currency=self.currency, created=created),
			ledger(-1234, "Deposit", ledger_type="deposit", currency=self.currency, created=created),
		]
		names = self.shop.create_payout_journal_entries(entries)
		self.assertEqual(len(names), 2)

		je = frappe.get_doc("Journal Entry", names[0])
		self.assertEqual(je.voucher_type, "Bank Entry")
		self.assertEqual(je.docstatus, 0)
		self.assertEqual(str(je.posting_date), str(created.date()))
		self.assertEqual(je.etsy_shop, self.SHOP_NAME)
		self.assertEqual(je.etsy_ledger_entry_id, str(entries[-2].entry_id))
		self.assertIn(str(entries[-2].entry_id), je.cheque_no)

		rows = {row.account: (row.debit, row.credit) for row in je.accounts}
		self.assertAlmostEqual(rows[self.payout_account][0], 90.0)
		self.assertAlmostEqual(rows[self.bank_account][1], 90.0)

		# idempotent: nothing new on a second run, fees untouched
		self.assertEqual(self.shop.create_payout_journal_entries(entries), [])
		self.assertEqual(frappe.db.count("Journal Entry", {"etsy_shop": self.SHOP_NAME}), 2)

	def test_returned_payout_is_reversed(self):
		entry = ledger(5000, "Returned deposit", ledger_type="returned_disbursement", currency=self.currency)
		(name,) = self.shop.create_payout_journal_entries([entry])
		rows = {
			row.account: (row.debit, row.credit) for row in frappe.get_doc("Journal Entry", name).accounts
		}
		self.assertAlmostEqual(rows[self.bank_account][0], 50.0)  # back into the Etsy balance
		self.assertAlmostEqual(rows[self.payout_account][1], 50.0)

	def test_book_ledger_month(self):
		entries = [*self.entries(), ledger(-9000, "Deposit", ledger_type="deposit", currency=self.currency)]
		result = self.shop.book_ledger_month(self.year, self.month, ledger_entries=entries)
		self.assertTrue(result["fee_journal_entry"])
		self.assertEqual(len(result["payout_journal_entries"]), 1)

		# fees only, payouts skipped
		again = self.shop.book_ledger_month(self.year, self.month, payouts=False, ledger_entries=entries)
		self.assertEqual(again["fee_journal_entry"], result["fee_journal_entry"])
		self.assertEqual(again["payout_journal_entries"], [])

	def test_missing_payout_account(self):
		self.shop.payout_account = None
		entry = ledger(-9000, "Deposit", ledger_type="deposit", currency=self.currency)
		self.assertRaises(frappe.ValidationError, self.shop.create_payout_journal_entries, [entry])

	def test_missing_expense_account(self):
		self.shop.fees_expense_account = None
		self.assertRaises(
			frappe.ValidationError,
			self.shop.create_fee_journal_entry,
			self.year,
			self.month,
			ledger_entries=self.entries(),
		)


class TestShortTitle(FrappeTestCase):
	"""Tests for the short_title utility function."""

	def test_simple_title(self):
		self.assertEqual(short_title("Simple Title"), "Simple Title")

	def test_comma_split(self):
		self.assertEqual(short_title("Part One, Part Two, Part Three"), "Part One")

	def test_semicolon_split(self):
		self.assertEqual(short_title("Part One; Part Two"), "Part One")

	def test_pipe_split(self):
		self.assertEqual(short_title("Part One | Part Two"), "Part One")

	def test_bullet_split(self):
		result = short_title("Part One \u2022 Part Two")
		self.assertEqual(result, "Part One")

	def test_dash_split(self):
		self.assertEqual(short_title("Part One - Part Two"), "Part One")

	def test_endash_split(self):
		result = short_title("Part One \u2013 Part Two")
		self.assertEqual(result, "Part One")

	def test_html_entity_replacement(self):
		self.assertEqual(short_title("He said &quot;hello&quot;"), 'He said "hello"')

	def test_truncation_to_60_chars(self):
		long_title = "A" * 100
		self.assertEqual(len(short_title(long_title)), 60)

	def test_strips_whitespace(self):
		self.assertEqual(short_title("  Hello World  , more stuff"), "Hello World")
