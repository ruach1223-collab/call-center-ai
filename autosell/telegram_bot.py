"""
오토셀 텔레그램 봇 - 원격 제어 + 원클릭 발주

사용법:
  python telegram_bot.py

텔레그램 명령어:
  /status      - DB 현황 (등록 상품/주문 통계)
  /orders      - 최근 주문 목록
  /products    - 등록 상품 목록
  /register    - 키워드 일괄 등록 실행
  /batch 500   - 대량 등록 (카테고리당 500개)
  /monitor on  - 주문 모니터링 시작
  /monitor off - 주문 모니터링 중지
  /help        - 명령어 안내

인라인 버튼:
  [발주하기]   - 주문 알림의 버튼 클릭 → 도매꾹 자동 발주 (결제 직전까지)
"""

import time
import threading
import requests
import traceback
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from order_db import OrderDB
from order_helper import send_telegram

BOT_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
POLL_INTERVAL = 2  # 텔레그램 메시지 확인 간격 (초)

_db = None
_monitor_thread = None
_monitor_running = False

# Selenium 세션 재사용 + 동시 발주 방지
_orderer = None
_orderer_lock = threading.Lock()


def get_db():
    global _db
    if _db is None:
        _db = OrderDB()
    return _db


def send_msg(text):
    """텔레그램 메시지 전송"""
    try:
        url = f"{BOT_URL}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }, timeout=10)
        return resp.ok
    except Exception as e:
        print(f"[전송 오류] {e}")
        return False


def answer_callback(callback_query_id, text=""):
    """콜백 쿼리 즉시 응답 (3초 타임아웃 방지)"""
    try:
        url = f"{BOT_URL}/answerCallbackQuery"
        resp = requests.post(url, json={
            "callback_query_id": callback_query_id,
            "text": text,
        }, timeout=5)
        return resp.ok
    except Exception as e:
        print(f"[콜백 응답 오류] {e}")
        return False


def edit_message(chat_id, message_id, new_text):
    """기존 메시지 수정 (버튼 제거 + 텍스트 변경)"""
    try:
        url = f"{BOT_URL}/editMessageText"
        resp = requests.post(url, json={
            "chat_id": chat_id,
            "message_id": message_id,
            "text": new_text,
            "parse_mode": "HTML",
        }, timeout=10)
        return resp.ok
    except Exception as e:
        print(f"[메시지 수정 오류] {e}")
        return False


def handle_order_callback(callback_query):
    """발주 콜백 처리 (백그라운드 스레드에서 실행)"""
    global _orderer

    cb_id = callback_query['id']
    data = callback_query.get('data', '')
    msg = callback_query.get('message', {})
    chat_id = str(msg.get('chat', {}).get('id', ''))
    message_id = msg.get('message_id')
    original_text = msg.get('text', '')

    # 본인 chat_id만 처리
    if chat_id != TELEGRAM_CHAT_ID:
        answer_callback(cb_id, "권한 없음")
        return

    # callback_data 파싱: "order:{coupang_order_id}:{vendor_item_id}"
    parts = data.split(':')
    if len(parts) < 3 or parts[0] != 'order':
        answer_callback(cb_id, "잘못된 콜백 데이터")
        return

    coupang_order_id = parts[1]
    vendor_item_id = parts[2]

    # 1. 즉시 콜백 응답 (텔레그램 3초 타임아웃 방지)
    answer_callback(cb_id, "발주 시작합니다...")

    # 2. 메시지를 "발주 진행중..."으로 편집 (버튼 제거)
    edit_message(chat_id, message_id, f"{original_text}\n\n⏳ <b>발주 진행중...</b>")

    # 3. 백그라운드에서 Selenium 발주
    def _do_order():
        global _orderer
        try:
            db = get_db()
            order = db.get_order_by_coupang_id(coupang_order_id, vendor_item_id)

            if not order:
                edit_message(chat_id, message_id,
                             f"{original_text}\n\n❌ 주문 정보를 찾을 수 없습니다.")
                return

            # 중복 발주 방지: 이미 처리된 주문이면 건너뜀
            if order['status'] != 'new':
                edit_message(chat_id, message_id,
                             f"{original_text}\n\n⚠️ 이미 처리된 주문입니다 (상태: {order['status']})")
                return

            if not order['domeggook_id']:
                edit_message(chat_id, message_id,
                             f"{original_text}\n\n❌ 도매꾹 매칭 상품이 없습니다.")
                return

            # Selenium 세션 초기화 (최초 1회 또는 세션 만료시)
            with _orderer_lock:
                from auto_order import DomeggookOrderer
                if _orderer is None or _orderer.driver is None:
                    _orderer = DomeggookOrderer()
                    if not _orderer.start_browser():
                        _orderer = None
                        edit_message(chat_id, message_id,
                                     f"{original_text}\n\n❌ 브라우저 시작 실패")
                        return
                    if not _orderer.login():
                        _orderer.close()
                        _orderer = None
                        edit_message(chat_id, message_id,
                                     f"{original_text}\n\n❌ 도매꾹 로그인 실패")
                        return

            # 묶음 상품 수량 계산
            product = db.get_product_by_domeggook_id(order['domeggook_id'])
            bundle_qty = product['bundle_qty'] if product and product.get('bundle_qty') else 1
            supplier_qty = bundle_qty * order['quantity']

            # 발주 실행 (Lock으로 동시 발주 방지)
            with _orderer_lock:
                result = _orderer.place_order(
                    product_id=order['domeggook_id'],
                    quantity=supplier_qty,
                    receiver_name=order['receiver_name'],
                    receiver_phone=order['receiver_phone'] or '',
                    receiver_address=order['receiver_address'] or '',
                    receiver_postcode=order['receiver_postcode'] or '',
                )

            if result:
                db.update_order_status(coupang_order_id, 'ordered')
                status_text = "결제 대기" if result == 'PENDING_PAYMENT' else "배송지 수동 입력 필요"
                edit_message(chat_id, message_id,
                             f"{original_text}\n\n✅ <b>발주 완료!</b> ({status_text})\n→ 브라우저에서 결제를 진행하세요")
                print(f"  [발주 완료] {coupang_order_id} → {result}")
            else:
                edit_message(chat_id, message_id,
                             f"{original_text}\n\n❌ <b>발주 실패</b>\n→ 수동으로 발주하세요")
                print(f"  [발주 실패] {coupang_order_id}")

        except Exception as e:
            print(f"  [발주 오류] {e}")
            traceback.print_exc()
            edit_message(chat_id, message_id,
                         f"{original_text}\n\n❌ 발주 중 오류: {e}")

    t = threading.Thread(target=_do_order, daemon=True)
    t.start()


def cmd_help():
    return (
        "<b>오토셀 텔레그램 봇</b>\n\n"
        "/status - DB 현황\n"
        "/orders - 최근 주문 목록\n"
        "/products - 등록 상품 목록\n"
        "/register - 키워드 일괄 등록\n"
        "/batch [수량] - 대량 등록 (기본 200)\n"
        "/monitor on - 주문 모니터링 시작\n"
        "/monitor off - 주문 모니터링 중지\n"
        "/help - 이 도움말\n\n"
        "💡 주문 알림의 [발주하기] 버튼으로 원클릭 발주!"
    )


def cmd_status():
    db = get_db()
    prod = db.get_product_count()
    order = db.get_order_stats()
    return (
        "<b>오토셀 DB 현황</b>\n\n"
        f"<b>[상품]</b>\n"
        f"  전체 등록: {prod['total']}개\n"
        f"  판매중: {prod['active']}개\n"
        f"  중지: {prod['stopped']}개\n"
        f"  삭제: {prod['deleted']}개\n\n"
        f"<b>[주문]</b>\n"
        f"  전체: {order['total']}건\n"
        f"  신규(미처리): {order['new']}건\n"
        f"  발주완료: {order['ordered']}건\n"
        f"  배송중: {order['shipped']}건\n"
        f"  배송완료: {order['delivered']}건\n"
        f"  취소: {order['cancelled']}건"
    )


def cmd_orders():
    db = get_db()
    orders = db.get_orders(limit=10)
    if not orders:
        return "주문이 없습니다."

    status_label = {
        'new': '[신규]', 'ordered': '[발주]', 'shipped': '[배송]',
        'delivered': '[완료]', 'cancelled': '[취소]'
    }

    lines = [f"<b>최근 주문 {len(orders)}건</b>\n"]
    for o in orders:
        s = status_label.get(o['status'], o['status'])
        lines.append(
            f"{s} {o['coupang_order_id']}\n"
            f"  {o['product_name'][:30]} x{o['quantity']}\n"
            f"  {o['order_price']:,}원 | {o['receiver_name']}"
        )
    return "\n".join(lines)


def cmd_products():
    db = get_db()
    products = db.get_all_products(limit=10)
    if not products:
        return "등록된 상품이 없습니다."

    prod_count = db.get_product_count()
    lines = [f"<b>등록 상품 (최근 10개 / 전체 {prod_count['total']}개)</b>\n"]
    for p in products:
        lines.append(
            f"  {p['domeggook_id']} | {p['status']} | "
            f"도매 {p['domeggook_price']:,}원 → 판매 {p['sale_price']:,}원 | "
            f"마진 {p['margin']:,}원\n"
            f"  {p['coupang_product_name'][:35]}"
        )
    return "\n".join(lines)


def cmd_register():
    """키워드 일괄 등록 (백그라운드)"""
    def _run():
        try:
            send_msg("키워드 일괄 등록 시작합니다...")
            from domeggook import DomeggookScraper
            from main import batch_register
            kw_list = DomeggookScraper.POPULAR_KEYWORDS
            batch_register(keywords=kw_list, max_per_category=100, workers=5)

            db = get_db()
            prod = db.get_product_count()
            send_msg(
                f"키워드 등록 완료!\n"
                f"현재 등록 상품: {prod['total']}개 (판매중: {prod['active']}개)"
            )
        except Exception as e:
            send_msg(f"등록 오류: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return f"키워드 {len(__import__('domeggook').DomeggookScraper.POPULAR_KEYWORDS)}개로 등록 시작! 완료되면 알려드립니다."


def cmd_batch(args):
    """대량 등록 (백그라운드)"""
    max_per = 200
    if args:
        try:
            max_per = int(args[0])
        except ValueError:
            pass

    def _run():
        try:
            send_msg(f"대량 등록 시작 (카테고리당 {max_per}개)...")
            from main import batch_register
            batch_register(max_per_category=max_per, workers=5)

            db = get_db()
            prod = db.get_product_count()
            send_msg(
                f"대량 등록 완료!\n"
                f"현재 등록 상품: {prod['total']}개 (판매중: {prod['active']}개)"
            )
        except Exception as e:
            send_msg(f"등록 오류: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return f"대량 등록 시작 (카테고리당 {max_per}개)! 완료되면 알려드립니다."


def cmd_monitor(args):
    """주문 모니터링 on/off"""
    global _monitor_thread, _monitor_running

    sub = args[0].lower() if args else 'on'

    if sub == 'off':
        if _monitor_running:
            _monitor_running = False
            return "주문 모니터링 중지됨."
        return "모니터링이 실행 중이 아닙니다."

    if _monitor_running:
        return "이미 모니터링 중입니다."

    _monitor_running = True

    def _run():
        global _monitor_running
        from coupang import CoupangAPI
        from datetime import datetime, timedelta

        db = get_db()
        existing = db.get_orders(limit=10000)
        seen = {row['coupang_order_id'] for row in existing}

        send_msg("주문 모니터링 시작! (5분 간격)")

        while _monitor_running:
            try:
                api = CoupangAPI()
                end_date = datetime.now()
                start_date = end_date - timedelta(days=1)

                result = api.get_orders(
                    start_date.strftime('%Y-%m-%d'),
                    end_date.strftime('%Y-%m-%d'),
                    status='ACCEPT'
                )

                if result and result.get('data'):
                    for order in result['data']:
                        oid = str(order.get('orderId', ''))
                        if oid and oid not in seen:
                            seen.add(oid)
                            receiver = order.get('receiver', {})
                            items = order.get('orderItems', [])
                            names = [i.get('sellerProductName', '?')[:30] for i in items]
                            total_price = sum(i.get('orderPrice', 0) for i in items)

                            for item in items:
                                vid = str(item.get('vendorItemId', ''))
                                if not db.order_exists(oid, vid):
                                    matched = db.match_order_to_product(item.get('sellerProductName', ''))
                                    db.add_order(
                                        coupang_order_id=oid,
                                        vendor_item_id=vid,
                                        shipment_box_id=str(item.get('shipmentBoxId', '')),
                                        product_name=item.get('sellerProductName', ''),
                                        quantity=item.get('shippingCount', 1),
                                        order_price=item.get('orderPrice', 0),
                                        receiver_name=receiver.get('name', ''),
                                        receiver_address=(receiver.get('addr1', '') + ' ' + receiver.get('addr2', '')).strip(),
                                        domeggook_id=matched['domeggook_id'] if matched else None,
                                        ordered_at=order.get('paidAt', ''),
                                    )

                            send_msg(
                                f"<b>신규 주문!</b>\n\n"
                                f"주문번호: {oid}\n"
                                f"상품: {', '.join(names)}\n"
                                f"금액: {total_price:,}원\n"
                                f"수취인: {receiver.get('name', '?')}"
                            )

                time.sleep(300)
            except Exception as e:
                print(f"[모니터링 오류] {e}")
                time.sleep(300)

    _monitor_thread = threading.Thread(target=_run, daemon=True)
    _monitor_thread.start()
    return "주문 모니터링 시작! 신규 주문 시 알려드립니다."


def handle_message(text):
    """메시지 파싱 및 명령 실행"""
    text = text.strip()
    if not text.startswith('/'):
        return None

    parts = text.split()
    cmd = parts[0].lower()
    args = parts[1:]

    commands = {
        '/help': lambda: cmd_help(),
        '/start': lambda: cmd_help(),
        '/status': lambda: cmd_status(),
        '/orders': lambda: cmd_orders(),
        '/products': lambda: cmd_products(),
        '/register': lambda: cmd_register(),
        '/batch': lambda: cmd_batch(args),
        '/monitor': lambda: cmd_monitor(args),
    }

    handler = commands.get(cmd)
    if handler:
        try:
            return handler()
        except Exception as e:
            return f"오류 발생: {e}"
    else:
        return f"알 수 없는 명령: {cmd}\n/help 로 명령어를 확인하세요."


def run_bot():
    """텔레그램 봇 메인 루프 (Long Polling + 콜백 쿼리 처리)"""
    print("=" * 60)
    print("  오토셀 텔레그램 봇 시작 (원클릭 발주 지원)")
    print("  종료: Ctrl+C")
    print("=" * 60)

    offset = None

    # 시작 알림
    send_msg("오토셀 봇이 시작되었습니다!\n/help 로 명령어를 확인하세요.")

    while True:
        try:
            params = {
                "timeout": 30,
                "allowed_updates": ["message", "callback_query"],
            }
            if offset:
                params["offset"] = offset

            resp = requests.get(f"{BOT_URL}/getUpdates", params=params, timeout=35)
            data = resp.json()

            if not data.get('ok'):
                time.sleep(POLL_INTERVAL)
                continue

            for update in data.get('result', []):
                offset = update['update_id'] + 1

                # 콜백 쿼리 처리 (인라인 버튼 클릭)
                if 'callback_query' in update:
                    cb = update['callback_query']
                    cb_data = cb.get('data', '')
                    print(f"  [콜백] {cb_data}")

                    if cb_data.startswith('order:'):
                        handle_order_callback(cb)
                    else:
                        answer_callback(cb['id'], "알 수 없는 명령")
                    continue

                # 일반 메시지 처리
                msg = update.get('message', {})
                chat_id = str(msg.get('chat', {}).get('id', ''))
                text = msg.get('text', '')

                # 본인 chat_id만 응답
                if chat_id != TELEGRAM_CHAT_ID:
                    continue

                if not text:
                    continue

                print(f"  [수신] {text}")
                reply = handle_message(text)
                if reply:
                    send_msg(reply)
                    print(f"  [응답] {reply[:50]}...")

        except KeyboardInterrupt:
            print("\n봇 종료.")
            # Selenium 세션 정리
            global _orderer
            if _orderer:
                _orderer.close()
                _orderer = None
            send_msg("오토셀 봇이 종료되었습니다.")
            break
        except Exception as e:
            print(f"  [오류] {e}")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run_bot()
