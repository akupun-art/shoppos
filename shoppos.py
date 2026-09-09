#!/usr/bin/env python3
"""
ShopPOS — simple local stock + point of sale.
No extra packages. Python 3.8+ standard library only.

Run:  python3 shoppos.py
Open: http://127.0.0.1:8765
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import urllib.request
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

APP_VERSION = "1.54"
DEDICATION = "Pendekar's App — Blogs can die.. idea lives on."
DEFAULT_UPDATE_URL = "https://raw.githubusercontent.com/akupun-art/shoppos/main/shoppos.py"
ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "shoppos.db"
CONFIG_PATH = ROOT / "shoppos-config.json"
HOST = "127.0.0.1"
PORT = 8765


def load_config() -> dict:
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    if not (cfg.get("update_url") or "").strip():
        cfg["update_url"] = DEFAULT_UPDATE_URL
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


SESSIONS: dict[str, dict] = {}


def hash_pw(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return f"{salt}${digest}"


def check_pw(password: str, stored: str) -> bool:
    if not stored or "$" not in stored:
        return False
    salt, _ = stored.split("$", 1)
    return secrets.compare_digest(hash_pw(password, salt), stored)


def public_user(row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "name": row["name"],
        "role": row["role"],
    }


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
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            role TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    cols = [r[1] for r in con.execute("PRAGMA table_info(sales)").fetchall()]
    if "user_id" not in cols:
        con.execute("ALTER TABLE sales ADD COLUMN user_id INTEGER")
    if "voided" not in cols:
        con.execute("ALTER TABLE sales ADD COLUMN voided INTEGER NOT NULL DEFAULT 0")
    if "voided_by" not in cols:
        con.execute("ALTER TABLE sales ADD COLUMN voided_by INTEGER")
    if "voided_at" not in cols:
        con.execute("ALTER TABLE sales ADD COLUMN voided_at TEXT")
    if "discount" not in cols:
        con.execute("ALTER TABLE sales ADD COLUMN discount REAL NOT NULL DEFAULT 0")
    if "subtotal" not in cols:
        con.execute("ALTER TABLE sales ADD COLUMN subtotal REAL NOT NULL DEFAULT 0")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS day_closes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT NOT NULL,
            expected REAL NOT NULL,
            counted REAL NOT NULL,
            diff REAL NOT NULL,
            note TEXT,
            user_id INTEGER,
            created_at TEXT NOT NULL
        )
        """
    )
    if con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        con.execute(
            """INSERT INTO users (username, name, role, password_hash, created_at)
               VALUES (?,?,?,?,?)""",
            ("admin", "Owner", "admin", hash_pw("admin"), now_iso()),
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

    def _send(self, code: int, body, content_type="application/json", extra_headers=None):
        data = body if isinstance(body, (bytes, bytearray)) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200, extra_headers=None):
        self._send(
            code,
            json.dumps(obj, ensure_ascii=False),
            "application/json; charset=utf-8",
            extra_headers=extra_headers,
        )

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _cookie_sid(self) -> str:
        raw = self.headers.get("Cookie") or ""
        c = SimpleCookie()
        try:
            c.load(raw)
        except Exception:
            return ""
        if "shoppos_sid" in c:
            return c["shoppos_sid"].value
        return ""

    def current_user(self):
        sid = self._cookie_sid()
        sess = SESSIONS.get(sid)
        if not sess:
            return None
        con = connect()
        row = con.execute("SELECT * FROM users WHERE id=?", (sess["user_id"],)).fetchone()
        con.close()
        return dict(row) if row else None

    def require(self, *roles):
        user = self.current_user()
        if not user:
            self._json({"error": "Please log in"}, 401)
            return None
        if roles and user["role"] not in roles:
            self._json({"error": "No permission"}, 403)
            return None
        return user

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, HTML, "text/html; charset=utf-8")
            return
        if path == "/api/me":
            user = self.current_user()
            if not user:
                self._json({"user": None})
                return
            self._json({"user": public_user(user)})
            return
        if path == "/api/users":
            if not self.require("admin"):
                return
            con = connect()
            data = [public_user(r) for r in con.execute("SELECT * FROM users ORDER BY role, username")]
            con.close()
            self._json(data)
            return
        if path == "/api/products":
            if not self.require("admin", "manager", "staff"):
                return
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
            if not self.require("admin", "manager", "staff"):
                return
            con = connect()
            sales = rows(
                con.execute(
                    """SELECT s.*, u.username AS cashier
                       FROM sales s LEFT JOIN users u ON u.id = s.user_id
                       ORDER BY s.id DESC LIMIT 100"""
                )
            )
            for s in sales:
                s["items"] = rows(
                    con.execute("SELECT * FROM sale_items WHERE sale_id=?", (s["id"],))
                )
            con.close()
            self._json(sales)
            return
        if path == "/api/meta":
            if not self.require("admin", "manager", "staff"):
                return
            cfg = load_config()
            self._json(
                {
                    "version": APP_VERSION,
                    "update_url": cfg.get("update_url", ""),
                    "shop_name": cfg.get("shop_name", "ShopPOS"),
                    "shop_phone": cfg.get("shop_phone", ""),
                    "shop_address": cfg.get("shop_address", ""),
                    "paper": cfg.get("paper", "58"),
                    "dedication": DEDICATION,
                }
            )
            return
        if path == "/api/changelog":
            if not self.require("admin", "manager"):
                return
            cfg = load_config()
            url = (cfg.get("update_url") or "").strip()
            text = ""
            local = ROOT / "CHANGELOG.md"
            if url:
                if "github.com" in url and "/blob/" in url:
                    url = url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
                log_url = url.replace("shoppos.py", "CHANGELOG.md")
                if log_url != url:
                    try:
                        req = urllib.request.Request(log_url, headers={"User-Agent": "ShopPOS"})
                        with urllib.request.urlopen(req, timeout=20) as resp:
                            text = resp.read().decode("utf-8", errors="replace")
                    except Exception:
                        text = ""
            if not text and local.exists():
                text = local.read_text(encoding="utf-8")
            if not text:
                text = "No changelog yet. Upload CHANGELOG.md to the GitHub repo next to shoppos.py."
            self._json({"text": text})
            return
        if path == "/api/summary":
            if not self.require("admin", "manager", "staff"):
                return
            con = connect()
            today = datetime.now().strftime("%Y-%m-%d")
            sold = con.execute(
                """SELECT IFNULL(SUM(total),0) AS t, COUNT(*) AS n FROM sales
                   WHERE created_at LIKE ? AND IFNULL(voided,0)=0""",
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
        if path == "/api/day":
            if not self.require("admin", "manager"):
                return
            today = datetime.now().strftime("%Y-%m-%d")
            con = connect()
            sold = con.execute(
                """SELECT IFNULL(SUM(total),0) AS t, IFNULL(SUM(paid),0) AS p, COUNT(*) AS n
                   FROM sales WHERE created_at LIKE ? AND IFNULL(voided,0)=0""",
                (today + "%",),
            ).fetchone()
            closes = rows(con.execute("SELECT * FROM day_closes ORDER BY id DESC LIMIT 20"))
            con.close()
            self._json({"day": today, "expected": sold["t"], "paid": sold["p"], "count": sold["n"], "closes": closes})
            return
        if path == "/api/backup":
            if not self.require("admin"):
                return
            data = DB_PATH.read_bytes() if DB_PATH.exists() else b""
            name = "shoppos-backup-" + datetime.now().strftime("%Y%m%d-%H%M") + ".db"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
        except json.JSONDecodeError:
            self._json({"error": "invalid json"}, 400)
            return

        if path == "/api/login":
            username = (payload.get("username") or "").strip()
            password = payload.get("password") or ""
            con = connect()
            row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            con.close()
            if not row or not check_pw(password, row["password_hash"]):
                self._json({"error": "Wrong ID or password"}, 401)
                return
            sid = secrets.token_hex(16)
            SESSIONS[sid] = {"user_id": row["id"]}
            cookie = f"shoppos_sid={sid}; Path=/; HttpOnly; SameSite=Lax"
            self._json({"user": public_user(row)}, extra_headers={"Set-Cookie": cookie})
            return

        if path == "/api/logout":
            sid = self._cookie_sid()
            SESSIONS.pop(sid, None)
            self._json({"ok": True}, extra_headers={"Set-Cookie": "shoppos_sid=; Path=/; Max-Age=0"})
            return

        if path == "/api/users":
            if not self.require("admin"):
                return
            username = (payload.get("username") or "").strip()
            name = (payload.get("name") or username).strip()
            role = (payload.get("role") or "staff").strip()
            password = payload.get("password") or ""
            if not username or not password:
                self._json({"error": "ID and password required"}, 400)
                return
            if role not in ("admin", "manager", "staff"):
                self._json({"error": "Role must be admin, manager or staff"}, 400)
                return
            con = connect()
            try:
                con.execute(
                    """INSERT INTO users (username, name, role, password_hash, created_at)
                       VALUES (?,?,?,?,?)""",
                    (username, name, role, hash_pw(password), now_iso()),
                )
                con.commit()
            except sqlite3.IntegrityError:
                con.close()
                self._json({"error": "That ID is already used"}, 400)
                return
            row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            con.close()
            self._json(public_user(row), 201)
            return

        if path == "/api/password":
            user = self.require("admin", "manager", "staff")
            if not user:
                return
            old = payload.get("old") or ""
            new = payload.get("new") or ""
            if len(new) < 4:
                self._json({"error": "New password must be at least 4 characters"}, 400)
                return
            if not check_pw(old, user["password_hash"]):
                self._json({"error": "Current password is wrong"}, 400)
                return
            con = connect()
            con.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_pw(new), user["id"]))
            con.commit()
            con.close()
            self._json({"ok": True})
            return

        if path.startswith("/api/users/") and path.endswith("/password"):
            if not self.require("admin"):
                return
            uid = int(path.split("/")[3])
            new = payload.get("password") or ""
            if len(new) < 4:
                self._json({"error": "Password must be at least 4 characters"}, 400)
                return
            con = connect()
            row = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if not row:
                con.close()
                self._json({"error": "User not found"}, 404)
                return
            con.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_pw(new), uid))
            con.commit()
            con.close()
            self._json({"ok": True})
            return

        if path == "/api/config":
            if not self.require("admin"):
                return
            cfg = load_config()
            if "update_url" in payload:
                cfg["update_url"] = (payload.get("update_url") or "").strip()
            if "shop_name" in payload:
                cfg["shop_name"] = (payload.get("shop_name") or "ShopPOS").strip() or "ShopPOS"
            if "shop_phone" in payload:
                cfg["shop_phone"] = (payload.get("shop_phone") or "").strip()
            if "shop_address" in payload:
                cfg["shop_address"] = (payload.get("shop_address") or "").strip()
            if "paper" in payload:
                cfg["paper"] = payload.get("paper") if payload.get("paper") in ("58", "a4") else "58"
            save_config(cfg)
            self._json({"ok": True, **{k: cfg.get(k, "") for k in ("update_url", "shop_name", "shop_phone", "shop_address", "paper")}})
            return

        if path == "/api/update":
            if not self.require("admin"):
                return
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
            if not self.require("admin", "manager"):
                return
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
            if not self.require("admin", "manager"):
                return
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
            user = self.require("admin", "manager", "staff")
            if not user:
                return
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
                subtotal = round(total, 2)
                try:
                    discount = float(payload.get("discount") or 0)
                except (TypeError, ValueError):
                    discount = 0.0
                if discount < 0:
                    discount = 0.0
                if discount > subtotal:
                    discount = subtotal
                discount = round(discount, 2)
                total = round(subtotal - discount, 2)
                if paid < total:
                    con.close()
                    self._json({"error": f"Paid is less than total ({total:.2f})"}, 400)
                    return
                change = round(paid - total, 2)
                note = payload.get("note") or ""
                if discount:
                    note = (note + " | discount " + f"{discount:.2f}").strip(" |")
                cur = con.execute(
                    """INSERT INTO sales (total, paid, change_amt, note, created_at, user_id, discount, subtotal)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (total, paid, change, note, now_iso(), user["id"], discount, subtotal),
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
            sale["cashier"] = user.get("name") or user.get("username")
            self._json(sale, 201)
            return

        if path.startswith("/api/sales/") and path.endswith("/void"):
            user = self.require("admin", "manager")
            if not user:
                return
            sid = int(path.split("/")[3])
            con = connect()
            sale = con.execute("SELECT * FROM sales WHERE id=?", (sid,)).fetchone()
            if not sale:
                con.close()
                self._json({"error": "Sale not found"}, 404)
                return
            if sale["voided"]:
                con.close()
                self._json({"error": "Already voided"}, 400)
                return
            items = rows(con.execute("SELECT * FROM sale_items WHERE sale_id=?", (sid,)))
            for it in items:
                if it["product_id"]:
                    con.execute("UPDATE products SET stock = stock + ? WHERE id=?", (it["qty"], it["product_id"]))
                    con.execute(
                        """INSERT INTO stock_moves (product_id, qty, reason, note, created_at)
                           VALUES (?,?,?,?,?)""",
                        (it["product_id"], it["qty"], "void", f"void sale #{sid}", now_iso()),
                    )
            con.execute(
                "UPDATE sales SET voided=1, voided_by=?, voided_at=? WHERE id=?",
                (user["id"], now_iso(), sid),
            )
            con.commit()
            con.close()
            self._json({"ok": True})
            return

        if path == "/api/day-close":
            user = self.require("admin", "manager")
            if not user:
                return
            try:
                counted = float(payload.get("counted") or 0)
            except (TypeError, ValueError):
                self._json({"error": "Counted cash must be a number"}, 400)
                return
            today = datetime.now().strftime("%Y-%m-%d")
            con = connect()
            sold = con.execute(
                """SELECT IFNULL(SUM(total),0) AS t FROM sales
                   WHERE created_at LIKE ? AND IFNULL(voided,0)=0""",
                (today + "%",),
            ).fetchone()
            expected = float(sold["t"] or 0)
            diff = round(counted - expected, 2)
            con.execute(
                """INSERT INTO day_closes (day, expected, counted, diff, note, user_id, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (today, expected, counted, diff, payload.get("note") or "", user["id"], now_iso()),
            )
            con.commit()
            row = dict(con.execute("SELECT * FROM day_closes ORDER BY id DESC LIMIT 1").fetchone())
            con.close()
            self._json(row, 201)
            return

        self._json({"error": "not found"}, 404)

    def do_PUT(self):
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
        except json.JSONDecodeError:
            self._json({"error": "invalid json"}, 400)
            return
        if path.startswith("/api/products/"):
            if not self.require("admin", "manager"):
                return
            pid = int(path.split("/")[3])
            name = (payload.get("name") or "").strip()
            if not name:
                self._json({"error": "Name is required"}, 400)
                return
            barcode = (payload.get("barcode") or "").strip() or None
            try:
                price = float(payload.get("price") or 0)
                cost = float(payload.get("cost") or 0)
            except (TypeError, ValueError):
                self._json({"error": "Price / cost must be numbers"}, 400)
                return
            con = connect()
            p = con.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
            if not p:
                con.close()
                self._json({"error": "Product not found"}, 404)
                return
            try:
                con.execute(
                    "UPDATE products SET name=?, barcode=?, price=?, cost=? WHERE id=?",
                    (name, barcode, price, cost, pid),
                )
                con.commit()
            except sqlite3.IntegrityError:
                con.close()
                self._json({"error": "Barcode already used"}, 400)
                return
            row = dict(con.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone())
            con.close()
            self._json(row)
            return
        self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/products/"):
            if not self.require("admin", "manager"):
                return
            pid = int(path.split("/")[3])
            con = connect()
            con.execute("UPDATE sale_items SET product_id=NULL WHERE product_id=?", (pid,))
            con.execute("DELETE FROM products WHERE id=?", (pid,))
            con.commit()
            con.close()
            self._json({"ok": True})
            return
        if path.startswith("/api/users/"):
            admin = self.require("admin")
            if not admin:
                return
            uid = int(path.split("/")[3])
            if uid == admin["id"]:
                self._json({"error": "You cannot delete the account you are using"}, 400)
                return
            con = connect()
            row = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if not row:
                con.close()
                self._json({"error": "User not found"}, 404)
                return
            if row["role"] == "admin":
                n = con.execute("SELECT COUNT(*) AS n FROM users WHERE role='admin'").fetchone()["n"]
                if n <= 1:
                    con.close()
                    self._json({"error": "Cannot delete the last admin"}, 400)
                    return
            con.execute("DELETE FROM users WHERE id=?", (uid,))
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
  tr.clickable { cursor:pointer; }
  tr.clickable:hover { background:#f3f4f6; }
  .label-box { border:1px dashed var(--line); border-radius:12px; padding:16px; text-align:center; background:#fff; }
  .toast { position:fixed; bottom:16px; right:16px; background:#111827; color:#fff; padding:10px 14px; border-radius:10px; display:none; }
  #login { position:fixed; inset:0; background:#111827; display:flex; align-items:center; justify-content:center; z-index:20; }
  #login .card { width:min(380px,92vw); }
  label { font-size:12px; color:var(--muted); display:block; margin:8px 0 4px; }
  .dedication { text-align:center; color:#6b7280; font-size:12px; padding:18px 14px 8px; }
</style>
</head>
<body>
<header>
  <h1>ShopPOS</h1>
  <nav>
    <button class="active" data-tab="sell" data-i18n="sell">Sell</button>
    <button data-tab="stock" data-i18n="stock">Stock</button>
    <button data-tab="history" data-i18n="history">History</button>
    <button data-tab="settings" data-i18n="settings">Settings</button>
    <button data-tab="people" data-i18n="people">People</button>
    <button data-tab="account" data-i18n="passwordTab">Password</button>
  </nav>
  <div class="stats" id="stats">Today: —</div>
  <div class="muted" id="who" style="color:#d1d5db;font-size:13px"></div>
  <button class="ghost" id="logoutBtn" onclick="doLogout()" style="display:none;background:#1f2937;color:#fff;border:0" data-i18n="logout">Log out</button>
</header>
<div id="login">
  <div class="card">
    <h2 data-i18n="loginTitle">ShopPOS login</h2>
    <label data-i18n="staffId">Staff ID</label>
    <input id="loginId" placeholder="e.g. admin or ali01"/>
    <label data-i18n="password">Password</label>
    <input id="loginPw" type="password"/>
    <div class="pay" style="margin-top:12px">
      <button class="primary" onclick="doLogin()" data-i18n="login">Log in</button>
    </div>
    <p class="muted" id="loginErr"></p>
  </div>
</div>
<main>
<section id="sell" class="grid">
  <div class="card">
    <h2 data-i18n="products">Products</h2>
    <input id="search" placeholder="Search name or barcode (scanner works here)" data-i18n-placeholder="searchPh" autofocus/>
    <div class="list" id="productList"></div>
  </div>
  <div class="card">
    <h2 data-i18n="cart">Cart</h2>
    <div id="cart"></div>
    <div class="total" id="total">0.00</div>
    <div style="background:#fff7ed;border:1px solid #fdba74;border-radius:12px;padding:10px;margin:8px 0 12px">
      <b>Discount</b>
      <div class="row" style="margin:6px 0 0">
        <div><label>RM off</label><input id="discRm" type="number" min="0" step="0.01" value="0" oninput="updateChange()"/></div>
        <div><label>% off</label><input id="discPct" type="number" min="0" max="100" step="0.01" value="0" oninput="updateChange()"/></div>
      </div>
      <div class="muted" id="discLine"></div>
    </div>
    <label data-i18n="cashReceived">Cash received</label>
    <input id="paid" type="number" min="0" step="0.01" placeholder="0.00"/>
    <div class="muted" id="change">Change: 0.00</div>
    <div class="pay" style="margin-top:10px">
      <button class="good" onclick="checkout()" data-i18n="charge">Charge</button>
      <button class="ghost" onclick="holdCart()" data-i18n="hold">Hold</button>
      <button class="ghost" onclick="clearCart()" data-i18n="clear">Clear</button>
    </div>
    <div id="holds" style="margin-top:12px"></div>
  </div>
</section>

<section id="stock" class="card" hidden>
  <h2 id="formTitle">Add product</h2>
  <input type="hidden" id="pId" value=""/>
  <div class="row">
    <div style="flex:2"><label>Name</label><input id="pName" placeholder="e.g. Water 600ml"/></div>
    <div style="flex:1"><label>Barcode</label><input id="pCode" placeholder="optional"/></div>
  </div>
  <div class="row">
    <div><label>Sell price</label><input id="pPrice" type="number" step="0.01" value="0"/></div>
    <div><label>Cost</label><input id="pCost" type="number" step="0.01" value="0"/></div>
    <div id="openStockWrap"><label>Opening stock</label><input id="pStock" type="number" step="1" value="0"/></div>
  </div>
  <div class="pay">
    <button class="primary" id="saveBtn" onclick="saveProduct()">Save product</button>
    <button class="ghost" id="cancelEdit" onclick="resetForm()" hidden>Cancel edit</button>
  </div>
  <h2 style="margin-top:22px">Inventory</h2>
  <div id="low" class="muted"></div>
  <table><thead><tr><th>Name</th><th>Barcode</th><th>Price</th><th>Stock</th><th></th></tr></thead>
  <tbody id="stockBody"></tbody></table>
</section>

<section id="product" class="card" hidden>
  <div class="pay" style="margin-bottom:12px">
    <button class="ghost" onclick="showTab('stock')">← Back to stock</button>
  </div>
  <h2 id="prodTitle">Product</h2>
  <input type="hidden" id="dId" value=""/>
  <div class="row">
    <div style="flex:2"><label>Name</label><input id="dName"/></div>
    <div style="flex:1"><label>Barcode number</label><input id="dCode" placeholder="scan or type"/></div>
  </div>
  <div class="row">
    <div><label>Sell price</label><input id="dPrice" type="number" step="0.01"/></div>
    <div><label>Cost</label><input id="dCost" type="number" step="0.01"/></div>
    <div><label>Stock</label><input id="dStock" type="number" step="1"/></div>
  </div>
  <div class="pay">
    <button class="primary" onclick="saveDetail()">Save changes</button>
    <button class="ghost" onclick="adjustFromDetail(1)">Stock +1</button>
    <button class="ghost" onclick="adjustFromDetail(-1)">Stock −1</button>
    <button class="ghost" onclick="deleteFromDetail()">Delete</button>
  </div>
  <h2 style="margin-top:22px">Label</h2>
  <p class="muted">This draws the bars from the barcode number. Print and stick on the product. Scanner reads the bars.</p>
  <div class="label-box" id="labelPreview">
    <div id="labelName" style="font-weight:700;margin-bottom:8px"></div>
    <svg id="labelBars" width="280" height="80"></svg>
    <div id="labelCode" class="muted"></div>
    <div id="labelPrice" style="font-weight:700;margin-top:6px"></div>
  </div>
  <div class="pay" style="margin-top:10px">
    <button class="primary" onclick="printLabel()">Print label</button>
  </div>
</section>

<section id="history" class="card" hidden>
  <h2>End of day</h2>
  <p class="muted" id="dayLine">Today: —</p>
  <div class="row">
    <div><label>Cash counted in drawer</label><input id="counted" type="number" step="0.01" placeholder="0.00"/></div>
  </div>
  <div class="pay">
    <button class="primary" onclick="closeDay()">Save day close</button>
    <button class="ghost" onclick="printDay()">Print summary</button>
  </div>
  <div id="closes" class="muted" style="margin:10px 0 18px"></div>
  <h2>Recent sales</h2>
  <div id="sales"></div>
</section>

<section id="settings" class="card" hidden>
  <h2 data-i18n="language">Language</h2>
  <select id="settingsLang" onchange="setLang(this.value)" style="width:100%;max-width:320px;padding:10px 12px;border:1px solid var(--line);border-radius:10px;margin-bottom:18px"></select>
  <h2>Updates</h2>
  <p class="muted" id="verLine">Version —</p>
  <label>GitHub raw URL for shoppos.py</label>
  <input id="updateUrl" placeholder="https://raw.githubusercontent.com/USERNAME/shoppos/main/shoppos.py"/>
  <div class="pay" style="margin-top:10px">
    <button class="ghost" onclick="saveUpdateUrl()">Save URL</button>
    <button class="primary" onclick="doUpdate()">Check and update</button>
  </div>
  <p class="muted" style="margin-top:12px">After a successful update, close the black Python window and start shoppos.py again. Your shoppos.db is not replaced.</p>
  <h2 style="margin-top:22px">Receipt</h2>
  <label>Shop name on receipt</label>
  <input id="shopName" placeholder="My Shop"/>
  <label>Phone</label>
  <input id="shopPhone" placeholder="01x-xxxxxxx"/>
  <label>Address</label>
  <input id="shopAddress" placeholder="Street, town"/>
  <label>Paper</label>
  <select id="paper" style="width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:10px">
    <option value="58">58mm thermal</option>
    <option value="a4">A4</option>
  </select>
  <div class="pay" style="margin-top:10px">
    <button class="ghost" onclick="saveReceiptPrefs()">Save receipt settings</button>
  </div>
  <h2 style="margin-top:22px">Backup</h2>
  <p class="muted">Downloads shoppos.db (products, sales, users). Save the file onto a USB drive.</p>
  <button class="primary" onclick="downloadBackup()">Download backup</button>
  <h2 style="margin-top:22px">What changed</h2>
  <pre id="changelog" class="muted" style="white-space:pre-wrap;font:13px/1.45 system-ui,sans-serif">Loading changelog…</pre>
</section>

<section id="people" class="card" hidden>
  <h2>Staff accounts</h2>
  <p class="muted">Each person gets their own ID. Staff can only use Sell. Manager can sell and stock. Admin can do everything.</p>
  <div class="row">
    <div><label>Staff ID</label><input id="uId" placeholder="ali01"/></div>
    <div><label>Name</label><input id="uName" placeholder="Ali"/></div>
  </div>
  <div class="row">
    <div><label>Role</label>
      <select id="uRole" style="width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:10px">
        <option value="staff">staff</option>
        <option value="manager">manager</option>
        <option value="admin">admin</option>
      </select>
    </div>
    <div><label>Password</label><input id="uPw" type="password"/></div>
  </div>
  <button class="primary" onclick="addUser()">Create login</button>
  <table style="margin-top:16px"><thead><tr><th>ID</th><th>Name</th><th>Role</th><th></th></tr></thead>
  <tbody id="userBody"></tbody></table>
  <h2 style="margin-top:22px">Change my password</h2>
  <div class="row">
    <div><label>Current password</label><input id="oldPw" type="password"/></div>
    <div><label>New password</label><input id="newPw" type="password"/></div>
  </div>
  <button class="primary" onclick="changeMyPassword()">Save password</button>
</section>

<section id="account" class="card" hidden>
  <h2>Change my password</h2>
  <div class="row">
    <div><label>Current password</label><input id="oldPw2" type="password"/></div>
    <div><label>New password</label><input id="newPw2" type="password"/></div>
  </div>
  <button class="primary" onclick="changeMyPassword()">Save password</button>
</section>
</main>
<p class="dedication" id="dedication">Pendekar's App — Blogs can die.. idea lives on.</p>
<div class="toast" id="toast"></div>
<script>
const $ = (id) => document.getElementById(id);
let products = [];
let cart = [];
let currentUser = null;
let lang = localStorage.getItem("shoppos_lang") || "en";
const LANG_LIST = [
  ["en","English"],["ms","Bahasa Melayu"],["id","Bahasa Indonesia"],
  ["zh","简体中文"],["zh-tw","繁體中文"],["ta","தமிழ்"],["hi","हिन्दी"],
  ["th","ไทย"],["vi","Tiếng Việt"],["fil","Filipino"],
  ["ar","العربية"],["ur","اردو"],
  ["es","Español"],["fr","Français"],["de","Deutsch"],["pt","Português"],
  ["it","Italiano"],["ru","Русский"],["tr","Türkçe"],
  ["ja","日本語"],["ko","한국어"],["bn","বাংলা"]
];
const I18N = {
en:{sell:"Sell",stock:"Stock",history:"History",settings:"Settings",people:"People",passwordTab:"Password",logout:"Log out",loginTitle:"ShopPOS login",language:"Language",staffId:"Staff ID",password:"Password",login:"Log in",products:"Products",searchPh:"Search name or barcode (scanner works here)",cart:"Cart",cashReceived:"Cash received",charge:"Charge",hold:"Hold",clear:"Clear",change:"Change",tapAdd:"Tap a product to add.",emptyCart:"Cart is empty",inStock:"in stock",today:"Today",sales:"sales",items:"items"},
ms:{sell:"Jual",stock:"Stok",history:"Sejarah",settings:"Tetapan",people:"Pekerja",passwordTab:"Kata laluan",logout:"Log keluar",loginTitle:"Log masuk ShopPOS",language:"Bahasa",staffId:"ID staf",password:"Kata laluan",login:"Log masuk",products:"Produk",searchPh:"Cari nama atau kod bar (pengimbas di sini)",cart:"Troli",cashReceived:"Tunai diterima",charge:"Caj",hold:"Tahan",clear:"Kosongkan",change:"Baki",tapAdd:"Ketuk produk untuk tambah.",emptyCart:"Troli kosong",inStock:"dalam stok",today:"Hari ini",sales:"jualan",items:"item"},
id:{sell:"Jual",stock:"Stok",history:"Riwayat",settings:"Pengaturan",people:"Staf",passwordTab:"Kata sandi",logout:"Keluar",loginTitle:"Masuk ShopPOS",language:"Bahasa",staffId:"ID staf",password:"Kata sandi",login:"Masuk",products:"Produk",searchPh:"Cari nama atau barcode",cart:"Keranjang",cashReceived:"Tunai diterima",charge:"Bayar",hold:"Tahan",clear:"Hapus",change:"Kembalian",tapAdd:"Ketuk produk untuk menambah.",emptyCart:"Keranjang kosong",inStock:"stok",today:"Hari ini",sales:"penjualan",items:"item"},
zh:{sell:"收银",stock:"库存",history:"记录",settings:"设置",people:"员工",passwordTab:"密码",logout:"退出",loginTitle:"ShopPOS 登录",language:"语言",staffId:"工号",password:"密码",login:"登录",products:"商品",searchPh:"搜索名称或条码",cart:"购物车",cashReceived:"收款",charge:"结账",hold:"挂单",clear:"清空",change:"找零",tapAdd:"点击商品加入。",emptyCart:"购物车为空",inStock:"库存",today:"今天",sales:"笔",items:"件"},
"zh-tw":{sell:"收銀",stock:"庫存",history:"紀錄",settings:"設定",people:"員工",passwordTab:"密碼",logout:"登出",loginTitle:"ShopPOS 登入",language:"語言",staffId:"員工編號",password:"密碼",login:"登入",products:"商品",searchPh:"搜尋名稱或條碼",cart:"購物車",cashReceived:"收款",charge:"結帳",hold:"掛單",clear:"清除",change:"找零",tapAdd:"點選商品加入。",emptyCart:"購物車是空的",inStock:"庫存",today:"今天",sales:"筆",items:"件"},
ta:{sell:"விற்பனை",stock:"சரக்கு",history:"வரலாறு",settings:"அமைப்புகள்",people:"பணியாளர்",passwordTab:"கடவுச்சொல்",logout:"வெளியேறு",loginTitle:"ShopPOS நுழைவு",language:"மொழி",staffId:"ஊழியர் ID",password:"கடவுச்சொல்",login:"நுழை",products:"பொருட்கள்",searchPh:"பெயர் அல்லது பார்கோடு",cart:"கூடை",cashReceived:"ரொக்கம்",charge:"பணம் வாங்கு",hold:"வைத்திரு",clear:"அழி",change:"மீதி",tapAdd:"பொருளைத் தட்டவும்.",emptyCart:"கூடை காலி",inStock:"சரக்கு",today:"இன்று",sales:"விற்பனை",items:"பொருள்"},
hi:{sell:"बिक्री",stock:"स्टॉक",history:"इतिहास",settings:"सेटिंग",people:"स्टाफ",passwordTab:"पासवर्ड",logout:"लॉग आउट",loginTitle:"ShopPOS लॉगिन",language:"भाषा",staffId:"स्टाफ आईडी",password:"पासवर्ड",login:"लॉग इन",products:"सामान",searchPh:"नाम या बारकोड खोजें",cart:"कार्ट",cashReceived:"नकद",charge:"चार्ज",hold:"होल्ड",clear:"साफ़",change:"वापसी",tapAdd:"जोड़ने के लिए टैप करें।",emptyCart:"कार्ट खाली",inStock:"स्टॉक",today:"आज",sales:"बिक्री",items:"आइटम"},
th:{sell:"ขาย",stock:"สต็อก",history:"ประวัติ",settings:"ตั้งค่า",people:"พนักงาน",passwordTab:"รหัสผ่าน",logout:"ออก",loginTitle:"เข้าสู่ระบบ ShopPOS",language:"ภาษา",staffId:"รหัสพนักงาน",password:"รหัสผ่าน",login:"เข้าสู่ระบบ",products:"สินค้า",searchPh:"ค้นหาชื่อหรือบาร์โค้ด",cart:"ตะกร้า",cashReceived:"เงินสด",charge:"คิดเงิน",hold:"พักบิล",clear:"ล้าง",change:"เงินทอน",tapAdd:"แตะสินค้าเพื่อเพิ่ม",emptyCart:"ตะกร้าว่าง",inStock:"คงเหลือ",today:"วันนี้",sales:"บิล",items:"รายการ"},
vi:{sell:"Bán",stock:"Kho",history:"Lịch sử",settings:"Cài đặt",people:"Nhân viên",passwordTab:"Mật khẩu",logout:"Đăng xuất",loginTitle:"Đăng nhập ShopPOS",language:"Ngôn ngữ",staffId:"Mã NV",password:"Mật khẩu",login:"Đăng nhập",products:"Sản phẩm",searchPh:"Tìm tên hoặc mã vạch",cart:"Giỏ",cashReceived:"Tiền mặt",charge:"Thu tiền",hold:"Giữ đơn",clear:"Xóa",change:"Tiền thối",tapAdd:"Chạm để thêm.",emptyCart:"Giỏ trống",inStock:"tồn",today:"Hôm nay",sales:"đơn",items:"món"},
fil:{sell:"Benta",stock:"Stock",history:"Kasaysayan",settings:"Settings",people:"Staff",passwordTab:"Password",logout:"Log out",loginTitle:"Login sa ShopPOS",language:"Wika",staffId:"Staff ID",password:"Password",login:"Log in",products:"Produkto",searchPh:"Hanapin ang pangalan o barcode",cart:"Cart",cashReceived:"Cash",charge:"Charge",hold:"Hold",clear:"Clear",change:"Sukli",tapAdd:"I-tap para idagdag.",emptyCart:"Walang laman ang cart",inStock:"stock",today:"Ngayon",sales:"benta",items:"item"},
ar:{sell:"بيع",stock:"مخزون",history:"سجل",settings:"إعدادات",people:"موظفون",passwordTab:"كلمة السر",logout:"خروج",loginTitle:"دخول ShopPOS",language:"اللغة",staffId:"رقم الموظف",password:"كلمة السر",login:"دخول",products:"منتجات",searchPh:"ابحث بالاسم أو الباركود",cart:"سلة",cashReceived:"نقداً",charge:"تحصيل",hold:"تعليق",clear:"مسح",change:"الباقي",tapAdd:"اضغط للإضافة.",emptyCart:"السلة فارغة",inStock:"متوفر",today:"اليوم",sales:"مبيعات",items:"أصناف"},
ur:{sell:"فروخت",stock:"اسٹاک",history:"ریکارڈ",settings:"ترتیبات",people:"عملہ",passwordTab:"پاس ورڈ",logout:"لاگ آؤٹ",loginTitle:"ShopPOS لاگ ان",language:"زبان",staffId:"اسٹاف آئی ڈی",password:"پاس ورڈ",login:"لاگ ان",products:"سامان",searchPh:"نام یا بارکوڈ",cart:"کارٹ",cashReceived:"نقد",charge:"چارج",hold:"ہولڈ",clear:"خالی",change:"بقیہ",tapAdd:"شامل کرنے کے لیے ٹیپ کریں۔",emptyCart:"کارٹ خالی",inStock:"اسٹاک",today:"آج",sales:"سیلز",items:"آئٹم"},
es:{sell:"Vender",stock:"Stock",history:"Historial",settings:"Ajustes",people:"Personal",passwordTab:"Contraseña",logout:"Salir",loginTitle:"Entrar a ShopPOS",language:"Idioma",staffId:"ID de personal",password:"Contraseña",login:"Entrar",products:"Productos",searchPh:"Buscar nombre o código",cart:"Carrito",cashReceived:"Efectivo",charge:"Cobrar",hold:"Pausar",clear:"Vaciar",change:"Cambio",tapAdd:"Toca para añadir.",emptyCart:"Carrito vacío",inStock:"en stock",today:"Hoy",sales:"ventas",items:"artículos"},
fr:{sell:"Vente",stock:"Stock",history:"Historique",settings:"Réglages",people:"Équipe",passwordTab:"Mot de passe",logout:"Déconnexion",loginTitle:"Connexion ShopPOS",language:"Langue",staffId:"ID employé",password:"Mot de passe",login:"Connexion",products:"Produits",searchPh:"Nom ou code-barres",cart:"Panier",cashReceived:"Espèces",charge:"Encaisser",hold:"Mettre en attente",clear:"Vider",change:"Monnaie",tapAdd:"Touchez pour ajouter.",emptyCart:"Panier vide",inStock:"en stock",today:"Aujourd'hui",sales:"ventes",items:"articles"},
de:{sell:"Kasse",stock:"Lager",history:"Verlauf",settings:"Einstellungen",people:"Personal",passwordTab:"Passwort",logout:"Abmelden",loginTitle:"ShopPOS Anmeldung",language:"Sprache",staffId:"Mitarbeiter-ID",password:"Passwort",login:"Anmelden",products:"Artikel",searchPh:"Name oder Barcode",cart:"Warenkorb",cashReceived:"Bar",charge:"Kasse",hold:"Halten",clear:"Leeren",change:"Rückgeld",tapAdd:"Tippen zum Hinzufügen.",emptyCart:"Warenkorb leer",inStock:"auf Lager",today:"Heute",sales:"Verkäufe",items:"Artikel"},
pt:{sell:"Venda",stock:"Estoque",history:"Histórico",settings:"Definições",people:"Pessoal",passwordTab:"Palavra-passe",logout:"Sair",loginTitle:"Entrar no ShopPOS",language:"Idioma",staffId:"ID do funcionário",password:"Palavra-passe",login:"Entrar",products:"Produtos",searchPh:"Nome ou código de barras",cart:"Carrinho",cashReceived:"Dinheiro",charge:"Cobrar",hold:"Espera",clear:"Limpar",change:"Troco",tapAdd:"Toque para adicionar.",emptyCart:"Carrinho vazio",inStock:"em stock",today:"Hoje",sales:"vendas",items:"itens"},
it:{sell:"Vendita",stock:"Magazzino",history:"Cronologia",settings:"Impostazioni",people:"Personale",passwordTab:"Password",logout:"Esci",loginTitle:"Accesso ShopPOS",language:"Lingua",staffId:"ID dipendente",password:"Password",login:"Accedi",products:"Prodotti",searchPh:"Nome o codice a barre",cart:"Carrello",cashReceived:"Contanti",charge:"Incassa",hold:"Sospendi",clear:"Svuota",change:"Resto",tapAdd:"Tocca per aggiungere.",emptyCart:"Carrello vuoto",inStock:"disponibili",today:"Oggi",sales:"vendite",items:"articoli"},
ru:{sell:"Продажа",stock:"Склад",history:"История",settings:"Настройки",people:"Сотрудники",passwordTab:"Пароль",logout:"Выход",loginTitle:"Вход в ShopPOS",language:"Язык",staffId:"ID сотрудника",password:"Пароль",login:"Войти",products:"Товары",searchPh:"Имя или штрихкод",cart:"Корзина",cashReceived:"Наличные",charge:"Оплата",hold:"Отложить",clear:"Очистить",change:"Сдача",tapAdd:"Нажмите, чтобы добавить.",emptyCart:"Корзина пуста",inStock:"на складе",today:"Сегодня",sales:"продажи",items:"позиции"},
tr:{sell:"Satış",stock:"Stok",history:"Geçmiş",settings:"Ayarlar",people:"Personel",passwordTab:"Şifre",logout:"Çıkış",loginTitle:"ShopPOS giriş",language:"Dil",staffId:"Personel no",password:"Şifre",login:"Giriş",products:"Ürünler",searchPh:"Ad veya barkod ara",cart:"Sepet",cashReceived:"Nakit",charge:"Tahsil et",hold:"Beklet",clear:"Temizle",change:"Para üstü",tapAdd:"Eklemek için dokunun.",emptyCart:"Sepet boş",inStock:"stokta",today:"Bugün",sales:"satış",items:"ürün"},
ja:{sell:"販売",stock:"在庫",history:"履歴",settings:"設定",people:"スタッフ",passwordTab:"パスワード",logout:"ログアウト",loginTitle:"ShopPOS ログイン",language:"言語",staffId:"スタッフID",password:"パスワード",login:"ログイン",products:"商品",searchPh:"名前またはバーコード",cart:"カート",cashReceived:"預かり金",charge:"会計",hold:"保留",clear:"クリア",change:"お釣り",tapAdd:"タップして追加",emptyCart:"カートは空です",inStock:"在庫",today:"今日",sales:"件",items:"点"},
ko:{sell:"판매",stock:"재고",history:"기록",settings:"설정",people:"직원",passwordTab:"비밀번호",logout:"로그아웃",loginTitle:"ShopPOS 로그인",language:"언어",staffId:"직원 ID",password:"비밀번호",login:"로그인",products:"상품",searchPh:"이름 또는 바코드",cart:"장바구니",cashReceived:"받은 현금",charge:"결제",hold:"보류",clear:"비우기",change:"거스름돈",tapAdd:"눌러서 추가",emptyCart:"장바구니가 비었습니다",inStock:"재고",today:"오늘",sales:"건",items:"개"},
bn:{sell:"বিক্রি",stock:"স্টক",history:"ইতিহাস",settings:"সেটিংস",people:"স্টাফ",passwordTab:"পাসওয়ার্ড",logout:"লগ আউট",loginTitle:"ShopPOS লগইন",language:"ভাষা",staffId:"স্টাফ আইডি",password:"পাসওয়ার্ড",login:"লগইন",products:"পণ্য",searchPh:"নাম বা বারকোড",cart:"কার্ট",cashReceived:"নগদ",charge:"চার্জ",hold:"হোল্ড",clear:"মুছুন",change:"ফেরত",tapAdd:"যোগ করতে ট্যাপ করুন।",emptyCart:"কার্ট খালি",inStock:"স্টক",today:"আজ",sales:"বিক্রি",items:"আইটেম"}
};
function t(k){ const pack=I18N[lang]||I18N.en; return pack[k]||I18N.en[k]||k; }
function fillLangSelects(){
  ["settingsLang"].forEach(id=>{
    const el=$(id); if(!el) return;
    el.innerHTML=LANG_LIST.map(([c,n])=>`<option value="${c}">${n}</option>`).join("");
    el.value=lang;
  });
}
function applyI18n(){
  document.documentElement.lang = lang;
  document.documentElement.dir = (lang==="ar"||lang==="ur") ? "rtl" : "ltr";
  document.querySelectorAll("[data-i18n]").forEach(el=>{ el.textContent=t(el.getAttribute("data-i18n")); });
  document.querySelectorAll("[data-i18n-placeholder]").forEach(el=>{ el.placeholder=t(el.getAttribute("data-i18n-placeholder")); });
  fillLangSelects();
  if(typeof renderCart==="function") renderCart();
  if(typeof loadSummary==="function" && currentUser) loadSummary();
}
function setLang(code){
  lang=code||"en";
  localStorage.setItem("shoppos_lang", lang);
  applyI18n();
}

function toast(msg){
  const t = $("toast"); t.textContent = msg; t.style.display="block";
  setTimeout(()=>t.style.display="none", 2200);
}
function money(n){ return Number(n).toFixed(2); }
function stockClass(s){ return s<=0?"out":s<=5?"low":"ok"; }

document.querySelectorAll("nav button").forEach(b=>{
  b.onclick = ()=> showTab(b.dataset.tab);
});
function showTab(name){
  document.querySelectorAll("nav button").forEach(x=>x.classList.toggle("active", x.dataset.tab===name));
  ["sell","stock","history","settings","product","people","account"].forEach(id=>{ if($(id)) $(id).hidden = id!==name; });
  if(name==="history"){ loadSales(); loadDay(); }
  if(name==="stock") loadProducts();
  if(name==="settings"){ loadMeta(); fillLangSelects(); }
  if(name==="people") loadUsers();
  if(name==="sell") focusSearch();
}

async function api(path, opt){
  opt = opt || {};
  opt.credentials = "same-origin";
  const r = await fetch(path, opt);
  const data = await r.json();
  if(r.status===401){ showLogin(); throw new Error(data.error || "Please log in"); }
  if(!r.ok) throw new Error(data.error || "Request failed");
  return data;
}
function showLogin(){
  currentUser=null;
  $("login").style.display="flex";
  $("logoutBtn").style.display="none";
}
function applyRole(){
  const role = currentUser && currentUser.role;
  const allow = {
    sell: true,
    stock: role==="admin" || role==="manager",
    history: true,
    settings: true,
    people: role==="admin",
    product: role==="admin" || role==="manager",
    account: true
  };
  document.querySelectorAll("nav button").forEach(b=>{
    b.style.display = allow[b.dataset.tab] ? "" : "none";
  });
  $("who").textContent = currentUser ? (currentUser.name+" · "+currentUser.username+" · "+currentUser.role) : "";
  $("logoutBtn").style.display = currentUser ? "" : "none";
}
async function boot(){
  fillLangSelects();
  applyI18n();
  try{
    const me = await fetch("/api/me", {credentials:"same-origin"}).then(r=>r.json());
    if(me.user){
      currentUser=me.user;
      $("login").style.display="none";
      applyRole();
      loadProducts();
      return;
    }
  }catch(e){}
  showLogin();
}
async function doLogin(){
  $("loginErr").textContent="";
  try{
    const r = await api("/api/login", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ username:$("loginId").value, password:$("loginPw").value })
    });
    currentUser=r.user;
    $("login").style.display="none";
    $("loginPw").value="";
    applyRole();
    showTab("sell");
    loadProducts();
  }catch(err){ $("loginErr").textContent=err.message; }
}
async function doLogout(){
  try{ await api("/api/logout", {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"}); }catch(e){}
  showLogin();
}
async function loadUsers(){
  const users = await api("/api/users");
  $("userBody").innerHTML = users.map(u=>`
    <tr>
      <td>${esc(u.username)}</td><td>${esc(u.name)}</td><td>${esc(u.role)}</td>
      <td>
        <button class="ghost" onclick="resetUserPw(${u.id})">Reset password</button>
        <button class="ghost" onclick="deleteUser(${u.id})">Delete</button>
      </td>
    </tr>`).join("");
}
async function deleteUser(id){
  if(!confirm("Delete this login?")) return;
  try{
    await api("/api/users/"+id, {method:"DELETE"});
    toast("Deleted");
    loadUsers();
  }catch(err){ toast(err.message); }
}
async function resetUserPw(id){
  const pw = prompt("New password for this person:");
  if(!pw) return;
  try{
    await api("/api/users/"+id+"/password", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ password: pw })
    });
    toast("Password reset");
  }catch(err){ toast(err.message); }
}
async function changeMyPassword(){
  try{
    await api("/api/password", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({
        old: ($("oldPw")&&$("oldPw").value)||($("oldPw2")&&$("oldPw2").value)||"",
        new: ($("newPw")&&$("newPw").value)||($("newPw2")&&$("newPw2").value)||""
      })
    });
    ["oldPw","newPw","oldPw2","newPw2"].forEach(id=>{ if($(id)) $(id).value=""; });
    toast("Password changed");
  }catch(err){ toast(err.message); }
}
async function addUser(){
  try{
    await api("/api/users", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({
        username:$("uId").value, name:$("uName").value,
        role:$("uRole").value, password:$("uPw").value
      })
    });
    $("uId").value=$("uName").value=$("uPw").value="";
    toast("Account created");
    loadUsers();
  }catch(err){ toast(err.message); }
}

async function loadSummary(){
  const s = await api("/api/summary");
  $("stats").textContent = `${t("today")}: ${money(s.today_total)}  ·  ${s.today_count} ${t("sales")}  ·  ${s.product_count} ${t("items")}`;
  $("low").textContent = s.low_stock.length ? ("Low stock: " + s.low_stock.map(p=>p.name+" ("+p.stock+")").join(", ")) : "No low-stock alerts.";
}

async function loadProducts(){
  const q = $("search") ? $("search").value : "";
  products = await api("/api/products" + (q?("?q="+encodeURIComponent(q)):""));
  $("productList").innerHTML = products.map(p=>`
    <div class="item" onclick="addToCart(${p.id})">
      <div><b>${esc(p.name)}</b><div class="muted">${esc(p.barcode||"")} · <span class="stock ${stockClass(p.stock)}">${p.stock} ${t("inStock")}</span></div></div>
      <div class="price">${money(p.price)}</div>
    </div>`).join("") || "<p class='muted'>No products yet. Add some in Stock.</p>";
  $("stockBody").innerHTML = products.map(p=>`
    <tr class="clickable" onclick="openProduct(${p.id})">
      <td>${esc(p.name)}</td><td>${esc(p.barcode||"")}</td>
      <td>${money(p.price)}</td>
      <td class="stock ${stockClass(p.stock)}">${p.stock}</td>
      <td onclick="event.stopPropagation()">
        <button class="ghost" onclick="adjust(${p.id},1)">+1</button>
        <button class="ghost" onclick="adjust(${p.id},-1)">-1</button>
        <button class="ghost" onclick="promptQty(${p.id})">+/- qty</button>
        <button class="ghost" onclick="openProduct(${p.id})">Open</button>
        <button class="ghost" onclick="deleteProduct(${p.id})">Del</button>
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
    </div>`).join("") || `<p class='muted'>${t("tapAdd")}</p>`;
  const tot = cartTotals();
  $("total").textContent = money(tot.payable);
  updateChange();
}
function chg(i,d){
  cart[i].qty += d;
  if(cart[i].qty<=0) cart.splice(i,1);
  renderCart();
}
function clearCart(){
  cart=[];
  if($("discRm")) $("discRm").value="0";
  if($("discPct")) $("discPct").value="0";
  renderCart();
}
function cartTotals(){
  const sub = cart.reduce((s,l)=>s+l.price*l.qty,0);
  let disc = Number(($("discRm")&&$("discRm").value)||0);
  const pct = Number(($("discPct")&&$("discPct").value)||0);
  if(!disc && pct) disc = sub * pct / 100;
  if(disc<0) disc=0;
  if(disc>sub) disc=sub;
  disc=Math.round(disc*100)/100;
  return { sub:Math.round(sub*100)/100, disc, payable:Math.round((sub-disc)*100)/100 };
}
function updateChange(){
  const tot = cartTotals();
  const paid = Number($("paid").value||0);
  $("total").textContent = money(tot.payable);
  if($("discLine")){
    $("discLine").textContent = tot.disc ? ("Discount -"+money(tot.disc)+"  ·  was "+money(tot.sub)) : "";
  }
  $("change").textContent = t("change") + ": " + money(Math.max(0, paid-tot.payable));
  $("change").style.fontSize = "";
  $("change").style.fontWeight = "";
}
$("paid").addEventListener("input", updateChange);
$("search").addEventListener("input", ()=>loadProducts());
$("search").addEventListener("keydown", (e)=>{
  if(e.key==="Enter" && products.length===1){ addToCart(products[0].id); $("search").value=""; loadProducts(); }
});

async function checkout(){
  const tot = cartTotals();
  let paid = Number($("paid").value||0);
  if(!cart.length){ toast(t("emptyCart")); return; }
  if(!paid) paid = tot.payable;
  try{
    const sale = await api("/api/checkout", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({
        items: cart.map(l=>({product_id:l.product_id, qty:l.qty})),
        paid, discount: tot.disc
      })
    });
    toast("Sale #"+sale.id+" · change "+money(sale.change_amt));
    printReceipt(sale);
    cart=[]; $("paid").value="";
    if($("discRm")) $("discRm").value="0";
    if($("discPct")) $("discPct").value="0";
    renderCart(); loadProducts();
    $("change").textContent = t("change") + ": " + money(sale.change_amt);
    $("change").style.fontSize = "22px";
    $("change").style.fontWeight = "800";
    focusSearch();
  }catch(err){ toast(err.message); }
}

function resetForm(){
  $("pId").value="";
  $("pName").value=$("pCode").value="";
  $("pPrice").value=$("pCost").value=$("pStock").value="0";
  $("formTitle").textContent="Add product";
  $("saveBtn").textContent="Save product";
  $("openStockWrap").hidden=false;
  $("cancelEdit").hidden=true;
}
function editProduct(id){ openProduct(id); }

function openProduct(id){
  const p = products.find(x=>x.id===id);
  if(!p) return;
  $("dId").value=p.id;
  $("dName").value=p.name;
  $("dCode").value=p.barcode||"";
  $("dPrice").value=p.price;
  $("dCost").value=p.cost;
  $("dStock").value=p.stock;
  $("prodTitle").textContent=p.name;
  $("labelName").textContent=p.name;
  $("labelPrice").textContent=money(p.price);
  drawBarcode($("labelBars"), labelValue(p));
  $("labelCode").textContent=labelValue(p);
  showTab("product");
}
function labelValue(p){
  const c=(p.barcode||"").trim();
  return c || ("P"+String(p.id).padStart(6,"0"));
}
async function saveDetail(){
  const id=$("dId").value;
  try{
    await api("/api/products/"+id, {
      method:"PUT", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({
        name:$("dName").value, barcode:$("dCode").value,
        price:$("dPrice").value, cost:$("dCost").value
      })
    });
    const cur=Number($("dStock").value);
    const p=products.find(x=>x.id===Number(id));
    if(p && Number.isFinite(cur) && cur!==Number(p.stock)){
      await api("/api/products/"+id+"/adjust", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({ qty: cur-Number(p.stock), reason:"adjust" })
      });
    }
    toast("Saved");
    await loadProducts();
    openProduct(Number(id));
  }catch(err){ toast(err.message); }
}
async function adjustFromDetail(n){
  const id=Number($("dId").value);
  await adjust(id,n);
  const p=products.find(x=>x.id===id);
  if(p) openProduct(id);
}
async function deleteFromDetail(){
  const id=Number($("dId").value);
  await deleteProduct(id);
  showTab("stock");
}

const C39 = {"0":"nnnwwnwnn","1":"wnnwnnnnw","2":"nnwwnnnnw","3":"wnwwnnnnn","4":"nnnwwnnnw","5":"wnnwwnnnn","6":"nnwwwnnnn","7":"nnnwnnwnw","8":"wnnwnnwnn","9":"nnwwnnwnn","A":"wnnnnwnnw","B":"nnwnnwnnw","C":"wnwnnwnnn","D":"nnnnwwnnw","E":"wnnnwwnnn","F":"nnwnwwnnn","G":"nnnnnwwnw","H":"wnnnnwwnn","I":"nnwnnwwnn","J":"nnnnwwwnn","K":"wnnnnnnww","L":"nnwnnnnww","M":"wnwnnnnwn","N":"nnnnwnnww","O":"wnnnwnnwn","P":"nnwnwnnwn","Q":"nnnnnnwww","R":"wnnnnnwwn","S":"nnwnnnwwn","T":"nnnnwnwwn","U":"wwnnnnnnw","V":"nwwnnnnnw","W":"wwwnnnnnn","X":"nwnnwnnnw","Y":"wwnnwnnnn","Z":"nwwnwnnnn","-":"nwnnnnwnw",".":"wwnnnnwnn"," ":"nwwnnnwnn","*":"nwnnwnwnn","$":"nwnwnwnnn","/":"nwnwnnnwn","+":"nwnnnwnwn","%":"nnnwnwnwn"};
function drawBarcode(svg, text){
  let raw=String(text||"").toUpperCase().replace(/[^0-9A-Z.\-\/$+% ]/g,"");
  if(!raw) raw="0";
  const data="*"+raw+"*";
  const w=280, h=80, unit=2;
  svg.setAttribute("viewBox","0 0 "+w+" "+h);
  svg.innerHTML="";
  let x=4;
  for(const ch of data){
    const pat=C39[ch]; if(!pat) continue;
    if(x>4) x+=unit;
    let bar=true;
    for(const b of pat){
      const bw=unit*(b==="w"?3:1);
      if(bar){
        const r=document.createElementNS("http://www.w3.org/2000/svg","rect");
        r.setAttribute("x",x); r.setAttribute("y",8);
        r.setAttribute("width",bw); r.setAttribute("height",h-16);
        r.setAttribute("fill","#111");
        svg.appendChild(r);
      }
      x+=bw;
      bar=!bar;
    }
  }
}
function printLabel(){
  const id=Number($("dId").value);
  const p=products.find(x=>x.id===id);
  if(!p) return;
  const w=window.open("","label","width=420,height=360");
  if(!w){ toast("Allow pop-ups to print"); return; }
  w.document.write(`<!DOCTYPE html><html><head><title>Label</title>
    <style>body{font-family:sans-serif;text-align:center;padding:16px}svg{width:280px;height:80px}</style>
    </head><body>
    <div style="font-weight:700">${esc($("dName").value)}</div>
    ${$("labelBars").outerHTML}
    <div>${esc(labelValue({id:p.id,barcode:$("dCode").value}))}</div>
    <div style="font-weight:700;margin-top:6px">${money($("dPrice").value)}</div>
    <script>setTimeout(()=>{window.print();},200);<\/script>
    </body></html>`);
  w.document.close();
}
async function deleteProduct(id){
  const p = products.find(x=>x.id===id);
  if(!p) return;
  if(!confirm("Delete \""+p.name+"\"? Past sales keep the name. Stock of this item is removed.")) return;
  try{
    await api("/api/products/"+id, { method:"DELETE" });
    if($("pId").value==String(id)) resetForm();
    toast("Deleted");
    loadProducts();
  }catch(err){ toast(err.message); }
}
async function saveProduct(){
  const id = $("pId").value;
  const body = {
    name: $("pName").value, barcode: $("pCode").value,
    price: $("pPrice").value, cost: $("pCost").value, stock: $("pStock").value
  };
  try{
    if(id){
      await api("/api/products/"+id, {
        method:"PUT", headers:{"Content-Type":"application/json"},
        body: JSON.stringify(body)
      });
      toast("Updated");
    }else{
      await api("/api/products", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify(body)
      });
      toast("Product saved");
    }
    resetForm();
    loadProducts();
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
  const canVoid = currentUser && (currentUser.role==="admin" || currentUser.role==="manager");
  $("sales").innerHTML = sales.map(s=>`
    <div class="item" style="cursor:default">
      <div style="flex:1"><b>#${s.id} · ${money(s.total)} ${s.voided?"· VOID":""}</b>
        <div class="muted">${esc(s.created_at)} · ${esc(s.cashier||"-")} · ${s.items.map(i=>i.qty+"× "+i.name).join(", ")}</div>
      </div>
      ${canVoid && !s.voided ? `<button class="ghost" onclick="voidSale(${s.id})">Void</button>`:""}
      <button class="ghost" onclick="reprint(${s.id})">Receipt</button>
    </div>`).join("") || "<p class='muted'>No sales yet.</p>";
  window._sales = sales;
}

async function loadMeta(){
  const m = await api("/api/meta");
  $("verLine").textContent = "This PC version: " + m.version;
  $("updateUrl").value = m.update_url || "";
  if($("shopName")) $("shopName").value = m.shop_name || "ShopPOS";
  if($("shopPhone")) $("shopPhone").value = m.shop_phone || "";
  if($("shopAddress")) $("shopAddress").value = m.shop_address || "";
  if($("paper")) $("paper").value = m.paper || "58";
  window._shopName = m.shop_name || "ShopPOS";
  window._shopPhone = m.shop_phone || "";
  window._shopAddress = m.shop_address || "";
  window._paper = m.paper || "58";
  try{
    const c = await api("/api/changelog");
    $("changelog").textContent = c.text || "";
  }catch(err){
    $("changelog").textContent = "Could not load changelog.";
  }
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

function focusSearch(){
  if($("search") && $("sell") && !$("sell").hidden) $("search").focus();
}

function holdCart(){
  if(!cart.length){ toast(t("emptyCart")); return; }
  const label=prompt("Hold name (optional)", "Hold "+new Date().toLocaleTimeString());
  if(label===null) return;
  const holds=JSON.parse(localStorage.getItem("shoppos_holds")||"[]");
  holds.push({id:Date.now(), label:label||"Hold", cart:cart, at:new Date().toLocaleString()});
  localStorage.setItem("shoppos_holds", JSON.stringify(holds));
  cart=[]; $("paid").value=""; renderCart(); renderHolds(); toast("Sale parked");
}
function renderHolds(){
  const holds=JSON.parse(localStorage.getItem("shoppos_holds")||"[]");
  if(!$("holds")) return;
  $("holds").innerHTML = holds.length ? ("<div class='muted'>Held</div>"+holds.map(h=>`
    <div class="cart-line">
      <div style="flex:1"><b>${esc(h.label)}</b><div class="muted">${esc(h.at)}</div></div>
      <button class="ghost" onclick="resumeHold(${h.id})">Resume</button>
      <button class="ghost" onclick="dropHold(${h.id})">Del</button>
    </div>`).join("")) : "";
}
function resumeHold(id){
  const holds=JSON.parse(localStorage.getItem("shoppos_holds")||"[]");
  const h=holds.find(x=>x.id===id);
  if(!h) return;
  if(cart.length && !confirm("Replace current cart with the held sale?")) return;
  cart=h.cart||[];
  localStorage.setItem("shoppos_holds", JSON.stringify(holds.filter(x=>x.id!==id)));
  renderCart(); renderHolds(); toast("Resumed");
}
function dropHold(id){
  const holds=JSON.parse(localStorage.getItem("shoppos_holds")||"[]").filter(x=>x.id!==id);
  localStorage.setItem("shoppos_holds", JSON.stringify(holds));
  renderHolds();
}

async function voidSale(id){
  if(!confirm("Void sale #"+id+" and return stock?")) return;
  try{
    await api("/api/sales/"+id+"/void", {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"});
    toast("Voided");
    loadSales(); loadProducts(); loadDay();
  }catch(err){ toast(err.message); }
}
function reprint(id){
  const s=(window._sales||[]).find(x=>x.id===id);
  if(s) printReceipt(s);
}
function printReceipt(sale){
  const paper=window._paper||"58";
  const shop=window._shopName||"ShopPOS";
  const phone=window._shopPhone||"";
  const addr=window._shopAddress||"";
  const w=window.open("","receipt","width=420,height=640");
  if(!w){ toast("Allow pop-ups to print receipt"); return; }
  const width = paper==="a4" ? "180mm" : "58mm";
  const items=(sale.items||[]).map(i=>`<tr><td>${esc(i.name)} × ${i.qty}</td><td style="text-align:right">${money(i.line_total||i.price*i.qty)}</td></tr>`).join("");
  w.document.write(`<!DOCTYPE html><html><head><title>Receipt ${sale.id}</title>
    <style>
      body{font-family:ui-monospace,Consolas,monospace;margin:8px;width:${width};font-size:${paper==="a4"?"14px":"12px"}}
      h1{font-size:16px;margin:0 0 8px;text-align:center}
      table{width:100%}
      .tot{font-weight:700;border-top:1px dashed #000;margin-top:8px;padding-top:6px}
    </style></head><body>
    <h1>${esc(shop)}</h1>
    ${addr?`<div style="text-align:center">${esc(addr)}</div>`:""}
    ${phone?`<div style="text-align:center">${esc(phone)}</div>`:""}
    <div>Sale #${sale.id}</div>
    <div>${esc(sale.created_at||"")}</div>
    <div>${esc(sale.cashier||"")}</div>
    <table>${items}</table>
    <div class="tot">
      ${sale.subtotal && sale.discount ? `Subtotal ${money(sale.subtotal)}<br>Discount -${money(sale.discount)}<br>`:""}
      Total ${money(sale.total)}<br>Paid ${money(sale.paid)}<br>Change ${money(sale.change_amt)}
    </div>
    <div style="text-align:center;margin-top:10px">Thank you</div>
    <div style="text-align:center;margin-top:8px;font-size:10px">Pendekar's App — Blogs can die.. idea lives on.</div>
    <script>setTimeout(()=>window.print(),250);<\/script>
    </body></html>`);
  w.document.close();
}

async function loadDay(){
  if(!$("dayLine")) return;
  try{
    const d=await api("/api/day");
    $("dayLine").textContent = d.day+" · sales "+d.count+" · expected cash "+money(d.expected);
    window._day=d;
    $("closes").innerHTML = (d.closes||[]).map(c=>
      `${esc(c.created_at)} · expected ${money(c.expected)} · counted ${money(c.counted)} · diff ${money(c.diff)}`
    ).join("<br>") || "";
  }catch(e){
    $("dayLine").textContent="End of day (manager/admin)";
  }
}
async function closeDay(){
  try{
    const row=await api("/api/day-close", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ counted: $("counted").value })
    });
    toast("Saved · difference "+money(row.diff));
    loadDay();
  }catch(err){ toast(err.message); }
}
function printDay(){
  const d=window._day; if(!d){ toast("Open History first"); return; }
  const w=window.open("","day","width=480,height=640");
  if(!w){ toast("Allow pop-ups"); return; }
  w.document.write(`<!DOCTYPE html><html><head><title>Day ${d.day}</title></head><body style="font-family:sans-serif;padding:16px">
    <h2>${esc(window._shopName||"ShopPOS")} — ${esc(d.day)}</h2>
    <p>Sales: ${d.count}<br>Expected cash: ${money(d.expected)}<br>Paid in: ${money(d.paid)}</p>
    <script>setTimeout(()=>window.print(),200);<\/script></body></html>`);
  w.document.close();
}
async function saveReceiptPrefs(){
  try{
    await api("/api/config", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({
        shop_name:$("shopName").value, shop_phone:$("shopPhone").value,
        shop_address:$("shopAddress").value, paper:$("paper").value,
        update_url:$("updateUrl").value
      })
    });
    window._shopName=$("shopName").value||"ShopPOS";
    window._shopPhone=$("shopPhone").value||"";
    window._shopAddress=$("shopAddress").value||"";
    window._paper=$("paper").value||"58";
    toast("Receipt settings saved");
  }catch(err){ toast(err.message); }
}
async function downloadBackup(){
  try{
    const r=await fetch("/api/backup", {credentials:"same-origin"});
    if(r.status===401){ showLogin(); return; }
    if(!r.ok){ toast("Backup failed"); return; }
    const blob=await r.blob();
    const a=document.createElement("a");
    a.href=URL.createObjectURL(blob);
    a.download="shoppos-backup.db";
    a.click();
    toast("Backup downloaded — copy it to USB");
  }catch(err){ toast(err.message); }
}

renderHolds();
boot();
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
