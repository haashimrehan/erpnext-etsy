# Limitations

Current limitations of the Etsy Integration.

## Read-Only Integration

- **No write-back**: Changes in ERPNext are not pushed to Etsy
- Inventory updates in ERPNext don't sync to Etsy
- Pricing changes don't update Etsy listings

## Refunds

- Refunds are not automatically processed
- You must manually create Credit Notes in ERPNext for refunded Etsy orders

## Order Updates

- Once a Sales Order is created, subsequent Etsy updates (e.g., address changes) are not synced
- You must manually update the Sales Order in ERPNext

## Stock Sync

- Stock levels are imported on initial listing import only
- No ongoing stock synchronization

## Shipping Labels

- Shipping labels from Etsy are not imported (their cost is booked by the monthly fee Journal Entry, see [Fees & Expenses](fees-and-expenses.md))
- Tracking numbers are not automatically synced to ERPNext

## Fees

- Fee entries are classified by their Etsy ledger type; unknown fee types are booked to the "Other" (or "Fees") category
- Fees are booked once per month, not per order

## Multiple Currencies

- Each Etsy Shop operates in one currency (as defined on Etsy)
- Multi-currency support depends on ERPNext configuration
- Currency conversion must be handled in ERPNext
