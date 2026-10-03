import frappe

from etsy.etsy.doctype.etsy_settings.etsy_settings import JOBS, get_scheduler_event


def execute():
	"""Tag existing Etsy jobs with a Scheduler Event so `bench migrate` stops deleting them.

	Runs before sync_jobs in the same migrate, so already-deployed sites upgrade cleanly.
	"""
	for link_field, method in JOBS:
		job = frappe.db.get_single_value("Etsy Settings", link_field)
		if job and frappe.db.exists("Scheduled Job Type", job):
			frappe.db.set_value(
				"Scheduled Job Type", job, "scheduler_event", get_scheduler_event(method), update_modified=False
			)