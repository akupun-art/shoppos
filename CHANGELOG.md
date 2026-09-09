# Changelog

All notable changes to ShopPOS are listed here.
Newest version first.

Versions go 1.4 → 1.41 → 1.42 … 1.49 → 1.5

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
