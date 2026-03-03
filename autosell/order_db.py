"""
오토셀 주문/상품 추적 DB 모듈
SQLite 기반, 서버 불필요

테이블:
  products - 도매꾹↔쿠팡 상품 매핑 (중복 등록 방지)
  orders   - 쿠팡 주문 추적 (상태 관리, 운송장 입력)
"""

import sqlite3
import os
import threading
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), 'autosell.db')

PRODUCTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domeggook_id TEXT NOT NULL,
    domeggook_name TEXT NOT NULL,
    domeggook_price INTEGER NOT NULL,
    domeggook_url TEXT,
    coupang_seller_product_id TEXT,
    coupang_product_name TEXT,
    sale_price INTEGER,
    margin INTEGER,
    shipping_fee INTEGER DEFAULT 3500,
    bundle_qty INTEGER DEFAULT 1,
    image_url TEXT,
    registered_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active', 'stopped', 'deleted')),
    UNIQUE(domeggook_id)
);
"""

ORDERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coupang_order_id TEXT NOT NULL,
    coupang_vendor_item_id TEXT,
    shipment_box_id TEXT,
    product_name TEXT,
    quantity INTEGER DEFAULT 1,
    order_price INTEGER,
    receiver_name TEXT,
    receiver_phone TEXT,
    receiver_postcode TEXT,
    receiver_address TEXT,
    domeggook_id TEXT,
    domeggook_order_id TEXT,
    tracking_number TEXT,
    status TEXT NOT NULL DEFAULT 'new'
        CHECK(status IN ('new', 'ordered', 'shipped', 'delivered', 'cancelled')),
    ordered_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE(coupang_order_id, coupang_vendor_item_id)
);
"""


class OrderDB:
    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        self._local = threading.local()
        self._init_db()

    def _get_conn(self):
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path, timeout=30)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
        return self._local.conn

    def _init_db(self):
        conn = self._get_conn()
        conn.executescript(PRODUCTS_SCHEMA + ORDERS_SCHEMA)
        # 기존 DB 마이그레이션: bundle_qty 컬럼 추가
        try:
            conn.execute("ALTER TABLE products ADD COLUMN bundle_qty INTEGER DEFAULT 1")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # 이미 존재
        conn.commit()

    def close(self):
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None

    # ========== 상품 (Products) ==========

    def product_exists(self, domeggook_id):
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM products WHERE domeggook_id = ?", (str(domeggook_id),)
        ).fetchone()
        return row is not None

    def add_product(self, domeggook_id, domeggook_name, domeggook_price,
                    coupang_seller_product_id, coupang_product_name,
                    sale_price, margin, image_url='', domeggook_url='',
                    shipping_fee=3500, bundle_qty=1):
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO products
                   (domeggook_id, domeggook_name, domeggook_price, domeggook_url,
                    coupang_seller_product_id, coupang_product_name,
                    sale_price, margin, shipping_fee, bundle_qty, image_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(domeggook_id), domeggook_name, domeggook_price, domeggook_url,
                 str(coupang_seller_product_id), coupang_product_name,
                 sale_price, margin, shipping_fee, bundle_qty, image_url)
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_product_by_domeggook_id(self, domeggook_id):
        conn = self._get_conn()
        return conn.execute(
            "SELECT * FROM products WHERE domeggook_id = ?", (str(domeggook_id),)
        ).fetchone()

    def match_order_to_product(self, seller_product_name):
        conn = self._get_conn()
        # active 먼저 검색, 없으면 stopped도 포함
        row = conn.execute(
            "SELECT * FROM products WHERE coupang_product_name = ? AND status = 'active'",
            (seller_product_name,)
        ).fetchone()
        if row:
            return row
        row = conn.execute(
            "SELECT * FROM products WHERE coupang_product_name = ? AND status IN ('active', 'stopped')",
            (seller_product_name,)
        ).fetchone()
        if row:
            return row
        row = conn.execute(
            "SELECT * FROM products WHERE coupang_product_name LIKE ? AND status IN ('active', 'stopped')",
            (f"%{seller_product_name[:50]}%",)
        ).fetchone()
        return row

    def update_product_status(self, domeggook_id, status):
        conn = self._get_conn()
        conn.execute(
            "UPDATE products SET status = ?, updated_at = datetime('now', 'localtime') WHERE domeggook_id = ?",
            (status, str(domeggook_id))
        )
        conn.commit()

    def update_product_price(self, domeggook_id, new_sale_price, new_margin):
        conn = self._get_conn()
        conn.execute(
            """UPDATE products SET sale_price = ?, margin = ?,
               updated_at = datetime('now', 'localtime')
               WHERE domeggook_id = ?""",
            (new_sale_price, new_margin, str(domeggook_id))
        )
        conn.commit()

    def update_product_wholesale(self, domeggook_id, new_price, new_shipping_fee=None):
        """도매가/배송비 변경 반영"""
        conn = self._get_conn()
        if new_shipping_fee is not None:
            conn.execute(
                """UPDATE products SET domeggook_price = ?, shipping_fee = ?,
                   updated_at = datetime('now', 'localtime')
                   WHERE domeggook_id = ?""",
                (new_price, new_shipping_fee, str(domeggook_id))
            )
        else:
            conn.execute(
                """UPDATE products SET domeggook_price = ?,
                   updated_at = datetime('now', 'localtime')
                   WHERE domeggook_id = ?""",
                (new_price, str(domeggook_id))
            )
        conn.commit()

    def get_all_products(self, status=None, limit=100):
        conn = self._get_conn()
        if status:
            rows = conn.execute(
                "SELECT * FROM products WHERE status = ? ORDER BY registered_at DESC LIMIT ?",
                (status, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM products ORDER BY registered_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return rows

    def get_product_count(self):
        conn = self._get_conn()
        total = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM products WHERE status='active'").fetchone()[0]
        stopped = conn.execute("SELECT COUNT(*) FROM products WHERE status='stopped'").fetchone()[0]
        deleted = conn.execute("SELECT COUNT(*) FROM products WHERE status='deleted'").fetchone()[0]
        return {'total': total, 'active': active, 'stopped': stopped, 'deleted': deleted}

    # ========== 주문 (Orders) ==========

    def order_exists(self, coupang_order_id, vendor_item_id=''):
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM orders WHERE coupang_order_id = ? AND coupang_vendor_item_id = ?",
            (str(coupang_order_id), str(vendor_item_id))
        ).fetchone()
        return row is not None

    def add_order(self, coupang_order_id, vendor_item_id='', shipment_box_id='',
                  product_name='', quantity=1, order_price=0,
                  receiver_name='', receiver_phone='', receiver_postcode='',
                  receiver_address='',
                  domeggook_id=None, ordered_at=''):
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO orders
                   (coupang_order_id, coupang_vendor_item_id, shipment_box_id,
                    product_name, quantity, order_price,
                    receiver_name, receiver_phone, receiver_postcode, receiver_address,
                    domeggook_id, ordered_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(coupang_order_id), str(vendor_item_id), str(shipment_box_id),
                 product_name, quantity, order_price,
                 receiver_name, receiver_phone, receiver_postcode, receiver_address,
                 str(domeggook_id) if domeggook_id else None, ordered_at)
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def update_order_tracking(self, coupang_order_id, tracking_number):
        conn = self._get_conn()
        conn.execute(
            """UPDATE orders SET tracking_number = ?, updated_at = datetime('now', 'localtime')
               WHERE coupang_order_id = ?""",
            (tracking_number, str(coupang_order_id))
        )
        conn.commit()

    def update_order_status(self, coupang_order_id, status):
        conn = self._get_conn()
        conn.execute(
            """UPDATE orders SET status = ?, updated_at = datetime('now', 'localtime')
               WHERE coupang_order_id = ?""",
            (status, str(coupang_order_id))
        )
        conn.commit()

    def get_orders(self, status=None, limit=100):
        conn = self._get_conn()
        if status:
            rows = conn.execute(
                "SELECT * FROM orders WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return rows

    def get_pending_orders(self):
        return self.get_orders(status='new', limit=1000)

    def get_order_by_coupang_id(self, coupang_order_id, vendor_item_id=None):
        """쿠팡 주문번호(+vendor_item_id)로 주문 1건 조회"""
        conn = self._get_conn()
        if vendor_item_id:
            row = conn.execute(
                "SELECT * FROM orders WHERE coupang_order_id = ? AND coupang_vendor_item_id = ?",
                (str(coupang_order_id), str(vendor_item_id))
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM orders WHERE coupang_order_id = ? ORDER BY created_at DESC LIMIT 1",
                (str(coupang_order_id),)
            ).fetchone()
        return row

    def get_order_stats(self):
        conn = self._get_conn()
        total = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        new = conn.execute("SELECT COUNT(*) FROM orders WHERE status='new'").fetchone()[0]
        ordered = conn.execute("SELECT COUNT(*) FROM orders WHERE status='ordered'").fetchone()[0]
        shipped = conn.execute("SELECT COUNT(*) FROM orders WHERE status='shipped'").fetchone()[0]
        delivered = conn.execute("SELECT COUNT(*) FROM orders WHERE status='delivered'").fetchone()[0]
        cancelled = conn.execute("SELECT COUNT(*) FROM orders WHERE status='cancelled'").fetchone()[0]
        return {'total': total, 'new': new, 'ordered': ordered,
                'shipped': shipped, 'delivered': delivered, 'cancelled': cancelled}
