# Fees & Expenses

Etsy deducts its fees directly from your payment account balance: listing fees, transaction fees, payment processing fees, Etsy Ads, Offsite Ads, postage labels, and the VAT charged on those fees. None of these appear on a Sales Order, so without further action your ERPNext books would show the full sales amount but never the cost of selling on Etsy.

The integration closes this gap with a **monthly Journal Entry** per Etsy Shop that books all fees of a month as expenses against the shop's Bank Account (the account that receives your Etsy payments).

## How It Works

1. The app downloads all entries of the Etsy **payment account ledger** for the month (`getShopPaymentAccountLedgerEntries`). Etsy refuses any request covering more than 31 days, so longer periods are split into several requests automatically.
2. Entries that only move money are ignored. They are not costs, and booking them would both invent an expense and break the reconciliation of the clearing account:

    | Ledger type | What it is |
    |-------------|------------|
    | `PAYMENT_GROSS` | A buyer's payment arriving on the Etsy balance |
    | `DISBURSE2` | A payout to your bank. Booked separately, see [Payouts](#payouts) |
    | `sales_tax` | Tax the buyer paid, which Etsy collects and remits itself |
    | `billing_payment` | You settling an Etsy bill by card |
    | `recoupment` | Etsy charging the card on file for a negative balance |

3. The remaining entries are classified by their **ledger type** and netted per category:

    | Category | Ledger types |
    |----------|--------------|
    | **Fees** | `transaction`, `transaction_quantity`, `PAYMENT_PROCESSING_FEE`, `regulatory_operating_fee`, `shipping_transaction`, `listing`, `renew_sold`, `renew_sold_auto`, `auto_renew_expired`, subscriptions |
    | **Marketing** | `prolist` (Etsy Ads), `offsite_ads_fee` |
    | **Shipping** | `shipping_label`, postage bought through Etsy |
    | **Other** | Anything that cannot be classified |
    | **Tax on ...** | `vat_seller_services`, `vat_on_processing_fees`, ... - the tax follows the category of the fee it belongs to |

!!! note "Etsy returns machine tokens, not prose"
    In the live API the `description` of a ledger entry carries the same token as its `ledger_type`, for example `prolist` rather than "Etsy Ads". Classification therefore keys on the ledger type. The description is only used as a fallback for shops that do return readable text.

4. One Journal Entry is created with one row per category (and per tax category) debiting the configured expense accounts, and one row crediting the shop's **Bank Account** for the total.

    ![Fee Journal Entry](../images/fees-journal-entry.png)

    <!-- IMAGE: Screenshot of a Journal Entry "Etsy Fees 2026-08 - My Shop" with rows: Etsy Fees Expense (debit), Marketing Expense (debit), Tax on Seller Fees (debit), Etsy Payments bank account (credit); the "Etsy Shop" and "Etsy Fee Period" fields visible at the top -->

Credits from Etsy (e.g. a refunded transaction fee after you refund a buyer) reduce the category total. If a category nets to a credit, the expense account is credited instead.

Each Journal Entry row carries a remark with the breakdown by ledger description, for example:

```
Etsy Fees (41 entries) - transaction: -23.52 USD; PAYMENT_PROCESSING_FEE: -16.44 USD; shipping_transaction: -6.74 USD; regulatory_operating_fee: -2.33 USD
```

The Journal Entry is tagged with the **Etsy Shop** and the **Etsy Fee Period** (`YYYY-MM`). Only one Journal Entry (draft or submitted) can exist per shop and month, so running the job twice never duplicates bookings. Cancel the Journal Entry to allow it to be regenerated.

## Configuration

Open the **Etsy Shop** and expand the **Fee & Payout Settings (Journal Entries)** section.

| Field | Required | Description |
|-------|----------|-------------|
| **Etsy Fees Expense Account** | Yes | Listing, transaction, processing and regulatory operating fees. Also the fallback for every other category. Without this account no fee Journal Entry is created for the shop. |
| **Marketing Expense Account** | No | Etsy Ads and Offsite Ads. |
| **Shipping Label Expense Account** | No | Postage labels bought through Etsy. |
| **Other Expense Account** | No | Unclassified charges. |
| **Tax on Seller Fees Account** | No | VAT / tax charged on fees. Leave empty to book the tax to the same expense account as the fee. Set this to an input VAT account if you can reclaim the VAT. |
| **Cost Center for fees** | No | Defaults to the company's default Cost Center. |
| **Submit Journal Entry automatically** | No | Unchecked: the Journal Entry is saved as a **draft** for review. Checked: it is submitted immediately. |

The credit side always uses the shop's **Bank Account** from the *Sales Order & Invoice Settings* - the same account that Payment Entries for Etsy orders are posted to, so the account balance mirrors your Etsy payment account.

!!! tip "Reconciliation"
    After the fee Journal Entry is booked, the balance of the Bank Account should match your Etsy payment account balance (sales in, fees and deposits out). Deposits to your real bank account can then be recorded as a simple Bank Entry between the two accounts.

### Currencies

Fees are booked in the currency of the Etsy ledger. If your company currency differs, the Journal Entry is created as a multi-currency entry using the ERPNext exchange rate of the posting date (the last day of the month). Make sure a **Currency Exchange** record exists, or that automatic exchange rate fetching is enabled in Accounts Settings.

## Automatic Monthly Booking

In **Etsy Settings**, section **Fees - Monthly Journal Entry**:

| Field | Range | Default | Description |
|-------|-------|---------|-------------|
| **Day of Month** | 1-28 | 1 | Day on which the Journal Entry for the **previous** month is created (at 03:00 server time). Set to `0` to disable. |
| **Last Run / Next Run** | Read-only | - | Timestamps of the Scheduled Job Type. |
| **Scheduler Link** | Read-only | - | Link to the Scheduled Job Type (`etsy.api.synchronise_fees`). |

The job runs for every Etsy Shop that is **Connected** and has an **Etsy Fees Expense Account** configured. Errors are logged per shop in the Error Log without affecting other shops.

!!! info "Timing"
    Etsy can still post fees for a month (e.g. the ads bill for the last day) on the first day of the following month. Running on day 2 or later avoids missing them.

## Payouts

Etsy periodically pays the balance of your payment account out to your bank. In the ledger these appear as `DISBURSE2` entries. When a **Payout Account** is configured on the Etsy Shop, every payout is booked as its own **Bank Entry**:

| Account | Debit | Credit |
|---------|-------|--------|
| Payout Account (e.g. Wise USD) | payout amount | |
| Bank Account (Etsy clearing) | | payout amount |

- The posting date is the date of the ledger entry, and the reference number contains the Etsy ledger entry id, so each entry matches one transaction on your bank statement for **Bank Reconciliation**.
- In the ledger a payout carries the `DISBURSE2` type. A payout your bank returns to Etsy arrives as a positive entry of the same type and is booked in reverse.
- Each ledger entry is booked at most once, tracked by the **Etsy Ledger Entry ID** field on the Journal Entry.
- **Submit Payout Journal Entries automatically** controls whether the entries are submitted or left as drafts.

| Field (Etsy Shop) | Required | Description |
|-------------------|----------|-------------|
| **Payout Account** | No | Bank account receiving Etsy payouts. Leave empty to skip payout booking. Must differ from the Bank Account. |
| **Submit Payout Journal Entries automatically** | No | Default off. |

### Payout schedule

In **Etsy Settings**, section **Payouts - Journal Entries**, the **Sync Interval** (1 to 30 days, `0` disables) runs `etsy.api.synchronise_payouts`, which downloads the last 35 days of the ledger and books any payout that has not been booked yet. The lookback is deliberately longer than the largest sync interval so a missed run leaves no gap. The monthly fee job books the previous month's payouts as well, from the same ledger download.

!!! tip "Negative balances"
    When your Etsy balance is negative, Etsy charges the card on file (a *recoupment*). These are not booked automatically because the card account is unknown to the integration. Book them manually as a Bank Entry from the card's account to the Etsy clearing account.

## Manual Booking

On a connected **Etsy Shop**, use **Create > Etsy Fees & Payouts**, pick a **From Month** and a **To Month**, choose whether to book fees, payouts or both, and confirm. Any date inside a month selects that whole month. The entries are created in the background and a notification with links appears when done.

**One Journal Entry is created per month**, dated on the last day of that month, even when you book a range in one go. Booking several months into a single entry would post January's costs with a December date, which misstates every monthly and quarterly report in between, so the app does not offer it.

Months that already have a fee Journal Entry are left untouched and named in the result, so re-running a range is safe. At most 36 months can be booked in one action.

### Starting part way through your Etsy history

If your ERPNext books start later than your Etsy shop, nothing needs to be excluded: no scheduled job ever reaches further back than the previous month, and the ledger is only ever downloaded for the months you ask for.

To start from a given month:

1. Find your Etsy balance on the day before your first month. The **Balance** column of the Etsy payment account, or the `balance` field on the last ledger entry of that day, gives it.
2. Book that as an opening balance on the Etsy clearing account, dated the first day of your first month. Without it the clearing account carries a permanent offset, because Etsy pays out money earned before your books began.
3. Use **Create > Etsy Fees & Payouts** with the full range, for example January to August, and let it create one entry per month.

!!! warning "Card payments are not booked"
    If Etsy charges a card on file to settle a negative balance, that entry (`billing_payment`, or a `recoupment`) is not booked, because the integration does not know which account the money came from. In a month with no sales this can be the only inflow, so the clearing account drifts by that amount. Book those manually as a Bank Entry from the card's account to the clearing account.

## Multi-currency example

A Canadian seller with a USD shop, a USD Etsy clearing account, a USD Wise account and CAD books gets:

- **Fee Journal Entry**: expense and HST rows in CAD (converted at the month-end rate), one credit row on the USD clearing account in USD. Any cent of rounding from converting rows separately is absorbed into the largest CAD row so the entry balances.
- **Payout Bank Entries**: both rows in USD with the exchange rate of the payout date. The CAD value of the USD accounts drifts with the rate; use ERPNext's *Exchange Rate Revaluation* for that.

## Limitations

- Classification is based on the ledger types Etsy provides. New or unusual fee types land in **Other** (or in **Fees** when they are recognised as a tax). Review draft Journal Entries before submitting them, and check whether the **Other** row is empty: on a shop whose ledger is fully understood it should be.
- Refunds to buyers are not booked (see [Limitations](limitations.md)); only the fee credits Etsy grants on refunds are included.
- Recoupments (card charges for negative balances) and card payments of Etsy bills are not booked.
- Sales tax that Etsy collects from buyers and remits itself is not booked. It is not your revenue or your cost, and the receipt import already leaves it off the Sales Order.
- Amounts are taken from the ledger in the smallest currency unit (cents).
