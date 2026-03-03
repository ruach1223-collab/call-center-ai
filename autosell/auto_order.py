"""
자동 발주 시스템

1. 쿠팡 신규 주문 자동 감지
2. 수익성 체크 (손해 주문 경고)
3. 도매꾹 자동 발주 (Selenium)
4. 송장번호 쿠팡 자동 전송
5. 텔레그램 알림

사용법:
  python auto_order.py              - 자동 발주 시작 (모니터링 + 발주)
  python auto_order.py monitor      - 모니터링만 (발주 안함)
  python auto_order.py process      - 미처리 주문 일괄 발주
  python auto_order.py tracking     - 미입력 송장 확인/입력
"""

import sys
import io
import os
import time
import json
from datetime import datetime, timedelta

if not isinstance(sys.stdout, io.TextIOWrapper) or sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    except (AttributeError, ValueError):
        pass

from coupang import CoupangAPI
from order_db import OrderDB
from order_helper import send_telegram, send_telegram_with_button, play_alert_sound, show_notification, copy_to_clipboard
from config import (
    COUPANG_FEE_RATE, DEFAULT_SHIPPING_FEE, MIN_MARGIN_AMOUNT,
    AUTO_ORDER_CHECK_INTERVAL, DELIVERY_COMPANY_CODE,
)

# === 도매꾹 로그인 정보 ===
DOMEGGOOK_USER_ID = os.environ.get('DOMEGGOOK_USER_ID', '')
DOMEGGOOK_PASSWORD = os.environ.get('DOMEGGOOK_PASSWORD', '')

# === 자동 발주 설정 ===
AUTO_ORDER_ENABLED = True        # False면 알림만, 발주는 수동
PROFIT_WARN_THRESHOLD = 500      # 이 금액 이하 마진이면 경고
LOSS_AUTO_CANCEL = False         # True면 손해 주문 자동 취소 (위험)


class ProfitChecker:
    """주문 수익성 분석"""

    def check(self, product, order_price, quantity):
        """
        주문 건당 수익성 계산

        묶음 상품인 경우: 도매가 × bundle_qty × 주문수량
        예: 3개세트 상품 2건 주문 → 도매꾹에 6개 발주

        Returns: {
            'wholesale_cost': 도매 원가,
            'shipping_cost': 배송비,
            'coupang_fee': 쿠팡 수수료,
            'total_cost': 총 비용,
            'revenue': 매출 (주문가),
            'profit': 순이익,
            'profit_per_unit': 건당 이익,
            'is_loss': 손해 여부,
            'is_low_margin': 저마진 여부,
            'bundle_qty': 묶음 수량,
            'order_qty_to_supplier': 도매꾹 발주 수량,
        }
        """
        if not product:
            return None

        wholesale = product['domeggook_price']
        # 실제 배송비 우선, 없으면 기본값
        shipping = product.get('shipping_fee') or DEFAULT_SHIPPING_FEE
        bundle_qty = product.get('bundle_qty') or 1

        # 도매꾹 발주 수량: 묶음 수량 × 쿠팡 주문 수량
        order_qty_to_supplier = bundle_qty * quantity

        # 비용: 도매가 × 총발주수량 + 배송비 × 주문건수 + 쿠팡 수수료
        wholesale_cost = wholesale * order_qty_to_supplier
        coupang_fee = int(order_price * COUPANG_FEE_RATE)
        total_cost = wholesale_cost + (shipping * quantity) + coupang_fee

        profit = order_price - total_cost
        profit_per_unit = profit // quantity if quantity > 0 else profit

        return {
            'wholesale_cost': wholesale_cost,
            'shipping_cost': shipping * quantity,
            'coupang_fee': coupang_fee,
            'total_cost': total_cost,
            'revenue': order_price,
            'profit': profit,
            'profit_per_unit': profit_per_unit,
            'is_loss': profit <= 0,
            'is_low_margin': 0 < profit < PROFIT_WARN_THRESHOLD,
            'bundle_qty': bundle_qty,
            'order_qty_to_supplier': order_qty_to_supplier,
        }


class DomeggookOrderer:
    """도매꾹 Selenium 자동 발주"""

    def __init__(self):
        self.driver = None
        self.logged_in = False

    def start_browser(self, headless=False):
        """Chrome 브라우저 시작"""
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        options = Options()
        if headless:
            options.add_argument('--headless=new')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--window-size=1280,900')
        # 자동화 탐지 방지
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_experimental_option('excludeSwitches', ['enable-automation'])

        self.driver = webdriver.Chrome(options=options)
        self.driver.implicitly_wait(10)
        print("[브라우저] Chrome 시작")
        return True

    @staticmethod
    def _parse_phone(phone):
        """전화번호 → [prefix, middle, last] 분리

        예: '01012345678' → ['010', '1234', '5678']
            '010-1234-5678' → ['010', '1234', '5678']
            '0505-1234-5678' → ['0505', '1234', '5678']
        """
        phone = phone.replace('-', '').replace(' ', '').replace('+82', '0')
        if len(phone) == 12 and phone.startswith('050'):
            return [phone[:4], phone[4:8], phone[8:]]
        if len(phone) == 11:
            return [phone[:3], phone[3:7], phone[7:]]
        elif len(phone) == 10:
            return [phone[:3], phone[3:6], phone[6:]]
        elif len(phone) >= 7:
            return [phone[:3], phone[3:-4], phone[-4:]]
        return ['010', '', '']

    @staticmethod
    def _parse_address(full_address):
        """통합 주소를 (기본주소, 상세주소)로 분리

        쿠팡 주소 형식: "서울특별시 강남구 역삼로 123 아파트 101동 202호"
        → addr1: "서울특별시 강남구 역삼로 123"
        → addr2: "아파트 101동 202호"
        """
        import re
        addr = full_address.strip()
        if not addr:
            return ('', '')

        # 패턴: 도로명/지번 뒤에서 분리
        patterns = [
            r'(.*?\d+번지?)\s+(.+)',
            r'(.*?(?:로|길)\s+\d+[-~]?\d*)\s+(.+)',
            r'(.*?\d+[-~]?\d*)\s+((?:아파트|APT|빌라|오피스텔|빌딩|타워|마을|단지|상가|주택|연립|다세대|맨션|팰리스|파크|센터|플라자|스카이|더\s).+)',
            r'(.*?\d+[-~]?\d*)\s+(\(.+)',
        ]

        for pattern in patterns:
            m = re.match(pattern, addr, re.IGNORECASE)
            if m:
                return (m.group(1).strip(), m.group(2).strip())

        # 분리 실패시 공백 기준 70% 위치에서 분리
        split_point = len(addr) * 7 // 10
        space_pos = addr.rfind(' ', 0, split_point + 10)
        if space_pos > 0:
            return (addr[:space_pos].strip(), addr[space_pos:].strip())
        return (addr, '')

    def login(self, user_id=None, password=None):
        """도매꾹 로그인"""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        user_id = user_id or DOMEGGOOK_USER_ID
        password = password or DOMEGGOOK_PASSWORD

        if not user_id or not password:
            print("[오류] 도매꾹 로그인 정보가 없습니다.")
            print("  환경변수 설정: set DOMEGGOOK_USER_ID=아이디")
            print("  환경변수 설정: set DOMEGGOOK_PASSWORD=비밀번호")
            return False

        try:
            self.driver.get('https://domeggook.com/main/member/mem_formLogin.php')
            time.sleep(2)

            # 로그인 폼 (#lLoginFromWrap)
            id_input = self.driver.find_element(By.CSS_SELECTOR, '#lLoginFromWrap input[name="id"]')
            pw_input = self.driver.find_element(By.CSS_SELECTOR, '#lLoginFromWrap input[name="pass"]')

            id_input.clear()
            id_input.send_keys(user_id)
            pw_input.clear()
            pw_input.send_keys(password)

            # 로그인 버튼
            login_btn = self.driver.find_element(By.CSS_SELECTOR, '#lLoginFromWrap input.formSubmit')
            login_btn.click()
            time.sleep(3)

            # 로그인 확인 (성공시 메인 페이지로 이동)
            if 'formLogin' not in self.driver.current_url and 'login' not in self.driver.current_url.lower():
                self.logged_in = True
                print("[로그인] 도매꾹 로그인 성공")
                return True
            else:
                print("[오류] 로그인 실패 - 아이디/비밀번호 확인")
                return False

        except Exception as e:
            print(f"[오류] 로그인 중 오류: {e}")
            return False

    def place_order(self, product_id, quantity, receiver_name, receiver_phone,
                    receiver_address, receiver_postcode='', shop_name='오토셀',
                    receiver_message=''):
        """
        도매꾹 위탁배송(supply) 주문

        Flow:
        1. 상품 페이지 → JS로 상품 정보 추출
        2. AJAX로 장바구니 추가 (supply market + 소비자 정보)
        3. 주문 페이지 이동 → 결제 대기

        Returns: 'PENDING_PAYMENT' | 'PENDING_MANUAL_ADDRESS' | None
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        if not self.logged_in:
            print("[오류] 로그인 필요")
            return None

        try:
            # 1. 상품 페이지 이동
            self.driver.get(f'https://domeggook.com/{product_id}')
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '#lPay, #lPayBtnBuy'))
            )
            time.sleep(2)

            # 2. 상품 정보 추출 (JS)
            product_info = self.driver.execute_script("""
                if (!window.lItem) return null;

                var state = window.lItem.store ? window.lItem.store.getState() : {};
                var bac = window.lItem.baseAmtController || {};

                // sellerId 추출
                var sellerId = '';
                try {
                    var scripts = document.querySelectorAll('script');
                    for (var i = 0; i < scripts.length; i++) {
                        var m = scripts[i].textContent.match(/sellerId\\s*[:=]\\s*["']([^"']+)["']/);
                        if (m) { sellerId = m[1]; break; }
                    }
                } catch(e) {}

                // supply market 가능 여부
                var hasSupply = !!document.querySelector('.lBtn[data-market="supply"]');

                // 사용 가능한 첫 번째 옵션 코드
                var firstOptCode = null;
                var markets = hasSupply ? ['supply', 'dome'] : ['dome'];
                for (var mi = 0; mi < markets.length; mi++) {
                    var mk = markets[mi];
                    if (window.lItem.optController && window.lItem.optController[mk]) {
                        var ctrl = window.lItem.optController[mk].pay || window.lItem.optController[mk].sticky;
                        if (ctrl && ctrl.data) {
                            var codes = Object.keys(ctrl.data);
                            for (var ci = 0; ci < codes.length; ci++) {
                                var d = ctrl.data[codes[ci]];
                                if (d && parseInt(d.qty || 0) > 0 && d.sup !== '0') {
                                    firstOptCode = codes[ci];
                                    break;
                                }
                            }
                            if (firstOptCode) break;
                        }
                    }
                }

                return {
                    sellerId: sellerId,
                    hasSupply: hasSupply,
                    unitQty: bac.unitQty || 1,
                    firstOptCode: firstOptCode,
                    priceDome: bac.amtDome || 0,
                    priceSupply: bac.amtSupply || 0
                };
            """)

            if not product_info:
                print(f"  [오류] 상품 정보 추출 실패 (상품 ID: {product_id})")
                return None

            # 3. market 결정 (위탁배송 supply 우선)
            market = 'supply' if product_info.get('hasSupply') else 'dome'

            # 4. 최소 주문 수량 체크
            min_qty = product_info.get('unitQty', 1)
            actual_qty = max(quantity, min_qty)
            if actual_qty > quantity:
                print(f"  [주의] 최소 주문 수량 {min_qty}개 → {actual_qty}개로 조정")

            # 5. 전화번호/주소 파싱
            phone_parts = self._parse_phone(receiver_phone)
            addr1, addr2 = self._parse_address(receiver_address)
            postcode = receiver_postcode or ''

            opt_code = product_info.get('firstOptCode', '00')

            # 6. AJAX 장바구니 추가 (supply market + 소비자 정보)
            result = self.driver.execute_script("""
                var param = {
                    format: "json", mode: "add",
                    market: arguments[0],
                    no: arguments[1],
                    sellerId: arguments[2],
                    amt: 0,
                    qty: arguments[3]
                };

                // 옵션 설정
                var optCode = arguments[4];
                if (optCode) {
                    param['selectOpt[' + optCode + ']'] = arguments[3];
                }

                // 소비자 정보 (supply market 위탁배송)
                if (arguments[0] === 'supply') {
                    param['cons[shop]']    = arguments[5];
                    param['cons[name]']    = arguments[6];
                    param['cons[post]']    = arguments[7];
                    param['cons[addr1]']   = arguments[8];
                    param['cons[addr2]']   = arguments[9];
                    param['cons[mobile1]'] = arguments[10];
                    param['cons[mobile2]'] = arguments[11];
                    param['cons[mobile3]'] = arguments[12];
                    param['cons[phone1]']  = '';
                    param['cons[phone2]']  = '';
                    param['cons[phone3]']  = '';
                    param['cons[deliReq]'] = arguments[13];
                }

                var result = null;
                $.ajax({
                    type: 'post',
                    url: '/main/myBuy/order/my_cartIng.php',
                    data: param,
                    dataType: 'json',
                    async: false,
                    success: function(data) { result = data; },
                    error: function(xhr) { result = {res:'error', msg: xhr.status + ' ' + xhr.statusText}; }
                });
                return result;
            """,
                market,                                     # 0
                str(product_id),                            # 1
                product_info.get('sellerId', ''),           # 2
                actual_qty,                                 # 3
                opt_code,                                   # 4
                shop_name,                                  # 5
                receiver_name,                              # 6
                postcode,                                   # 7
                addr1,                                      # 8
                addr2,                                      # 9
                phone_parts[0],                             # 10
                phone_parts[1],                             # 11
                phone_parts[2],                             # 12
                receiver_message,                           # 13
            )

            if not result or result.get('res') != 'success':
                error_msg = result.get('msg', '알 수 없는 오류') if result else 'AJAX 호출 실패'
                print(f"  [오류] 장바구니 추가 실패: {error_msg}")

                # dome market 폴백 (supply 실패시)
                if market == 'supply':
                    print(f"  [재시도] dome market으로 재시도...")
                    return self._fallback_dome_order(
                        product_id, actual_qty, opt_code, product_info
                    )
                return None

            cart_no = result.get('no', '')
            print(f"  [장바구니] 추가 성공 (cart: {cart_no}, market: {market})")

            # 7. 주문 페이지로 이동
            order_url = f'https://domeggook.com/main/myBuy/order/my_orderInfoForm.php?checkedItem={cart_no}'
            self.driver.get(order_url)
            time.sleep(2)

            print(f"  [주문] 주문 페이지 이동 완료")
            print(f"  수취인: {receiver_name}")
            print(f"  주소: {addr1} {addr2}")
            print(f"  전화: {'-'.join(phone_parts)}")
            print(f"  → 결제를 진행하세요")

            return 'PENDING_PAYMENT'

        except Exception as e:
            print(f"  [오류] 주문 중 오류: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _fallback_dome_order(self, product_id, quantity, opt_code, product_info):
        """dome market 주문 폴백 (배송지 수동 입력 필요)"""
        try:
            result = self.driver.execute_script("""
                var param = {
                    format: "json", mode: "add",
                    market: "dome",
                    no: arguments[0],
                    sellerId: arguments[1],
                    amt: 0, qty: arguments[2]
                };
                if (arguments[3]) {
                    param['selectOpt[' + arguments[3] + ']'] = arguments[2];
                }
                var result = null;
                $.ajax({
                    type: 'post',
                    url: '/main/myBuy/order/my_cartIng.php',
                    data: param,
                    dataType: 'json',
                    async: false,
                    success: function(data) { result = data; },
                    error: function(xhr) { result = {res:'error', msg: xhr.statusText}; }
                });
                return result;
            """, str(product_id), product_info.get('sellerId', ''), quantity, opt_code)

            if result and result.get('res') == 'success':
                cart_no = result.get('no', '')
                self.driver.get(
                    f'https://domeggook.com/main/myBuy/order/my_orderInfoForm.php?checkedItem={cart_no}'
                )
                time.sleep(2)
                print(f"  [장바구니] dome market 추가 성공 (배송지 수동 입력 필요)")
                return 'PENDING_MANUAL_ADDRESS'
            print(f"  [오류] dome market도 실패")
            return None
        except Exception:
            return None

    def close(self):
        """브라우저 종료"""
        if self.driver:
            self.driver.quit()
            self.driver = None
            self.logged_in = False
            print("[브라우저] 종료")


class OrderMonitor:
    """쿠팡 주문 모니터링 + 자동 발주 프로세서"""

    def __init__(self):
        self.api = CoupangAPI()
        self.db = OrderDB()
        self.checker = ProfitChecker()
        self.orderer = None  # 필요시 초기화
        self.seen_orders = set()

        # 기존 주문 ID 로드
        existing = self.db.get_orders(limit=10000)
        self.seen_orders = {row['coupang_order_id'] for row in existing}

    def fetch_new_orders(self):
        """쿠팡에서 신규 주문 가져오기"""
        end_date = datetime.now()
        start_date = end_date - timedelta(days=2)

        result = self.api.get_orders(
            start_date.strftime('%Y-%m-%d'),
            end_date.strftime('%Y-%m-%d'),
            status='ACCEPT'
        )

        if not result or not result.get('data'):
            return []

        new_orders = []
        for order in result['data']:
            order_id = str(order.get('orderId', ''))
            if order_id and order_id not in self.seen_orders:
                new_orders.append(order)
                self.seen_orders.add(order_id)

        return new_orders

    def save_order(self, order):
        """주문을 DB에 저장"""
        order_id = str(order.get('orderId', ''))
        receiver = order.get('receiver', {})
        receiver_name = receiver.get('name', '')
        receiver_phone = receiver.get('safeNumber', '') or receiver.get('phone', '')
        receiver_postcode = str(receiver.get('postCode', ''))
        addr = (receiver.get('addr1', '') + ' ' + receiver.get('addr2', '')).strip()

        items = order.get('orderItems', [])
        saved = []

        for item in items:
            product_name = item.get('sellerProductName', '')
            vendor_item_id = str(item.get('vendorItemId', ''))
            shipment_box_id = str(item.get('shipmentBoxId', ''))
            quantity = item.get('shippingCount', 1)
            order_price = item.get('orderPrice', 0)

            # 이미 존재하면 스킵
            if self.db.order_exists(order_id, vendor_item_id):
                continue

            # 도매꾹 매칭
            matched = self.db.match_order_to_product(product_name)

            self.db.add_order(
                coupang_order_id=order_id,
                vendor_item_id=vendor_item_id,
                shipment_box_id=shipment_box_id,
                product_name=product_name,
                quantity=quantity,
                order_price=order_price,
                receiver_name=receiver_name,
                receiver_phone=receiver_phone,
                receiver_address=addr,
                domeggook_id=matched['domeggook_id'] if matched else None,
                ordered_at=order.get('paidAt', ''),
                receiver_postcode=receiver_postcode,
            )

            saved.append({
                'order_id': order_id,
                'product_name': product_name,
                'quantity': quantity,
                'order_price': order_price,
                'receiver_name': receiver_name,
                'receiver_phone': receiver_phone,
                'receiver_postcode': receiver_postcode,
                'receiver_address': addr,
                'domeggook_id': matched['domeggook_id'] if matched else None,
                'product': matched,
                'vendor_item_id': vendor_item_id,
                'shipment_box_id': shipment_box_id,
            })

        return saved

    def analyze_and_notify(self, order_info):
        """주문 수익성 분석 + 알림"""
        product = order_info['product']
        profit_info = self.checker.check(
            product, order_info['order_price'], order_info['quantity']
        )

        # 콘솔 출력
        print(f"\n{'='*60}")
        print(f"  신규 주문!")
        print(f"  주문번호: {order_info['order_id']}")
        print(f"  상품: {order_info['product_name'][:40]}")
        print(f"  수량: {order_info['quantity']}개")
        print(f"  주문금액: {order_info['order_price']:,}원")
        print(f"  수취인: {order_info['receiver_name']}")

        if product:
            print(f"  도매꾹: {product['domeggook_id']} (도매가: {product['domeggook_price']:,}원)")

        if profit_info:
            emoji = '🔴' if profit_info['is_loss'] else ('🟡' if profit_info['is_low_margin'] else '🟢')
            print(f"  {'─'*40}")
            print(f"  {emoji} 수익 분석:")
            print(f"    매출: {profit_info['revenue']:,}원")
            print(f"    도매 원가: {profit_info['wholesale_cost']:,}원")
            print(f"    배송비: {profit_info['shipping_cost']:,}원")
            print(f"    쿠팡 수수료: {profit_info['coupang_fee']:,}원")
            print(f"    순이익: {profit_info['profit']:,}원")
            if order_info['quantity'] > 1:
                print(f"    개당 이익: {profit_info['profit_per_unit']:,}원")
        print(f"{'='*60}")

        # 텔레그램 알림
        tg_lines = []
        if profit_info and profit_info['is_loss']:
            tg_lines.append("🔴 <b>손해 주문 발생!</b>")
        elif profit_info and profit_info['is_low_margin']:
            tg_lines.append("🟡 <b>저마진 주문</b>")
        else:
            tg_lines.append("🟢 <b>신규 주문!</b>")

        tg_lines.extend([
            f"주문: {order_info['order_id']}",
            f"상품: {order_info['product_name'][:35]}",
            f"수량: {order_info['quantity']}개 | 금액: {order_info['order_price']:,}원",
            f"수취인: {order_info['receiver_name']}",
            f"📍 {order_info['receiver_address'][:50]}",
        ])

        if profit_info:
            tg_lines.append(f"💰 순이익: {profit_info['profit']:,}원")

        if product and product.get('domeggook_id'):
            tg_lines.append(f"\n🛒 도매꾹: {product['domeggook_id']}")
            # 버튼 포함 알림 (콜백 데이터: order:{주문번호}:{vendor_item_id})
            callback_data = f"order:{order_info['order_id']}:{order_info.get('vendor_item_id', '')}"
            send_telegram_with_button("\n".join(tg_lines), callback_data, "발주하기")
        else:
            send_telegram("\n".join(tg_lines))

        # PC 알림
        play_alert_sound()
        show_notification(
            "오토셀 - 신규 주문!",
            f"{order_info['product_name'][:30]} / {order_info['receiver_name']}"
        )

        return profit_info

    def process_auto_order(self, order_info, profit_info):
        """
        자동 발주 처리

        수익성 OK → 도매꾹 자동 발주
        손해 → 경고만 (수동 판단)
        """
        if not AUTO_ORDER_ENABLED:
            return

        if profit_info and profit_info['is_loss']:
            print(f"  ⚠️ 손해 주문 → 자동 발주 건너뜀 (수동 확인 필요)")
            send_telegram(f"⚠️ 손해 주문 건너뜀\n주문: {order_info['order_id']}\n순이익: {profit_info['profit']:,}원\n→ 수동으로 발주 여부 결정하세요")
            return

        product = order_info['product']
        if not product or not product.get('domeggook_id'):
            print(f"  ⚠️ 도매꾹 매칭 실패 → 수동 처리 필요")
            return

        # Selenium 자동 발주
        if self.orderer is None:
            self.orderer = DomeggookOrderer()
            if not self.orderer.start_browser():
                self.orderer = None
                return
            if not self.orderer.login():
                self.orderer.close()
                self.orderer = None
                return

        # 묶음 상품: bundle_qty × 주문수량 만큼 도매꾹에 발주
        bundle_qty = product.get('bundle_qty') or 1
        supplier_qty = bundle_qty * order_info['quantity']

        result = self.orderer.place_order(
            product_id=product['domeggook_id'],
            quantity=supplier_qty,
            receiver_name=order_info['receiver_name'],
            receiver_phone=order_info['receiver_phone'],
            receiver_address=order_info['receiver_address'],
            receiver_postcode=order_info.get('receiver_postcode', ''),
        )

        if result:
            self.db.update_order_status(order_info['order_id'], 'ordered')
            print(f"  ✓ 발주 완료 (상태: {result})")
            send_telegram(f"✅ 자동 발주 완료\n주문: {order_info['order_id']}")
        else:
            print(f"  ✗ 발주 실패 → 수동 처리 필요")
            send_telegram(f"❌ 자동 발주 실패\n주문: {order_info['order_id']}\n→ 수동으로 발주하세요")

    def submit_tracking(self, coupang_order_id, vendor_item_id, shipment_box_id, tracking_number, quantity=1):
        """쿠팡에 송장번호 전송"""
        result = self.api.confirm_order(
            shipment_box_id=shipment_box_id,
            vendor_item_id=vendor_item_id,
            shipping_count=quantity,
            invoice_number=tracking_number,
            delivery_company_code=DELIVERY_COMPANY_CODE,
        )

        if result:
            self.db.update_order_status(coupang_order_id, 'shipped')
            self.db.update_order_tracking(coupang_order_id, tracking_number)
            print(f"  ✓ 송장 등록: {tracking_number}")
            return True
        else:
            print(f"  ✗ 송장 등록 실패")
            return False

    def run_monitor(self, interval=None, auto_order=True):
        """
        메인 모니터링 루프

        interval: 조회 간격 (초)
        auto_order: True면 자동 발주, False면 알림만
        """
        if interval is None:
            interval = AUTO_ORDER_CHECK_INTERVAL

        print(f"\n{'='*60}")
        print(f"  오토셀 자동 발주 시스템")
        print(f"{'='*60}")
        print(f"  모드: {'자동 발주' if auto_order else '모니터링만'}")
        print(f"  조회 간격: {interval}초 ({interval//60}분)")
        print(f"  손해 경고 기준: {PROFIT_WARN_THRESHOLD:,}원 이하")
        print(f"  종료: Ctrl+C")
        print(f"{'='*60}\n")

        cycle = 0
        while True:
            try:
                cycle += 1
                now = datetime.now().strftime('%H:%M:%S')

                # 신규 주문 확인
                new_orders = self.fetch_new_orders()

                if new_orders:
                    print(f"\n  [{now}] 🔔 신규 주문 {len(new_orders)}건 감지!")

                    for order in new_orders:
                        saved_items = self.save_order(order)

                        for item in saved_items:
                            profit_info = self.analyze_and_notify(item)

                            if auto_order:
                                self.process_auto_order(item, profit_info)
                else:
                    # 10분마다 상태 출력
                    if cycle % max(1, (600 // interval)) == 0:
                        stats = self.db.get_order_stats()
                        print(f"  [{now}] 상태 - 신규:{stats['new']} 발주:{stats['ordered']} 배송:{stats['shipped']} 완료:{stats['delivered']}")
                    else:
                        print(f"  [{now}] 대기중...", end='\r')

                time.sleep(interval)

            except KeyboardInterrupt:
                print(f"\n\n모니터링 종료.")
                if self.orderer:
                    self.orderer.close()
                break
            except Exception as e:
                print(f"\n  [오류] {e}")
                time.sleep(interval)


def process_pending():
    """미처리 주문 일괄 처리 (반자동)"""
    db = OrderDB()
    pending = db.get_pending_orders()

    if not pending:
        print("\n미처리 주문이 없습니다.")
        return

    checker = ProfitChecker()

    print(f"\n{'='*60}")
    print(f"  미처리 주문 {len(pending)}건")
    print(f"{'='*60}")

    for i, order in enumerate(pending):
        product = db.get_product_by_domeggook_id(order['domeggook_id']) if order['domeggook_id'] else None
        profit_info = checker.check(product, order['order_price'], order['quantity'])

        # 수익 이모지
        if profit_info:
            emoji = '🔴' if profit_info['is_loss'] else ('🟡' if profit_info['is_low_margin'] else '🟢')
            profit_str = f"{profit_info['profit']:,}원"
        else:
            emoji = '⚪'
            profit_str = '???'

        print(f"\n  [{i+1}/{len(pending)}] {emoji}")
        print(f"  주문: {order['coupang_order_id']}")
        print(f"  상품: {order['product_name'][:40]}")
        print(f"  수량: {order['quantity']}개 | 금액: {order['order_price']:,}원 | 순이익: {profit_str}")
        phone = order['receiver_phone'] if order['receiver_phone'] else ''
        print(f"  수취인: {order['receiver_name']} / {phone}")
        print(f"  주소: {order['receiver_address'][:50]}")

        if product:
            print(f"  도매꾹: https://domeggook.com/{product['domeggook_id']}")

            # 배송지 클립보드 복사 (이름 + 전화 + 주소)
            addr_text = f"{order['receiver_name']}\n{phone}\n{order['receiver_address']}"
            copy_to_clipboard(addr_text)
            print(f"  📋 배송지 클립보드 복사 완료")

            # 브라우저 오픈
            import webbrowser
            webbrowser.open(f"https://domeggook.com/{product['domeggook_id']}")

        print(f"\n  [Enter]=발주완료  [s]=건너뛰기  [c]=취소처리  [q]=중단")
        choice = input("  >> ").strip().lower()

        if choice == 'q':
            break
        elif choice == 'c':
            db.update_order_status(order['coupang_order_id'], 'cancelled')
            print(f"  → 취소 처리")
        elif choice == 's':
            print(f"  → 건너뜀")
        else:
            db.update_order_status(order['coupang_order_id'], 'ordered')
            print(f"  → 발주 완료!")

            tracking = input("  송장번호 (없으면 Enter): ").strip()
            if tracking:
                db.update_order_tracking(order['coupang_order_id'], tracking)
                # 쿠팡 송장 전송
                if order.get('shipment_box_id') and order.get('coupang_vendor_item_id'):
                    api = CoupangAPI()
                    result = api.confirm_order(
                        shipment_box_id=order['shipment_box_id'],
                        vendor_item_id=order['coupang_vendor_item_id'],
                        shipping_count=order['quantity'],
                        invoice_number=tracking,
                    )
                    if result:
                        db.update_order_status(order['coupang_order_id'], 'shipped')
                        print(f"  → 쿠팡 송장 전송 완료!")
                    else:
                        print(f"  → 쿠팡 송장 전송 실패 (수동 입력 필요)")


def manage_tracking():
    """송장번호 관리"""
    db = OrderDB()
    api = CoupangAPI()

    # 발주 완료 but 송장 미입력
    ordered = db.get_orders(status='ordered', limit=100)
    no_tracking = [o for o in ordered if not o['tracking_number']]

    if not no_tracking:
        print("\n송장 미입력 주문이 없습니다.")
        return

    print(f"\n{'='*60}")
    print(f"  송장 미입력 주문 {len(no_tracking)}건")
    print(f"{'='*60}")

    for i, order in enumerate(no_tracking):
        print(f"\n  [{i+1}] 주문: {order['coupang_order_id']}")
        print(f"       상품: {order['product_name'][:40]}")
        print(f"       수취인: {order['receiver_name']}")

        tracking = input("  송장번호 (Enter=건너뛰기, q=종료): ").strip()

        if tracking == 'q':
            break
        elif tracking:
            db.update_order_tracking(order['coupang_order_id'], tracking)

            # 쿠팡 전송
            if order.get('shipment_box_id') and order.get('coupang_vendor_item_id'):
                result = api.confirm_order(
                    shipment_box_id=order['shipment_box_id'],
                    vendor_item_id=order['coupang_vendor_item_id'],
                    shipping_count=order['quantity'],
                    invoice_number=tracking,
                )
                if result:
                    db.update_order_status(order['coupang_order_id'], 'shipped')
                    print(f"  ✓ 쿠팡 송장 전송 완료!")
                else:
                    print(f"  ✗ 쿠팡 전송 실패 (수동 입력 필요)")
            else:
                print(f"  → 송장 저장 완료 (쿠팡 전송 정보 부족 - 수동 입력)")


if __name__ == '__main__':
    args = sys.argv[1:]

    if not args or args[0] == 'auto':
        # 자동 발주 모드
        monitor = OrderMonitor()
        monitor.run_monitor(auto_order=AUTO_ORDER_ENABLED)

    elif args[0] == 'monitor':
        # 모니터링만 (발주 안함)
        monitor = OrderMonitor()
        monitor.run_monitor(auto_order=False)

    elif args[0] == 'process':
        # 미처리 주문 반자동 발주
        process_pending()

    elif args[0] == 'tracking':
        # 송장 관리
        manage_tracking()

    elif args[0] == 'test':
        # 테스트: 수익성 체크만
        db = OrderDB()
        checker = ProfitChecker()
        orders = db.get_orders(limit=10)
        for o in orders:
            product = db.get_product_by_domeggook_id(o['domeggook_id']) if o['domeggook_id'] else None
            info = checker.check(product, o['order_price'], o['quantity'])
            if info:
                emoji = '🔴' if info['is_loss'] else ('🟡' if info['is_low_margin'] else '🟢')
                print(f"  {emoji} {o['coupang_order_id']}: 매출 {info['revenue']:,} → 순이익 {info['profit']:,}원")
            else:
                print(f"  ⚪ {o['coupang_order_id']}: 매칭 실패")

    else:
        print("사용법:")
        print("  python auto_order.py              - 자동 발주 시작")
        print("  python auto_order.py monitor      - 모니터링만 (알림)")
        print("  python auto_order.py process      - 미처리 주문 반자동 발주")
        print("  python auto_order.py tracking     - 송장번호 관리")
        print("  python auto_order.py test         - 수익성 체크 테스트")
