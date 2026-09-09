# Changelog

All notable changes to ShopPOS are listed here.
Newest version first.

Versions go 1.4 → 1.41 → 1.42 … 1.49 → 1.5

## 1.49 — 2026-09-10

- Language dropdown on login can be opened (scanner focus no longer closes it)

## 1.48 — 2026-09-10

- Language picker: English + Bahasa Melayu first
- Also Indonesian, Chinese, Tamil, Hindi, Thai, Vietnamese, Filipino, Arabic, Urdu, Spanish, French, German, Portuguese, Italian, Russian, Turkish, Japanese, Korean, Bengali
- Product names stay as typed. Dedication stays in English.

## 1.47 — 2026-09-10

- Update URL is built in. Shop staff do not need GitHub.
- start-shoppos.bat — double-click to open the till
- FOR-SHOP-OWNER.txt — plain install steps

## 1.46 — 2026-09-10

- Permanent footer: Pendekar's App — Blogs can die.. idea lives on.
- Same line on printed receipts
- LICENSE requires that dedication to stay in copies and forks

## 1.45 — 2026-09-10

- Receipt header is editable: shop name, phone, address (Settings → Receipt)

## 1.44 — 2026-09-10

- Receipt prints after Charge (58mm thermal or A4). Shop name + paper in Settings
- Reprint receipt from History
- Search box stays focused on Sell for a barcode scanner
- Hold parks a cart; Resume brings it back
- Void sale (admin/manager) returns stock
- End of day: expected cash vs counted, save + print summary
- Settings: Download backup of shoppos.db (save onto USB)

## 1.43 — 2026-09-10

- Change your own password (Password tab, also on People for admin)
- Admin can delete a login (not the account you are using, not the last admin)
- Admin can reset another person's password

## 1.42 — 2026-09-10

- Login with Staff ID + password
- Roles: admin, manager, staff
- Staff: Sell only
- Manager: Sell + Stock + History
- Admin: everything + Settings + People (create logins)
- History shows which cashier made the sale
- First login: ID `admin` / password `admin` — create a new admin and stop using this

## 1.41 — 2026-09-10

- Click a product in Stock to open its own page
- Edit name, barcode, price, cost, stock on that page
- Print a label with barcode bars + number + price
- If no barcode number is set, label uses P000123 from the product id

## 1.4 — 2026-09-10

- Settings shows this changelog (What changed)
- Changelog is read from CHANGELOG.md in the GitHub repo

## 1.3 — 2026-09-10

- Stock tab: **Edit** a product (fix name, barcode, price, cost)
- Stock tab: **Delete** a product (with confirm)
- Past sales keep the product name after delete
- Opening stock field hides while editing (qty still uses +1 / −1)

## 1.2 — 2026-09-10

- Settings tab
- Save a GitHub raw URL
- **Check and update** downloads the latest `shoppos.py`
- Stock tab no longer adds items to the cart (Sell and Stock are separate)

## 1.1 — 2026-09-10

- First shop build
- Sell, Stock, History
- Cash checkout and stock deduction
- Sample products on first run
