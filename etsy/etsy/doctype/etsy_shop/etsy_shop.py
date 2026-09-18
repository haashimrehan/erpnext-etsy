import base64
import datetime
import hashlib
import os
import secrets
from urllib.parse import quote_plus, unquote_plus, urlencode, urljoin

import erpnext
import frappe
import pytz
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
from erpnext.selling.doctype.sales_order.sales_order import close_or_unclose_sales_orders, make_sales_invoice
from erpnext.setup.utils import get_exchange_rate
from frappe import _
from frappe.model.document import Document
from frappe.utils import (
	add_days,
	cint,
	cstr,
	flt,
	get_first_day,
	get_last_day,
	get_link_to_form,
	get_system_timezone,
	getdate,
)
from requests_oauthlib import OAuth2Session

from etsy.api import (
	EtsyAPI,
	QP_getListingsByShop,
	QP_getShopPaymentAccountLedgerEntries,
	QP_getShopReceipts,
	fetch_all,
)
from etsy.datastruct import LedgerEntry, ListingType
from etsy.fees import (
	CATEGORY_FEES,
	CATEGORY_MARKETING,
	CATEGORY_OTHER,
	CATEGORY_SHIPPING,
	FeeBucket,
	LedgerRow,
	aggregate_fees,
	build_journal_rows,
	format_breakdown,
	is_payout,
	ledger_amount,
)

AUTHORIZATION_URI = "https://www.etsy.com/oauth/connect"
TOKEN_URI = "https://api.etsy.com/v3/public/oauth/token"
SCOPES = ["address_r", "email_r", "listings_r", "shops_r", "transactions_r"]
QUERY_PARAMS = {}
LISTING_STATES = ("active", "inactive", "sold_out", "draft", "expired")

# Etsy rejects a ledger request whose window is longer than 31 days (2678400 seconds).
LEDGER_MAX_WINDOW_DAYS = 30

# Guards a mistyped backfill range from queueing years of API calls.
MAX_BACKFILL_MONTHS = 36


if any((os.getenv("CI"), frappe.conf.developer_mode, frappe.conf.allow_tests)):
	# Disable mandatory TLS in developer mode and tests
	os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"


def months_between(from_date, to_date) -> list[tuple[int, int]]:
	"""
	Every ``(year, month)`` the period touches, oldest first and inclusive on both ends.

	Only the months matter, so any day inside a month selects that whole month.
	"""
	start, end = getdate(from_date), getdate(to_date)
	if end < start:
		start, end = end, start

	months: list[tuple[int, int]] = []
	year, month = start.year, start.month
	while (year, month) <= (end.year, end.month):
		months.append((year, month))
		year, month = (year + 1, 1) if month == 12 else (year, month + 1)
	return months


def short_title(title: str) -> str:  # TODO: move to utils
	return (
		title.replace("&quot;", '"')
		.split(",")[0]
		.split(";")[0]
		.split("|")[0]
		.split("•")[0]
		.split(" - ")[0]
		.split(" – ")[0]
		.strip()[:60]
	)


class EtsyShop(Document):
	### hooks
	def validate(self):
		# redirect_uri
		base_url = frappe.utils.get_url()  # or e.g.: "http://localhost:8000"
		if self.use_localhost:
			splt = base_url.split(":")
			base_url = f"{splt[0]}://localhost:{splt[-1]}"
		callback_path = f"/api/method/etsy.etsy.doctype.etsy_shop.etsy_shop.callback/{quote_plus(self.name)}"
		self.redirect_uri = urljoin(base_url, callback_path)

	### public
	def get_auth_header(self) -> dict:
		if not self.token_exists():
			frappe.log_error(f"Etsy: Access token does not exist for shop {self.name}")
			return None

		if self.token_expired():
			oauth_session = self.get_oauth2_session()

			try:
				token = oauth_session.refresh_token(
					body=f"redirect_uri={self.redirect_uri}",
					token_url=TOKEN_URI,
				)
			except Exception:
				frappe.log_error(f"Etsy: Token refresh failed for shop {self.name}")
				return None

			self.token_update(token)

		return {
			"x-api-key": f"{self.client_id}:{self.get_password('client_secret')}",
			"Authorization": f"Bearer {self.get_password('access_token')}",
		}

	### private
	# Token
	def token_exists(self) -> bool:
		return bool(self.get_password("access_token", False))

	def token_expires_in(self) -> int:
		system_timezone = pytz.timezone(get_system_timezone())
		modified = frappe.utils.get_datetime(self.expires_in_datetime)
		modified = system_timezone.localize(modified)
		expiry_utc = modified.astimezone(pytz.utc)
		now_utc = datetime.datetime.now(pytz.utc)
		return cint((expiry_utc - now_utc).total_seconds())  # why do comp. in UTC?

	def token_expired(self) -> bool:
		return self.token_expires_in() < 0

	def update_expires_in(self, seconds: int):
		self.expires_in = seconds
		self.expires_in_datetime = frappe.utils.add_to_date(
			datetime.datetime.now(pytz.timezone(get_system_timezone())),
			seconds=seconds,
			as_string=True,
			as_datetime=True,
		)

	def token_update(self, data: dict):
		"""
		Store data returned by authorization flow.

		Params:
		data - Dict with access_token, refresh_token, expires_in and scope.
		"""
		self.access_token = cstr(data.get("access_token", ""))
		self.refresh_token = cstr(data.get("refresh_token"))
		self.token_type = cstr(data.get("token_type", ""))
		self.update_expires_in(cint(data.get("expires_in", 0)))

		self.token_state = None
		self.save(ignore_permissions=True)
		frappe.db.commit()

		try:
			me = EtsyAPI(self).getMe()
		except Exception:
			frappe.log_error(f"Etsy: Failed to verify connection for shop {self.name}")
			self.status = "Disconnected"
		else:
			self.status = "Connected"
			self.user_id = cstr(me.user_id)
			self.shop_id = cstr(me.shop_id)
		self.save(ignore_permissions=True)
		frappe.db.commit()

	def token_json(self) -> dict:
		return {
			"access_token": self.get_password("access_token", False),
			"refresh_token": self.get_password("refresh_token", False),
			"expires_in": self.token_expires_in(),
			"token_type": self.token_type,
		}

	# OAuth
	def get_oauth2_session(self, init=False) -> OAuth2Session:
		"""Return an auto-refreshing OAuth2 session which is an extension of a requests.Session()"""
		token = None
		token_updater = None
		auto_refresh_kwargs = None

		if not init:
			token = self.token_json()
			token_updater = self.token_update
			auto_refresh_kwargs = {"client_id": self.client_id}
			client_secret = self.get_password("client_secret")
			if client_secret:
				auto_refresh_kwargs["client_secret"] = client_secret

		return OAuth2Session(
			client_id=self.client_id,
			token=token,
			token_updater=token_updater,
			auto_refresh_url=TOKEN_URI,
			auto_refresh_kwargs=auto_refresh_kwargs,
			redirect_uri=self.redirect_uri,
			scope=SCOPES,
		)

	def generate_code_verifier(self) -> str:
		self.code_verifier = secrets.token_urlsafe(48)
		self.save()
		return self.code_verifier

	def generate_code_challenge(self) -> str:
		m = hashlib.sha256(self.code_verifier.encode("utf-8"))
		b64_encode = base64.urlsafe_b64encode(m.digest()).decode("utf-8")
		# per https://docs.python.org/3/library/base64.html, there may be a trailing '=' - get rid of it
		return b64_encode.split("=")[0]

	@frappe.whitelist()
	def initiate_web_application_flow(self) -> str:
		if not self.client_id:
			frappe.throw(_("CLIENT_ID is mandatory!"))
		if not self.client_secret:
			frappe.throw(_("CLIENT_SECRET is mandatory!"))

		self.generate_code_verifier()
		code_challenge = self.generate_code_challenge()

		oauth = self.get_oauth2_session(init=True)
		authorization_url, state = oauth.authorization_url(
			AUTHORIZATION_URI, code_challenge=code_challenge, code_challenge_method="S256", **QUERY_PARAMS
		)

		self.token_state = state
		self.save(ignore_permissions=True)
		frappe.db.commit()

		return authorization_url

	@frappe.whitelist()
	def disconnect_etsy_shop(self):
		self.user_id = None
		self.shop_id = None
		self.access_token = None
		self.refresh_token = None
		self.expires_in = 0
		self.token_type = None
		self.token_state = None
		self.status = "Disconnected"
		self.save(ignore_permissions=True)
		frappe.db.commit()

	### Etsy Shop data import methods ###
	@frappe.whitelist()
	def enqueue_import_listings(
		self, listing_state: str = "active", include_attributes: int = 1, include_items: int = 0
	):
		"""Enqueue listing import as a background job to avoid request timeouts."""
		frappe.enqueue(
			"etsy.etsy.doctype.etsy_shop.etsy_shop.run_import_listings",
			queue="long",
			timeout=3600,
			enqueue_after_commit=True,
			user=frappe.session.user,
			etsy_shop=self.name,
			listing_state=listing_state,
			include_attributes=include_attributes,
			include_items=include_items,
		)

	@frappe.whitelist()
	def enqueue_import_receipts(self, min_date: str | None = None, max_date: str | None = None):
		"""Enqueue receipt import as a background job to avoid request timeouts."""
		frappe.enqueue(
			"etsy.etsy.doctype.etsy_shop.etsy_shop.run_import_receipts",
			queue="long",
			timeout=3600,
			enqueue_after_commit=True,
			user=frappe.session.user,
			etsy_shop=self.name,
			min_date=min_date,
			max_date=max_date,
		)

	@frappe.whitelist()
	def enqueue_book_ledger(
		self, from_date: str, to_date: str | None = None, fees: int = 1, payouts: int = 1
	):
		"""
		Enqueue booking of Etsy fees and/or payouts for every month the period touches.

		Each month is booked into its own Journal Entry, dated in that month, so a backfill of
		several months is one action but still produces correct monthly postings.
		"""
		fees, payouts = cint(fees), cint(payouts)
		if not (fees or payouts):
			frappe.throw(_("Select at least one of 'Book Fees' and 'Book Payouts'."))
		if fees:
			self.validate_fee_settings()
		if payouts:
			self.validate_payout_settings()

		months = months_between(from_date, to_date or from_date)
		if len(months) > MAX_BACKFILL_MONTHS:
			frappe.throw(
				_("That period covers {0} months. Please book at most {1} months at a time.").format(
					len(months), MAX_BACKFILL_MONTHS
				)
			)

		frappe.enqueue(
			"etsy.etsy.doctype.etsy_shop.etsy_shop.run_book_ledger",
			queue="long",
			timeout=3600,
			enqueue_after_commit=True,
			user=frappe.session.user,
			etsy_shop=self.name,
			from_date=str(getdate(from_date)),
			to_date=str(getdate(to_date or from_date)),
			fees=fees,
			payouts=payouts,
		)

		return len(months)

	def import_listings(
		self,
		listing_state: str = "active",
		include_attributes: int = 1,
		include_items: int = 0,
		etsy_api: EtsyAPI | None = None,
	):
		api = etsy_api or EtsyAPI(self)

		if listing_state == "all":
			for state in LISTING_STATES:
				self.import_listings(
					listing_state=state,
					include_attributes=include_attributes,
					include_items=include_items,
					etsy_api=api,
				)
			return

		if listing_state not in LISTING_STATES:
			frappe.throw(_("'listing_state' must be one of: {0}").format(LISTING_STATES))

		for listing in fetch_all(
			lambda o: api.getListingsByShop(
				QP_getListingsByShop(
					shop_id=self.shop_id,
					state=listing_state,
					limit=100,
					offset=o,
					includes=["Inventory", "Images"],
				)
			)
		):
			try:
				### Etsy Listing
				if frappe.db.exists("Etsy Listing", cstr(listing.listing_id)):
					etsy_listing = frappe.get_doc("Etsy Listing", cstr(listing.listing_id))
				else:
					etsy_listing = frappe.new_doc("Etsy Listing")
					etsy_listing.listing_id = cstr(listing.listing_id)
					etsy_listing.etsy_shop = self.name
					# Etsy Listing Settings
					etsy_listing.item_name = short_title(listing.title)
					etsy_listing.item_group = self.item_group or frappe.defaults.get_global_default(
						"item_group"
					)
					etsy_listing.stock_uom = self.stock_uom or frappe.defaults.get_global_default("stock_uom")
					etsy_listing.is_stock_item = 1 - int(listing.listing_type is ListingType.DOWNLOAD)

				etsy_listing.status = listing_state.replace("_", " ").title()
				etsy_listing.views = listing.views
				etsy_listing.likes = listing.num_favorers

				etsy_listing.title = listing.title
				etsy_listing.description = listing.description

				etsy_listing.set("tags", [])
				for tag in listing.tags:
					etsy_listing.append("tags", {"tag": tag})

				if listing.images:
					etsy_listing.image = listing.images[0].get("url_170x135")

				etsy_listing.flags.ignore_mandatory = True
				etsy_listing.save()

				if int(include_items):
					etsy_listing.update_items(listing)  # create items and attributes
				elif int(include_attributes):
					etsy_listing.update_attributes(listing)  # just create attributes

				frappe.db.commit()
			except Exception:
				frappe.db.rollback()
				frappe.log_error(f"Etsy: Failed to import listing {listing.listing_id}")

	def get_receivable_account(self, currency: str) -> str | None:
		"""Find a non-group Receivable account in the given currency for this company.

		Returns None if there is no match, in which case ERPNext falls back to the
		company default (debit_to stays unset and is resolved normally).
		"""
		return frappe.db.get_value(
			"Account",
			{
				"company": self.company,
				"account_type": "Receivable",
				"account_currency": currency,
				"is_group": 0,
			},
		)

	def import_receipts(
		self, min_date: str | None = None, max_date: str | None = None, abort_on_exist: bool = False
	):
		api = EtsyAPI(self)

		company_currency = frappe.get_cached_value("Company", self.company, "default_currency")

		for receipt in fetch_all(
			lambda o: api.getShopReceipts(
				QP_getShopReceipts(
					shop_id=self.shop_id,
					min_created=int(frappe.utils.get_datetime(f"{min_date} 00:00:00").timestamp())
					if min_date
					else None,
					max_created=int(frappe.utils.get_datetime(f"{max_date} 23:59:59").timestamp())
					if max_date
					else None,
					limit=100,
					offset=o,
				)
			)
		):
			if frappe.db.exists("Sales Order", {"etsy_order_id": receipt.receipt_id}):
				if abort_on_exist:
					break
				else:
					continue

			try:
				### Customer
				if customer_name := frappe.db.exists("Customer", {"etsy_customer_id": receipt.buyer_user_id}):
					customer = frappe.get_doc("Customer", customer_name)
				else:
					customer = frappe.new_doc("Customer")
					if naming_series := self.customer_naming_series:
						customer.naming_series = str(naming_series).replace(
							"{ETSY_BUYER_ID}", str(receipt.buyer_user_id)
						)
					customer.etsy_customer_id = receipt.buyer_user_id

				customer.customer_name = receipt.name
				customer.customer_type = self.customer_type or "Individual"
				customer.customer_group = self.customer_group or frappe.defaults.get_global_default(
					"customer_group"
				)

				customer.flags.ignore_mandatory = True
				customer.save()

				### Address
				if address_name := frappe.db.exists("Address", f"{customer.name}-Billing"):
					address = frappe.get_doc("Address", address_name)
				else:
					address = frappe.new_doc("Address")

				address.address_title = customer.name
				address.address_type = "Billing"
				address.address_line1 = receipt.first_line
				address.address_line2 = receipt.second_line
				address.city = receipt.city
				address.state = receipt.state
				address.pincode = receipt.zip
				address.country = frappe.db.get_value("Country", {"code": receipt.country_iso.lower()})
				address.email_id = receipt.buyer_email
				address.is_primary_address = 1
				address.is_shipping_address = 1

				address.append("links", {"link_doctype": "Customer", "link_name": customer.name})

				address.flags.ignore_mandatory = True
				address.save()

				### Contact - makes no sense without email address
				if receipt.buyer_email:
					if contact_name := frappe.db.exists(
						"Contact", {"etsy_customer_id": receipt.buyer_user_id}
					):
						contact = frappe.get_doc("Contact", contact_name)
					else:
						contact = frappe.new_doc("Contact")
						contact.etsy_customer_id = receipt.buyer_user_id

					contact.first_name = receipt.name.split(" ", 1)[0]
					contact.last_name = receipt.name.split(" ", 1)[-1]
					contact.email_id = receipt.buyer_email
					contact.add_email(receipt.buyer_email, is_primary=1)
					contact.is_primary_contact = 1
					contact.is_billing_contact = 1

					contact.append("links", {"link_doctype": "Customer", "link_name": customer.name})

					contact.flags.ignore_mandatory = True
					contact.save()

					# update customer - only if contact is created
					customer.customer_primary_address = address.name
					customer.customer_primary_contact = contact.name
					customer.save()

				### Currency
				# Etsy reports every monetary amount on the receipt in the shop/order
				# currency carried by the MonetaryAmount objects. Use it so USD orders
				# post as USD documents instead of being mislabeled company currency.
				order_currency = receipt.grandtotal.currency_code.value
				if order_currency != company_currency:
					conversion_rate = get_exchange_rate(
						order_currency, company_currency, str(receipt.created_timestamp.date())
					)
				else:
					conversion_rate = 1.0

				### Sales Order
				sales_order: Document = frappe.new_doc("Sales Order")
				if naming_series := self.sales_order_naming_series:
					sales_order.naming_series = str(naming_series).replace(
						"{ETSY_ORDER_ID}", str(receipt.receipt_id)
					)
				sales_order.etsy_order_id = sales_order.po_no = receipt.receipt_id
				sales_order.customer = customer.name
				sales_order.company = self.company

				sales_order.currency = order_currency
				sales_order.conversion_rate = conversion_rate
				# keep price list conversion consistent with the order currency so
				# ERPNext does not recompute rates from a company-currency price list
				sales_order.price_list_currency = order_currency
				sales_order.plc_conversion_rate = conversion_rate

				sales_order.transaction_date = sales_order.po_date = receipt.created_timestamp.date()
				sales_order.delivery_date = max(
					[t.expected_ship_date.date() for t in receipt.transactions if t.expected_ship_date]
					+ [receipt.create_timestamp.date()]
				)

				# Items
				for transaction in receipt.transactions:
					if item_name := frappe.db.exists("Item", {"etsy_product_id": transaction.product_id}):
						item = frappe.get_doc("Item", item_name)
					else:
						item = frappe.new_doc("Item")
						item.item_code = f"{transaction.product_id}"
						item.etsy_product_id = cstr(transaction.product_id)
						item.item_name = short_title(transaction.title)
						item.item_group = self.item_group or frappe.defaults.get_global_default("item_group")
						item.stock_uom = self.stock_uom or frappe.defaults.get_global_default("stock_uom")
						item.is_stock_item = 1 - int(transaction.is_digital)
						item.image = (
							api.rest.getListingImage(
								api.client, transaction.listing_id, transaction.listing_image_id
							)
							.json()
							.get("url_170x135")
						)
						item.flags.ignore_mandatory = True
						item.save()

					sales_order_item = {
						"item_code": item.name,
						"item_name": item.item_name,
						"delivery_date": transaction.expected_ship_date.date()
						if transaction.expected_ship_date
						else sales_order.delivery_date,
						"uom": item.stock_uom,
						"qty": transaction.quantity,
						# rate is in the order currency set above
						"rate": transaction.price.as_float(),
						"description": "".join(
							[
								f"<b>{v.formatted_name}:</b> {v.formatted_value}<br>"
								for v in transaction.variations
							]
						),
					}
					# Cost Center
					cost_center = (
						self.cost_center_digital if transaction.is_digital else self.cost_center_physical
					)
					if cost_center:
						sales_order_item["cost_center"] = cost_center

					# Warehouse (physical items only)
					if not transaction.is_digital and self.warehouse:
						sales_order_item["warehouse"] = self.warehouse

					sales_order.append("items", sales_order_item)

				# VAT and Shipping
				# Note: total_tax_cost (US/non-EU marketplace facilitator tax) is intentionally excluded —
				# Etsy collects and remits it directly and deducts it from the seller's payout, so it is
				# never the seller's revenue and must not appear as a receivable.
				# tax_amount values below are in the order currency (matches receipt amounts).
				if self.vat_account and receipt.total_vat_cost.as_float() > 0.0:
					sales_order.append(
						"taxes",
						{
							"charge_type": "Actual",
							"account_head": self.vat_account,
							"tax_amount": receipt.total_vat_cost.as_float(),
							"description": "VAT Total",
						},
					)
				if receipt.total_shipping_cost.as_float() > 0.0:
					sales_order.append(
						"taxes",
						{
							"charge_type": "Actual",
							"account_head": self.shipping_income_account,
							"tax_amount": receipt.total_shipping_cost.as_float(),
							"description": "Shipping Cost",
						},
					)
				if receipt.gift_wrap_price.as_float() > 0.0:
					sales_order.append(
						"taxes",
						{
							"charge_type": "Actual",
							"account_head": self.shipping_income_account,
							"tax_amount": receipt.gift_wrap_price.as_float(),
							"description": "Gift Wrap",
						},
					)

				# Discount
				if receipt.discount_amt.as_float() > 0.0:
					sales_order.discount_amount = receipt.discount_amt.as_float()
					sales_order.apply_discount_on = "Grand Total"

				sales_order.flags.ignore_mandatory = True
				sales_order.insert(ignore_permissions=True)
				sales_order.submit()

				### Sales Invoice
				sales_invoice: Document = make_sales_invoice(sales_order.name)
				if naming_series := self.sales_invoice_naming_series:
					sales_invoice.naming_series = str(naming_series).replace(
						"{ETSY_ORDER_ID}", str(receipt.receipt_id)
					)
				sales_invoice.etsy_order_id = receipt.receipt_id
				sales_invoice.set_posting_time = 1
				sales_invoice.posting_date = receipt.created_timestamp.date()
				sales_invoice.due_date = receipt.created_timestamp.date()

				# Receivable account matching the order currency (e.g. USD AR for USD
				# orders). Falls back to the company default when no such account exists.
				if order_currency != company_currency:
					if receivable_account := self.get_receivable_account(order_currency):
						sales_invoice.debit_to = receivable_account

				# Income Accounts
				if self.income_account_physical or self.income_account_digital:
					for invoice_item in sales_invoice.items:
						is_stock = frappe.db.get_value("Item", invoice_item.item_code, "is_stock_item")
						income_account = (
							self.income_account_digital if not is_stock else self.income_account_physical
						)
						if income_account:
							invoice_item.income_account = income_account

				# Discount Account
				if self.discount_account and sales_invoice.discount_amount:
					sales_invoice.discount_account = self.discount_account

				sales_invoice.insert(ignore_permissions=True)
				sales_invoice.submit()

				### Payment
				# self.bank_account should point at an "Etsy Clearing" account (ideally
				# in the payout currency, e.g. USD), NOT the real bank account. Etsy
				# deposits batched payouts net of fees, so per-order payment entries can
				# only ever reconcile against a clearing account; the real bank deposit
				# is recorded separately per payout (bank + fees vs clearing).
				if receipt.is_paid:
					payment_entry: Document = get_payment_entry(
						sales_invoice.doctype, sales_invoice.name, bank_account=self.bank_account
					)
					payment_entry.reference_no = sales_invoice.name
					payment_entry.posting_date = receipt.created_timestamp.date()
					payment_entry.reference_date = receipt.created_timestamp.date()
					payment_entry.insert(ignore_permissions=True)
					payment_entry.submit()

					# close Sales Order if is_shipped or everything is_digital
					if receipt.is_shipped or all([t.is_digital for t in receipt.transactions]):
						close_or_unclose_sales_orders(f'["{sales_order.name}"]', "Closed")

				frappe.db.commit()
			except Exception:
				frappe.db.rollback()
				frappe.log_error(f"Etsy: Failed to import receipt {receipt.receipt_id}")

	### Etsy ledger -> Journal Entries (fees & payouts) ###
	def validate_fee_settings(self):
		if not self.fees_expense_account:
			frappe.throw(
				_("Please set the 'Etsy Fees Expense Account' in the Fee Settings of Etsy Shop {0}.").format(
					self.name
				)
			)
		if not self.bank_account:
			frappe.throw(_("Please set the 'Bank Account' of Etsy Shop {0}.").format(self.name))

	def validate_payout_settings(self):
		if not self.payout_account:
			frappe.throw(
				_("Please set the 'Payout Account' in the Fee Settings of Etsy Shop {0}.").format(self.name)
			)
		if not self.bank_account:
			frappe.throw(_("Please set the 'Bank Account' of Etsy Shop {0}.").format(self.name))
		if self.payout_account == self.bank_account:
			frappe.throw(_("'Payout Account' and 'Bank Account' must be different accounts."))

	def get_fee_account(self, bucket: FeeBucket) -> str:
		"""Resolve the expense account for a fee bucket; everything falls back to the Etsy Fees account."""
		if bucket.classification.is_tax and self.fee_tax_account:
			return self.fee_tax_account

		category_accounts = {
			CATEGORY_FEES: self.fees_expense_account,
			CATEGORY_MARKETING: self.marketing_expense_account,
			CATEGORY_SHIPPING: self.shipping_expense_account,
			CATEGORY_OTHER: self.other_expense_account,
		}
		return category_accounts.get(bucket.classification.category) or self.fees_expense_account

	@staticmethod
	def fee_period(year: int, month: int) -> str:
		return f"{cint(year):04d}-{cint(month):02d}"

	@staticmethod
	def get_fee_journal_entry(etsy_shop: str, year: int, month: int) -> str | None:
		"""Return the name of an existing (draft or submitted) fee Journal Entry for this shop and month."""
		return frappe.db.get_value(
			"Journal Entry",
			{
				"etsy_shop": etsy_shop,
				"etsy_fee_period": EtsyShop.fee_period(year, month),
				"docstatus": ("<", 2),
			},
			"name",
		)

	@staticmethod
	def get_payout_journal_entry(entry_id: int | str) -> str | None:
		"""Return the name of an existing (draft or submitted) Journal Entry for this ledger entry."""
		return frappe.db.get_value(
			"Journal Entry",
			{"etsy_ledger_entry_id": cstr(entry_id), "docstatus": ("<", 2)},
			"name",
		)

	def fetch_ledger_entries(self, from_date, to_date, etsy_api: EtsyAPI | None = None) -> list[LedgerEntry]:
		"""
		Download all payment account ledger entries between two dates (inclusive).

		Etsy rejects any request whose window is longer than 31 days, so the period is split into
		chunks of at most ``LEDGER_MAX_WINDOW_DAYS``. The chunk size also keeps a whole day of slack,
		which a single calendar month would not have: a 31 day month spans 2678399 seconds, one
		second inside the limit, and a daylight saving change that sets the clocks back pushes it
		over.
		"""
		api = etsy_api or EtsyAPI(self)
		from_date, to_date = getdate(from_date), getdate(to_date)

		entries: list[LedgerEntry] = []
		window_start = from_date
		while window_start <= to_date:
			window_end = min(add_days(window_start, LEDGER_MAX_WINDOW_DAYS - 1), to_date)
			entries.extend(self.fetch_ledger_window(api, window_start, window_end))
			window_start = add_days(window_end, 1)

		return entries

	def fetch_ledger_window(self, api: EtsyAPI, from_date, to_date) -> list[LedgerEntry]:
		"""Download one window of at most 31 days. See ``fetch_ledger_entries``."""
		return list(
			fetch_all(
				lambda o: api.getShopPaymentAccountLedgerEntries(
					QP_getShopPaymentAccountLedgerEntries(
						shop_id=self.shop_id,
						min_created=int(
							frappe.utils.get_datetime(f"{getdate(from_date)} 00:00:00").timestamp()
						),
						max_created=int(
							frappe.utils.get_datetime(f"{getdate(to_date)} 23:59:59").timestamp()
						),
						limit=100,
						offset=o,
					)
				)
			)
		)

	def fetch_ledger_month(self, year: int, month: int, etsy_api: EtsyAPI | None = None) -> list[LedgerEntry]:
		first_day = get_first_day(f"{self.fee_period(year, month)}-01")
		return self.fetch_ledger_entries(first_day, get_last_day(first_day), etsy_api)

	def book_ledger_month(
		self,
		year: int,
		month: int,
		fees: bool = True,
		payouts: bool = True,
		etsy_api: EtsyAPI | None = None,
		ledger_entries: list[LedgerEntry] | None = None,
	) -> dict:
		"""
		Book the Etsy ledger of one month: the fee Journal Entry and one Journal Entry per payout.
		Returns ``{"fee_journal_entry": name | None, "payout_journal_entries": [names]}``.
		"""
		result = {"fee_journal_entry": None, "payout_journal_entries": []}
		if not (fees or payouts):
			return result

		entries = (
			ledger_entries if ledger_entries is not None else self.fetch_ledger_month(year, month, etsy_api)
		)

		if fees:
			result["fee_journal_entry"] = self.create_fee_journal_entry(year, month, ledger_entries=entries)
		if payouts:
			result["payout_journal_entries"] = self.create_payout_journal_entries(entries)
		return result

	def get_ledger_currency(self, entries: list[LedgerEntry]) -> str:
		currencies = {e.currency.upper() for e in entries if e.currency}
		if len(currencies) > 1:
			frappe.throw(
				_("Etsy ledger entries with mixed currencies are not supported: {0}").format(currencies)
			)
		return currencies.pop() if currencies else erpnext.get_company_currency(self.company)

	def create_fee_journal_entry(
		self,
		year: int,
		month: int,
		etsy_api: EtsyAPI | None = None,
		ledger_entries: list[LedgerEntry] | None = None,
	) -> str | None:
		"""
		Create one Journal Entry booking all Etsy fees of a month (listing, transaction & processing fees,
		marketing / ads, shipping labels, taxes on fees, ...) against the shop's bank account.

		Idempotent: if a Journal Entry for this shop and month already exists its name is returned.
		Returns ``None`` when the month contains no fee entries.
		"""
		self.validate_fee_settings()

		if existing := self.get_fee_journal_entry(self.name, year, month):
			return existing

		entries = (
			ledger_entries if ledger_entries is not None else self.fetch_ledger_month(year, month, etsy_api)
		)
		buckets = aggregate_fees(entries)
		if not buckets:
			return None

		ledger_currency = self.get_ledger_currency(entries)
		period = self.fee_period(year, month)
		posting_date = get_last_day(f"{period}-01")

		journal_entry: Document = frappe.new_doc("Journal Entry")
		journal_entry.voucher_type = "Journal Entry"
		journal_entry.company = self.company
		journal_entry.posting_date = posting_date
		journal_entry.title = f"Etsy Fees {period} - {self.name}"
		journal_entry.etsy_shop = self.name
		journal_entry.etsy_fee_period = period
		journal_entry.user_remark = self.get_fee_remark(period, buckets, ledger_currency)

		rows = [
			LedgerRow(self.get_fee_account(bucket), -bucket.total, format_breakdown(bucket, ledger_currency))
			for bucket in buckets
		]
		rows.append(
			LedgerRow(
				self.bank_account,
				flt(sum(bucket.total for bucket in buckets), 2),
				_("Etsy fees deducted from payment account"),
			)
		)
		self.append_ledger_rows(journal_entry, rows, ledger_currency, posting_date)

		journal_entry.flags.ignore_permissions = True
		journal_entry.insert(ignore_permissions=True)
		if self.fee_journal_entry_submit:
			journal_entry.submit()

		return journal_entry.name

	def get_fee_remark(self, period: str, buckets: list[FeeBucket], currency: str) -> str:
		lines = [f"Etsy fees for {period} ({self.name})"]
		lines.extend(f"{bucket.label}: {bucket.total:.2f} {currency}" for bucket in buckets)
		lines.append(f"Total: {sum(bucket.total for bucket in buckets):.2f} {currency}")
		return "\n".join(lines)

	def create_payout_journal_entries(self, ledger_entries: list[LedgerEntry]) -> list[str]:
		"""
		Create one Bank Entry per Etsy payout (deposit) moving the amount from the shop's bank
		(clearing) account to the payout account. Returned payouts are booked in reverse.

		Idempotent per ledger entry. Returns the names of the newly created Journal Entries.
		"""
		self.validate_payout_settings()

		created = []
		for entry in ledger_entries:
			if not is_payout(entry) or self.get_payout_journal_entry(entry.entry_id):
				continue
			created.append(self.create_payout_journal_entry(entry))
		return created

	def create_payout_journal_entry(self, entry: LedgerEntry) -> str:
		amount = ledger_amount(entry)  # negative = money left the Etsy balance
		ledger_currency = (entry.currency or erpnext.get_company_currency(self.company)).upper()
		posting_date = getdate(entry.created_timestamp)
		description = (entry.description or "Payout").strip()

		journal_entry: Document = frappe.new_doc("Journal Entry")
		journal_entry.voucher_type = "Bank Entry"
		journal_entry.company = self.company
		journal_entry.posting_date = posting_date
		journal_entry.cheque_no = f"Etsy {description} {entry.entry_id}"
		journal_entry.cheque_date = posting_date
		journal_entry.title = f"Etsy {description} {posting_date} - {self.name}"
		journal_entry.etsy_shop = self.name
		journal_entry.etsy_ledger_entry_id = cstr(entry.entry_id)
		journal_entry.user_remark = f"Etsy {description} of {abs(amount):.2f} {ledger_currency} ({self.name}), ledger entry {entry.entry_id}"

		rows = [
			LedgerRow(self.payout_account, -amount, f"Etsy {description} {entry.entry_id}"),
			LedgerRow(self.bank_account, amount, f"Etsy {description} {entry.entry_id}"),
		]
		self.append_ledger_rows(journal_entry, rows, ledger_currency, posting_date)

		journal_entry.flags.ignore_permissions = True
		journal_entry.insert(ignore_permissions=True)
		if self.payout_journal_entry_submit:
			journal_entry.submit()

		return journal_entry.name

	def append_ledger_rows(
		self, journal_entry: Document, rows: list[LedgerRow], ledger_currency: str, posting_date
	):
		"""
		Append balanced ledger currency rows to a Journal Entry, converting to the currency of each
		account (ledger or company currency) and absorbing rounding differences.
		"""
		company_currency = erpnext.get_company_currency(self.company)
		cost_center = self.fee_cost_center or erpnext.get_default_cost_center(self.company)

		exchange_rate = 1.0
		if ledger_currency != company_currency:
			exchange_rate = flt(get_exchange_rate(ledger_currency, company_currency, posting_date))
			if not exchange_rate:
				frappe.throw(
					_("No exchange rate found from {0} to {1} for {2}.").format(
						ledger_currency, company_currency, posting_date
					)
				)

		try:
			account_rows = build_journal_rows(
				rows,
				ledger_currency,
				company_currency,
				exchange_rate,
				account_currency_of=lambda account: frappe.get_cached_value(
					"Account", account, "account_currency"
				),
				rounding_account=frappe.get_cached_value(
					"Company", self.company, "exchange_gain_loss_account"
				),
			)
		except ValueError as e:
			frappe.throw(str(e))

		multi_currency = False
		for row in account_rows:
			row.pop("company_amount", None)
			multi_currency = multi_currency or row["account_currency"] != company_currency
			row["cost_center"] = cost_center
			journal_entry.append("accounts", row)
		journal_entry.multi_currency = int(multi_currency)


### background job entry points for enqueued imports
def run_import_listings(user, etsy_shop, listing_state="active", include_attributes=1, include_items=0):
	shop: EtsyShop = frappe.get_doc("Etsy Shop", etsy_shop)
	shop.import_listings(
		listing_state=listing_state, include_attributes=include_attributes, include_items=include_items
	)
	frappe.publish_realtime(
		"msgprint",
		{
			"message": _("Etsy listing import completed for {0}.").format(etsy_shop),
			"indicator": "green",
			"alert": True,
		},
		user=user,
	)


def run_import_receipts(user, etsy_shop, min_date=None, max_date=None):
	shop: EtsyShop = frappe.get_doc("Etsy Shop", etsy_shop)
	shop.import_receipts(min_date=min_date, max_date=max_date)
	frappe.publish_realtime(
		"msgprint",
		{
			"message": _("Etsy sales import completed for {0}.").format(etsy_shop),
			"indicator": "green",
			"alert": True,
		},
		user=user,
	)


def run_book_ledger(user, etsy_shop, from_date, to_date=None, fees=1, payouts=1):
	"""
	Book one Journal Entry per month in the period. Each month is committed on its own, so a failure
	part way through keeps the months already booked.
	"""
	shop: EtsyShop = frappe.get_doc("Etsy Shop", etsy_shop)
	fees, payouts = cint(fees), cint(payouts)
	months = months_between(from_date, to_date or from_date)

	created_fees: list[tuple[str, str]] = []
	empty_months: list[str] = []
	skipped: list[tuple[str, str]] = []
	payout_entries: list[str] = []
	failed: list[str] = []

	for year, month in months:
		period = shop.fee_period(year, month)
		existing = shop.get_fee_journal_entry(etsy_shop, year, month) if fees else None
		if existing:
			skipped.append((period, existing))

		try:
			result = shop.book_ledger_month(
				year, month, fees=bool(fees and not existing), payouts=bool(payouts)
			)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(f"Etsy: Failed to book ledger {period} for shop {etsy_shop}")
			failed.append(period)
			continue

		if fees and not existing:
			if result["fee_journal_entry"]:
				created_fees.append((period, result["fee_journal_entry"]))
			else:
				empty_months.append(period)
		payout_entries.extend(result["payout_journal_entries"])

	frappe.publish_realtime(
		"msgprint",
		{
			"message": book_ledger_summary(
				etsy_shop, months, created_fees, empty_months, skipped, payout_entries, failed, payouts
			),
			"indicator": "red" if failed else ("orange" if skipped else "green"),
			"alert": True,
		},
		user=user,
	)


def book_ledger_summary(
	etsy_shop, months, created_fees, empty_months, skipped, payout_entries, failed, payouts=1
) -> str:
	"""Readable result of a (possibly multi month) ledger booking."""

	def entry_links(entries, limit=12):
		links = [get_link_to_form("Journal Entry", name) for _, name in entries[:limit]]
		if len(entries) > limit:
			links.append(_("and {0} more").format(len(entries) - limit))
		return ", ".join(links)

	period = f"{months[0][0]:04d}-{months[0][1]:02d}"
	if len(months) > 1:
		period += f" to {months[-1][0]:04d}-{months[-1][1]:02d}"
	lines = [_("Etsy ledger {0} for {1}:").format(period, etsy_shop)]

	if created_fees:
		lines.append(
			_("{0} fee Journal Entries created: {1}").format(len(created_fees), entry_links(created_fees))
		)
	if empty_months:
		lines.append(_("No Etsy fees found for: {0}").format(", ".join(empty_months)))
	if skipped:
		lines.append(
			_("Already booked, left untouched: {0}").format(
				", ".join(f"{p} ({get_link_to_form('Journal Entry', n)})" for p, n in skipped[:12])
			)
		)
	if payout_entries:
		lines.append(
			_("{0} payout Journal Entries created: {1}").format(
				len(payout_entries),
				entry_links([(None, n) for n in payout_entries]),
			)
		)
	elif payouts and not failed:
		lines.append(_("No new Etsy payouts found."))
	if failed:
		lines.append(_("Failed (see Error Log): {0}").format(", ".join(failed)))

	return "<br>".join(lines)


### public functions
@frappe.whitelist(
	methods=["GET"], allow_guest=True
)  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
def callback(code: str | None = None, state: str | None = None):
	"""
	Handle client's code.

	Called during the oauthorization flow by the remote oAuth2 server to transmit
	a code that can be used by the local server to obtain an access token.
	"""
	if frappe.session.user == "Guest":
		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = "/login?" + urlencode({"redirect-to": frappe.request.url})
		return

	path = frappe.request.path[1:].split("/")
	if len(path) != 4 or not path[3]:
		frappe.throw(_("Invalid Parameters."))

	etsy_shop: EtsyShop = frappe.get_doc("Etsy Shop", unquote_plus(path[3]))

	if state != etsy_shop.token_state:
		frappe.throw(_("Invalid token state! Check if the token has been created by the OAuth flow."))

	oauth_session = etsy_shop.get_oauth2_session(init=True)
	token = oauth_session.fetch_token(
		TOKEN_URI,
		code=code,
		client_secret=etsy_shop.get_password("client_secret"),
		include_client_id=True,
		code_verifier=etsy_shop.code_verifier,
		**QUERY_PARAMS,
	)
	etsy_shop.token_update(token)

	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = etsy_shop.get_url()


@frappe.whitelist()
def has_token(etsy_shop: str) -> bool:
	shop: EtsyShop = frappe.get_doc("Etsy Shop", etsy_shop)
	return shop.token_exists()
