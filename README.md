# Etsy Integration for ERPNext

> [!IMPORTANT]
> The term 'Etsy' is a trademark of Etsy, Inc. This application uses the Etsy API but is not endorsed or certified by Etsy, Inc.

[![CI version-15](https://img.shields.io/github/actions/workflow/status/maeurerdev/erpnext-etsy/ci.yml?branch=version-15&label=version-15)](https://github.com/maeurerdev/erpnext-etsy/actions/workflows/ci.yml?query=branch%3Aversion-15)
[![CI version-16](https://img.shields.io/github/actions/workflow/status/maeurerdev/erpnext-etsy/ci.yml?branch=version-16&label=version-16)](https://github.com/maeurerdev/erpnext-etsy/actions/workflows/ci.yml?query=branch%3Aversion-16)

**📖 [Read the full documentation](https://maeurerdev.github.io/erpnext-etsy)**


## 🌟 Features
- ✅ **Sales Order Synchronization**  
  Automatically pulls new orders from Etsy into ERPNext as **Sales Orders**, including:  
  - Customer creation / matching
  - Shipping & billing addresses
  - Line items with correct variants & pricing
  - Taxes, shipping charges
  - **Sales Invoice** and **Payment Entry** generation
- ✅ **Etsy Listing Import**  
  Automatically retrieves missing item details (title, description, images, attributes, variants, pricing, etc.) directly from your Etsy shop to populate or complete records in ERPNext - The **Etsy Listing** doctype allows configuration and management on a per listing level.
- ✅ **Import Sales History**  
  Bulk import historical Etsy orders (back to a chosen date) to bring your ERPNext records up to date quickly — ideal during initial setup or after a period of disconnection.
- ✅ **Fees & Payouts as Journal Entries**  
  Books all Etsy fees of a month (listing, transaction & processing fees, Etsy Ads / Offsite Ads, postage labels, tax on fees) from the Etsy payment account ledger as one **Journal Entry** per shop — split across configurable expense accounts and credited against the shop's Etsy clearing account. Every Etsy payout becomes a **Bank Entry** from the clearing account to your payout bank account, ready for bank reconciliation. Multi-currency aware (e.g. USD shop, CAD books). Runs automatically or on demand.
- ✅ **Multi-Shop Support**  
  Connect and manage **multiple** Etsy shops from a single ERPNext instance. Each shop can have its own configuration, credentials, and settings.
- ✅ **Configurable Sync Scheduling**  
  Run synchronization manually, on a schedule (via ERPNext Scheduler).
- ✅ **Secure OAuth Authentication**  
  Uses Etsy's OAuth 2.0 flow for safe, token-based access (personal access token required).


## 🚀 Getting Started

### 🔧 Installation
You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/maeurerdev/erpnext-etsy
bench --site $SITE_NAME install-app etsy
```

### ⚙️ Configuration 
- Create a new **Etsy Shop**
  - Setup a new [Personal App](https://www.etsy.com/developers/your-apps) on the Etsy Website
  - Copy the redirect URL from ERP and paste it under the Personal App Settings
  - Enter your Etsy API Keystring and Shared Secret to ERP Etsy Shop API section
  - Click the Login Button to start OAuth2 flow
- Go to **Etsy Settings** to enable and configure automatic sync
- Optional: set the expense accounts and the payout account in the **Fee & Payout Settings** section of the Etsy Shop to have Etsy fees and payouts booked as Journal Entries