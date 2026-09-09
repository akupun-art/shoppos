# shoppos
post system
ShopPOS
Simple stock + point of sale for a small shop.  
Runs on one Windows PC in the browser. No monthly fee.
Sell — tap products, take cash, stock drops automatically
Stock — add items, see qty and price, +1 / −1 to adjust
History — recent sales
Settings — paste a GitHub raw URL and click update
Data is stored next to the program in `shoppos.db`. Updates replace `shoppos.py` only.
Requirements
Windows 10/11 (Mac/Linux also work)
Python 3.8 or newer from https://www.python.org/downloads/  
On Windows, tick Add python.exe to PATH when installing.
How to run
Put `shoppos.py` in a folder, for example `C:\shoppos\`
Open that folder in File Explorer
Click the address bar, type `cmd`, press Enter
Run:
```text
py shoppos.py
```
If that fails:
```text
python shoppos.py
```
Leave that window open
Open Chrome or Firefox:
```text
http://127.0.0.1:8765
```
Closing the black window stops the till.
Shortcut (optional)
Create `start-shoppos.bat` in the same folder:
```bat
@echo off
cd /d "%~dp0"
start "" py shoppos.py
timeout /t 2 >nul
start http://127.0.0.1:8765
```
Double-click the `.bat` to start.
First use
Open Stock
Add your products (name, barcode optional, sell price, opening stock)
Open Sell, tap items into the cart, enter cash, press Charge
Sample products (Coffee, Sandwich, Water) are created only on the first run. You can ignore them or reduce their stock to 0.
Updates from GitHub
Upload the latest `shoppos.py` to this public repo
Open the file on GitHub → click Raw → copy the URL  
Example:
`https://raw.githubusercontent.com/YOUR_USERNAME/shoppos/main/shoppos.py`
In ShopPOS go to Settings, paste the URL, Save URL, then Check and update
Close the Python window and start `shoppos.py` again
Do not delete `shoppos.db` when you update.
Backup
Copy `shoppos.db` to a USB drive or Google Drive. That file is your products and sales.
Notes
One PC, one shop. Not a phone APK.
Cash drawer / receipt printer are not included yet.
Keep the shop PC’s Python window running while you sell.
License
Use freely in your shop.
