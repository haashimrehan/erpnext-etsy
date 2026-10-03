import frappe
from frappe.model.document import Document

RECEIPTS_SCHEDULED_JOB_TYPE_METHOD = "etsy.api.synchronise_receipts"
LISTINGS_SCHEDULED_JOB_TYPE_METHOD = "etsy.api.synchronise_listings"
FEES_SCHEDULED_JOB_TYPE_METHOD = "etsy.api.synchronise_fees"
PAYOUTS_SCHEDULED_JOB_TYPE_METHOD = "etsy.api.synchronise_payouts"
FEES_JOB_HOUR = 3  # hour of the day at which the monthly fee Journal Entry job runs
PAYOUTS_JOB_HOUR = 4  # hour of the day at which the payout Journal Entry job runs

# (link field on Etsy Settings, method the job runs)
JOBS = (
	("sales_order_scheduler_link", RECEIPTS_SCHEDULED_JOB_TYPE_METHOD),
	("item_scheduler_link", LISTINGS_SCHEDULED_JOB_TYPE_METHOD),
	("fee_scheduler_link", FEES_SCHEDULED_JOB_TYPE_METHOD),
	("payout_scheduler_link", PAYOUTS_SCHEDULED_JOB_TYPE_METHOD),
)


class EtsySettings(Document):
	@property
	def sales_order_last_sync(self):
		return self.get_scheduler(self.sales_order_scheduler_link).last_execution

	@property
	def sales_order_next_sync(self):
		return self.get_scheduler(self.sales_order_scheduler_link).next_execution

	@property
	def item_last_sync(self):
		return self.get_scheduler(self.item_scheduler_link).last_execution

	@property
	def item_next_sync(self):
		return self.get_scheduler(self.item_scheduler_link).next_execution

	@property
	def fee_last_sync(self):
		return self.get_scheduler(self.fee_scheduler_link).last_execution

	@property
	def fee_next_sync(self):
		return self.get_scheduler(self.fee_scheduler_link).next_execution

	@property
	def payout_last_sync(self):
		return self.get_scheduler(self.payout_scheduler_link).last_execution

	@property
	def payout_next_sync(self):
		return self.get_scheduler(self.payout_scheduler_link).next_execution

	def before_save(self):
		### payouts
		payout_interval = min(max(0, self.payout_sync_interval or 0), 30)
		self.payout_sync_interval = payout_interval
		self.configure_job(
			"payout_scheduler_link",
			PAYOUTS_SCHEDULED_JOB_TYPE_METHOD,
			f"0 {PAYOUTS_JOB_HOUR} */{max(1, payout_interval)} * *",  # runs every X days
			payout_interval,
		)

		### fees
		fee_day = min(max(0, self.fee_journal_day or 0), 28)
		self.fee_journal_day = fee_day
		self.configure_job(
			"fee_scheduler_link",
			FEES_SCHEDULED_JOB_TYPE_METHOD,
			f"0 {FEES_JOB_HOUR} {max(1, fee_day)} * *",  # runs monthly on day X
			fee_day,
		)

		### receipts
		sales_order_interval = min(max(0, self.sales_order_sync_interval or 0), 60)
		self.sales_order_sync_interval = sales_order_interval
		self.configure_job(
			"sales_order_scheduler_link",
			RECEIPTS_SCHEDULED_JOB_TYPE_METHOD,
			f"*/{max(1, sales_order_interval)} * * * *",  # runs every X minutes
			sales_order_interval,
		)

		### listings
		item_interval = min(max(0, self.item_sync_interval or 0), 24)
		self.item_sync_interval = item_interval
		self.configure_job(
			"item_scheduler_link",
			LISTINGS_SCHEDULED_JOB_TYPE_METHOD,
			f"0 */{max(1, item_interval)} * * *",  # runs every X hours
			item_interval,
		)

	def configure_job(self, link_field, method, cron_format, setting):
		"Create/update the Scheduled Job Type behind `link_field`; a setting of 0 stops it."
		job = self.get_scheduler(self.get(link_field))
		job.method = method
		job.frequency = "Cron"
		job.cron_format = cron_format
		job.stopped = 1 - (self.etsy_enabled * min(1, setting))
		# Tag the job with a Scheduler Event. Jobs that are neither in hooks.py nor tagged
		# like this get deleted by `bench migrate` (sync_jobs -> clear_events).
		job.scheduler_event = get_scheduler_event(method)
		job.save(ignore_permissions=True)
		self.set(link_field, job.name)

	def get_scheduler(self, scheduler_link):
		"Return `Scheduled Job Type` if it exists, else create a new one"
		if scheduler_link and frappe.db.exists("Scheduled Job Type", scheduler_link):
			return frappe.get_doc("Scheduled Job Type", scheduler_link)
		else:
			return frappe.new_doc("Scheduled Job Type")


def get_scheduler_event(method):
	"Return the Scheduler Event for `method`, creating it if needed."
	filters = {"scheduled_against": "Etsy Settings", "method": method}
	if name := frappe.db.get_value("Scheduler Event", filters):
		return name
	event = frappe.get_doc({"doctype": "Scheduler Event", **filters})
	event.insert(ignore_permissions=True)
	return event.name