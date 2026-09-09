#!/usr/bin/env python3
"""
ShopPOS — simple local stock + point of sale.
No extra packages. Python 3.8+ standard library only.

Run:  python3 shoppos.py
Open: http://127.0.0.1:8765
"""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

APP_VERSION = "1.2"
ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "shoppos.db"
CONFIG_PATH = ROOT / "shoppos-config.json"
HOST = "127.0.0.1"
PORT = 8765


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db() -> None:
    con = connect()
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            barcode TEXT UNIQUE,
            price REAL NOT NULL DEFAULT 0,
            cost REAL NOT NULL DEFAULT 0,
            stock REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            total REAL NOT NULL,
            paid REAL NOT NULL,
            change_amt REAL NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sale_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
            product_id INTEGER REFERENCES products(id),
            name TEXT NOT NULL,
            qty REAL NOT NULL,
            price REAL NOT NULL,
            line_total REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stock_moves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
            qty REAL NOT NULL,
            reason TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    n = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    if n == 0:
        samples = [
            ("Water 600ml", "1001", 1.50, 0.60, 24),
            ("Coffee", "1002", 3.00, 1.00, 20),
            ("Sandwich", "1003", 5.50, 2.20, 10),
        ]
        for name, barcode, price, cost, stock in samples:
            con.execute(
                "INSERT INTO products (name, barcode, price, cost, stock, created_at) VALUES (?,?,?,?,?,?)",
                (name, barcode, price, cost, stock, now_iso()),
            )
    con.commit()
    con.close()


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _send(self, code: int, body, content_type="application/json"):
        data = body if isinstance(body, (bytes, bytearray)) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, HTML, "text/html; charset=utf-8")
            return
        if path == "/api/products":
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0].strip()
            con = connect()
            if q:
                like = f"%{q}%"
                cur = con.execute(
                    """SELECT * FROM products
                       WHERE name LIKE ? OR IFNULL(barcode,'') LIKE ?
                       ORDER BY name COLLATE NOCASE""",
                    (like, like),
                )
            else:
                cur = con.execute("SELECT * FROM products ORDER BY name COLLATE NOCASE")
            data = rows(cur)
            con.close()
            self._json(data)
            return
        if path == "/api/sales":
            con = connect()
            sales = rows(con.execute("SELECT * FROM sales ORDER BY id DESC LIMIT 100"))
            for s in sales:
                s["items"] = rows(
                    con.execute("SELECT * FROM sale_items WHERE sale_id=?", (s["id"],))
                )
            con.close()
            self._json(sales)
            return
        if path == "/api/meta":
            cfg = load_config()
            self._json(
                {
                    "version": APP_VERSION,
                    "update_url": cfg.get("update_url", ""),
                }
            )
            return
        if path == "/api/summary":
            con = connect()
            today = datetime.now().strftime("%Y-%m-%d")
            sold = con.execute(
                "SELECT IFNULL(SUM(total),0) AS t, COUNT(*) AS n FROM sales WHERE created_at LIKE ?",
                (today + "%",),
            ).fetchone()
            low = rows(con.execute("SELECT * FROM products WHERE stock <= 5 ORDER BY stock, name"))
            count = con.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"]
            con.close()
            self._json(
                {
                    "today_total": sold["t"],
                    "today_count": sold["n"],
                    "product_count": count,
                    "low_stock": low,
                }
            )
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
        except json.JSONDecodeError:
            self._json({"error": "invalid json"}, 400)
            return

        if path == "/api/config":
            url = (payload.get("update_url") or "").strip()
            cfg = load_config()
            cfg["update_url"] = url
            save_config(cfg)
            self._json({"ok": True, "update_url": url})
            return

        if path == "/api/update":
            cfg = load_config()
            url = (payload.get("update_url") or cfg.get("update_url") or "").strip()
            if not url:
                self._json({"error": "Save a GitHub raw URL first"}, 400)
                return
            if "github.com" in url and "/blob/" in url:
                url = url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "ShopPOS"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = resp.read()
            except Exception as e:
                self._json({"error": "Download failed: " + str(e)}, 400)
                return
            text = data.decode("utf-8", errors="replace")
            if "ShopPOS" not in text or "def main" not in text:
                self._json({"error": "That file does not look like ShopPOS"}, 400)
                return
            target = Path(__file__).resolve()
            target.write_bytes(data)
            self._json({"ok": True, "message": "Updated. Close the Python window and run py shoppos.py again."})
            return

        if path == "/api/products":
            name = (payload.get("name") or "").strip()
            if not name:
                self._json({"error": "Name is required"}, 400)
                return
            barcode = (payload.get("barcode") or "").strip() or None
            try:
                price = float(payload.get("price") or 0)
                cost = float(payload.get("cost") or 0)
                stock = float(payload.get("stock") or 0)
            except (TypeError, ValueError):
                self._json({"error": "Price / cost / stock must be numbers"}, 400)
                return
            con = connect()
            try:
                cur = con.execute(
                    """INSERT INTO products (name, barcode, price, cost, stock, created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (name, barcode, price, cost, stock, now_iso()),
                )
                if stock:
                    con.execute(
                        """INSERT INTO stock_moves (product_id, qty, reason, note, created_at)
                           VALUES (?,?,?,?,?)""",
                        (cur.lastrowid, stock, "adjust", "opening stock", now_iso()),
                    )
                con.commit()
                row = dict(con.execute("SELECT * FROM products WHERE id=?", (cur.lastrowid,)).fetchone())
            except sqlite3.IntegrityError:
                con.close()
                self._json({"error": "Barcode already used"}, 400)
                return
            con.close()
            self._json(row, 201)
            return

        if path.startswith("/api/products/") and path.endswith("/adjust"):
            pid = int(path.split("/")[3])
            try:
                qty = float(payload.get("qty") or 0)
            except (TypeError, ValueError):
                self._json({"error": "qty must be a number"}, 400)
                return
            reason = (payload.get("reason") or "adjust").strip()
            note = (payload.get("note") or "").strip()
            con = connect()
            p = con.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
            if not p:
                con.close()
                self._json({"error": "Product not found"}, 404)
                return
            new_stock = p["stock"] + qty
            if new_stock < 0:
                con.close()
                self._json({"error": "Not enough stock"}, 400)
                return
            con.execute("UPDATE products SET stock=? WHERE id=?", (new_stock, pid))
            con.execute(
                """INSERT INTO stock_moves (product_id, qty, reason, note, created_at)
                   VALUES (?,?,?,?,?)""",
                (pid, qty, reason, note, now_iso()),
            )
            con.commit()
            row = dict(con.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone())
            con.close()
            self._json(row)
            return

        if path == "/api/checkout":
            items = payload.get("items") or []
            if not items:
                self._json({"error": "Cart is empty"}, 400)
                return
            try:
                paid = float(payload.get("paid") or 0)
            except (TypeError, ValueError):
                self._json({"error": "Paid must be a number"}, 400)
                return
            con = connect()
            try:
                prepared = []
                total = 0.0
                for it in items:
                    pid = int(it["product_id"])
                    qty = float(it["qty"])
                    if qty <= 0:
                        raise ValueError("qty")
                    p = con.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
                    if not p:
                        raise ValueError(f"Missing product {pid}")
                    if p["stock"] < qty:
                        con.close()
                        self._json({"error": f"Not enough stock for {p['name']}"}, 400)
                        return
                    line = round(p["price"] * qty, 2)
                    total += line
                    prepared.append((p, qty, line))
                total = round(total, 2)
                if paid < total:
                    con.close()
                    self._json({"error": f"Paid is less than total ({total:.2f})"}, 400)
                    return
                change = round(paid - total, 2)
                cur = con.execute(
                    """INSERT INTO sales (total, paid, change_amt, note, created_at)
                       VALUES (?,?,?,?,?)""",
                    (total, paid, change, payload.get("note") or "", now_iso()),
                )
                sid = cur.lastrowid
                for p, qty, line in prepared:
                    con.execute(
                        """INSERT INTO sale_items (sale_id, product_id, name, qty, price, line_total)
                           VALUES (?,?,?,?,?,?)""",
                        (sid, p["id"], p["name"], qty, p["price"], line),
                    )
                    con.execute("UPDATE products SET stock = stock - ? WHERE id=?", (qty, p["id"]))
                    con.execute(
                        """INSERT INTO stock_moves (product_id, qty, reason, note, created_at)
                           VALUES (?,?,?,?,?)""",
                        (p["id"], -qty, "sale", f"sale #{sid}", now_iso()),
                    )
                con.commit()
                sale = dict(con.execute("SELECT * FROM sales WHERE id=?", (sid,)).fetchone())
                sale["items"] = rows(con.execute("SELECT * FROM sale_items WHERE sale_id=?", (sid,)))
            except Exception as e:
                con.rollback()
                con.close()
                self._json({"error": str(e)}, 400)
                return
            con.close()
            self._json(sale, 201)
            return

        self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/products/"):
            pid = int(path.split("/")[3])
            con = connect()
            con.execute("DELETE FROM products WHERE id=?", (pid,))
            con.commit()
            con.close()
            self._json({"ok": True})
            return
        self._json({"error": "not found"}, 404)


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>ShopPOS</title>
<style>
  :root {
    --bg: #f3f4f6; --card:#fff; --ink:#111827; --muted:#6b7280;
    --line:#e5e7eb; --brand:#2563eb; --brand2:#1d4ed8;
    --good:#059669; --bad:#dc2626; --warn:#d97706;
  }
  * { box-sizing: border-box; }
  body { margin:0; font: 15px/1.4 system-ui, Segoe UI, sans-serif; background:var(--bg); color:var(--ink); }
  header { background:#111827; color:#fff; padding:12px 18px; display:flex; gap:12px; align-items:center; }
  header h1 { font-size:18px; margin:0; letter-spacing:.02em; }
  header .stats { margin-left:auto; font-size:13px; color:#d1d5db; }
  nav { display:flex; gap:6px; }
  nav button { background:#1f2937; color:#fff; border:0; padding:8px 12px; border-radius:8px; cursor:pointer; }
  nav button.active { background:var(--brand); }
  main { max-width:1100px; margin:16px auto; padding:0 14px 40px; }
  section[hidden] { display: none !important; }
  .grid { display:grid; grid-template-columns: 1.3fr .9fr; gap:14px; }
  @media (max-width:900px){ .grid { grid-template-columns:1fr; } }
  .card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:14px; }
  h2 { margin:0 0 10px; font-size:16px; }
  input, button, select { font:inherit; }
  input { width:100%; padding:10px 12px; border:1px solid var(--line); border-radius:10px; }
  .row { display:flex; gap:8px; margin-bottom:8px; }
  .list { max-height:420px; overflow:auto; }
  .item { display:flex; gap:10px; align-items:center; padding:10px 0; border-bottom:1px solid var(--line); cursor:pointer; }
  .item:hover { background:#f9fafb; }
  .item b { display:block; }
  .muted { color:var(--muted); font-size:13px; }
  .price { margin-left:auto; font-weight:700; }
  .stock.ok { color:var(--good); }
  .stock.low { color:var(--warn); }
  .stock.out { color:var(--bad); }
  .cart-line { display:flex; align-items:center; gap:8px; padding:8px 0; border-bottom:1px solid var(--line); }
  .qty { width:64px; text-align:center; }
  .total { font-size:28px; font-weight:800; margin:10px 0; }
  .pay { display:flex; gap:8px; flex-wrap:wrap; }
  .pay button, .primary { background:var(--brand); color:#fff; border:0; padding:12px 14px; border-radius:10px; cursor:pointer; }
  .pay button.ghost, .ghost { background:#fff; color:var(--ink); border:1px solid var(--line); }
  .pay button.good { background:var(--good); }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:8px 6px; border-bottom:1px solid var(--line); font-size:14px; }
  .toast { position:fixed; bottom:16px; right:16px; background:#111827; color:#fff; padding:10px 14px; border-radius:10px; display:none; }
  label { font-size:12px; color:var(--muted); display:block; margin:8px 0 4px; }
</style>
</head>
<body>
<header>
  <h1>ShopPOS</h1>
  <nav>
    <button class="active" data-tab="sell">Sell</button>
    <button data-tab="stock">Stock</button>
    <button data-tab="history">History</button>
    <button data-tab="settings">Settings</button>
  </nav>
  <div class="stats" id="stats">Today: —</div>
</header>
<main>
<section id="sell" class="grid">
  <div class="card">
    <h2>Products</h2>
    <input id="search" placeholder="Search name or barcode (scanner works here)" autofocus/>
    <div class="list" id="productList"></div>
  </div>
  <div class="card">
    <h2>Cart</h2>
    <div id="cart"></div>
    <div class="total" id="total">0.00</div>
    <label>Cash received</label>
    <input id="paid" type="number" min="0" step="0.01" placeholder="0.00"/>
    <div class="muted" id="change">Change: 0.00</div>
    <div class="pay" style="margin-top:10px">
      <button class="good" onclick="checkout()">Charge</button>
      <button class="ghost" onclick="clearCart()">Clear</button>
    </div>
  </div>
</section>

<section id="stock" class="card" hidden>
  <h2>Add product</h2>
  <div class="row">
    <div style="flex:2"><label>Name</label><input id="pName" placeholder="e.g. Water 600ml"/></div>
    <div style="flex:1"><label>Barcode</label><input id="pCode" placeholder="optional"/></div>
  </div>
  <div class="row">
    <div><label>Sell price</label><input id="pPrice" type="number" step="0.01" value="0"/></div>
    <div><label>Cost</label><input id="pCost" type="number" step="0.01" value="0"/></div>
    <div><label>Opening stock</label><input id="pStock" type="number" step="1" value="0"/></div>
  </div>
  <button class="primary" onclick="addProduct()">Save product</button>
  <h2 style="margin-top:22px">Inventory</h2>
  <div id="low" class="muted"></div>
  <table><thead><tr><th>Name</th><th>Barcode</th><th>Price</th><th>Stock</th><th></th></tr></thead>
  <tbody id="stockBody"></tbody></table>
</section>

<section id="history" class="card" hidden>
  <h2>Recent sales</h2>
  <div id="sales"></div>
</section>

<section id="settings" class="card" hidden>
  <h2>Updates</h2>
  <p class="muted" id="verLine">Version —</p>
  <label>GitHub raw URL for shoppos.py</label>
  <input id="updateUrl" placeholder="https://raw.githubusercontent.com/USERNAME/shoppos/main/shoppos.py"/>
  <div class="pay" style="margin-top:10px">
    <button class="ghost" onclick="saveUpdateUrl()">Save URL</button>
    <button class="primary" onclick="doUpdate()">Check and update</button>
  </div>
  <p class="muted" style="margin-top:12px">After a successful update, close the black Python window and start shoppos.py again. Your shoppos.db is not replaced.</p>
</section>
</main>
<div class="toast" id="toast"></div>
<script>
const $ = (id) => document.getElementById(id);
let products = [];
let cart = [];

function toast(msg){
  const t = $("toast"); t.textContent = msg; t.style.display="block";
  setTimeout(()=>t.style.display="none", 2200);
}
function money(n){ return Number(n).toFixed(2); }
function stockClass(s){ return s<=0?"out":s<=5?"low":"ok"; }

document.querySelectorAll("nav button").forEach(b=>{
  b.onclick = ()=>{
    document.querySelectorAll("nav button").forEach(x=>x.classList.remove("active"));
    b.classList.add("active");
    ["sell","stock","history","settings"].forEach(id=>$(id).hidden = id!==b.dataset.tab);
    if(b.dataset.tab==="history") loadSales();
    if(b.dataset.tab==="stock") loadProducts();
    if(b.dataset.tab==="settings") loadMeta();
  };
});

async function api(path, opt){
  const r = await fetch(path, opt);
  const data = await r.json();
  if(!r.ok) throw new Error(data.error || "Request failed");
  return data;
}

async function loadSummary(){
  const s = await api("/api/summary");
  $("stats").textContent = `Today: ${money(s.today_total)}  ·  ${s.today_count} sales  ·  ${s.product_count} items`;
  $("low").textContent = s.low_stock.length ? ("Low stock: " + s.low_stock.map(p=>p.name+" ("+p.stock+")").join(", ")) : "No low-stock alerts.";
}

async function loadProducts(){
  const q = $("search") ? $("search").value : "";
  products = await api("/api/products" + (q?("?q="+encodeURIComponent(q)):""));
  $("productList").innerHTML = products.map(p=>`
    <div class="item" onclick="addToCart(${p.id})">
      <div><b>${esc(p.name)}</b><div class="muted">${esc(p.barcode||"")} · <span class="stock ${stockClass(p.stock)}">${p.stock} in stock</span></div></div>
      <div class="price">${money(p.price)}</div>
    </div>`).join("") || "<p class='muted'>No products yet. Add some in Stock.</p>";
  $("stockBody").innerHTML = products.map(p=>`
    <tr>
      <td>${esc(p.name)}</td><td>${esc(p.barcode||"")}</td>
      <td>${money(p.price)}</td>
      <td class="stock ${stockClass(p.stock)}">${p.stock}</td>
      <td>
        <button class="ghost" onclick="adjust(${p.id},1)">+1</button>
        <button class="ghost" onclick="adjust(${p.id},-1)">-1</button>
        <button class="ghost" onclick="promptQty(${p.id})">+/- qty</button>
      </td>
    </tr>`).join("");
  loadSummary();
}

function esc(s){ return String(s).replace(/[&<>"]/g, c=>({ "&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;" }[c])); }

function addToCart(id){
  const p = products.find(x=>x.id===id);
  if(!p) return;
  const line = cart.find(x=>x.product_id===id);
  const next = (line?line.qty:0)+1;
  if(next > p.stock){ toast("Not enough stock"); return; }
  if(line) line.qty = next; else cart.push({product_id:id, name:p.name, price:p.price, qty:1});
  renderCart();
}

function renderCart(){
  $("cart").innerHTML = cart.map((l,i)=>`
    <div class="cart-line">
      <div style="flex:1"><b>${esc(l.name)}</b><div class="muted">${money(l.price)}</div></div>
      <button class="ghost" onclick="chg(${i},-1)">−</button>
      <div class="qty">${l.qty}</div>
      <button class="ghost" onclick="chg(${i},1)">+</button>
      <div class="price">${money(l.price*l.qty)}</div>
    </div>`).join("") || "<p class='muted'>Tap a product to add.</p>";
  const t = cart.reduce((s,l)=>s+l.price*l.qty,0);
  $("total").textContent = money(t);
  updateChange();
}
function chg(i,d){
  cart[i].qty += d;
  if(cart[i].qty<=0) cart.splice(i,1);
  renderCart();
}
function clearCart(){ cart=[]; renderCart(); }
function updateChange(){
  const t = cart.reduce((s,l)=>s+l.price*l.qty,0);
  const paid = Number($("paid").value||0);
  $("change").textContent = "Change: " + money(Math.max(0, paid-t));
}
$("paid").addEventListener("input", updateChange);
$("search").addEventListener("input", ()=>loadProducts());
$("search").addEventListener("keydown", (e)=>{
  if(e.key==="Enter" && products.length===1){ addToCart(products[0].id); $("search").value=""; loadProducts(); }
});

async function checkout(){
  const t = cart.reduce((s,l)=>s+l.price*l.qty,0);
  let paid = Number($("paid").value||0);
  if(!cart.length){ toast("Cart is empty"); return; }
  if(!paid) paid = t;
  try{
    const sale = await api("/api/checkout", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ items: cart.map(l=>({product_id:l.product_id, qty:l.qty})), paid })
    });
    toast("Sale #"+sale.id+" · change "+money(sale.change_amt));
    cart=[]; $("paid").value=""; renderCart(); loadProducts();
  }catch(err){ toast(err.message); }
}

async function addProduct(){
  try{
    await api("/api/products", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({
        name: $("pName").value, barcode: $("pCode").value,
        price: $("pPrice").value, cost: $("pCost").value, stock: $("pStock").value
      })
    });
    $("pName").value=$("pCode").value=""; $("pPrice").value=$("pCost").value=$("pStock").value="0";
    toast("Product saved"); loadProducts();
  }catch(err){ toast(err.message); }
}

function promptQty(id){
  const n = Number(prompt("Add stock (use negative to remove), e.g. 10 or -2"));
  if(!Number.isFinite(n) || n===0) return 0;
  adjust(id, n);
  return 0;
}
async function adjust(id, qty){
  if(!qty) return;
  try{
    await api(`/api/products/${id}/adjust`, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ qty, reason:"adjust" })
    });
    loadProducts();
  }catch(err){ toast(err.message); }
}

async function loadSales(){
  const sales = await api("/api/sales");
  $("sales").innerHTML = sales.map(s=>`
    <div class="item" style="cursor:default">
      <div><b>#${s.id} · ${money(s.total)}</b>
        <div class="muted">${esc(s.created_at)} · ${s.items.map(i=>i.qty+"× "+i.name).join(", ")}</div>
      </div>
    </div>`).join("") || "<p class='muted'>No sales yet.</p>";
}

async function loadMeta(){
  const m = await api("/api/meta");
  $("verLine").textContent = "This PC version: " + m.version;
  $("updateUrl").value = m.update_url || "";
}
async function saveUpdateUrl(){
  try{
    await api("/api/config", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ update_url: $("updateUrl").value })
    });
    toast("URL saved");
  }catch(err){ toast(err.message); }
}
async function doUpdate(){
  try{
    await saveUpdateUrl();
    const r = await api("/api/update", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ update_url: $("updateUrl").value })
    });
    toast(r.message || "Updated");
  }catch(err){ toast(err.message); }
}

loadProducts();
loadMeta();
</script>
</body>
</html>
"""


def main():
    init_db()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"ShopPOS running at http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop. Data file:", DB_PATH)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        httpd.server_close()


if __name__ == "__main__":
    main()
