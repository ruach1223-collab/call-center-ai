"""
오토셀 반자동 발주 도우미

기능:
  - 도매꾹 상품 페이지 브라우저 자동 오픈
  - 고객 배송지 클립보드 자동 복사
  - Windows 알림 (소리 + 토스트)
  - 미처리 주문 일괄 처리
"""

import webbrowser
import subprocess
import platform
import requests


def copy_to_clipboard(text):
    """텍스트를 클립보드에 복사"""
    if platform.system() == 'Windows':
        process = subprocess.Popen(['clip'], stdin=subprocess.PIPE)
        process.communicate(text.encode('utf-16le'))
    else:
        # macOS/Linux 폴백
        try:
            process = subprocess.Popen(['pbcopy'], stdin=subprocess.PIPE)
            process.communicate(text.encode('utf-8'))
        except FileNotFoundError:
            print(f"  [클립보드 실패] 수동 복사 필요")
            return False
    return True


def play_alert_sound():
    """주문 알림 소리"""
    if platform.system() == 'Windows':
        import winsound
        # 3회 반복 비프음
        for _ in range(3):
            winsound.Beep(1000, 300)
            winsound.Beep(1500, 300)


def show_notification(title, message):
    """Windows 토스트 알림"""
    if platform.system() == 'Windows':
        try:
            from ctypes import windll
            windll.user32.MessageBeep(0x00000040)  # MB_ICONINFORMATION 소리
        except Exception:
            pass
        # PowerShell 토스트 알림
        try:
            ps_cmd = (
                f'[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, '
                f'ContentType = WindowsRuntime] > $null; '
                f'$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(0); '
                f'$text = $template.GetElementsByTagName("text"); '
                f'$text[0].AppendChild($template.CreateTextNode("{title}")) > $null; '
                f'$text[1].AppendChild($template.CreateTextNode("{message}")) > $null; '
                f'$notify = [Windows.UI.Notifications.ToastNotification]::new($template); '
                f'[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("AutoSell").Show($notify)'
            )
            subprocess.Popen(
                ['powershell', '-Command', ps_cmd],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except Exception:
            pass


def send_telegram(message):
    """텔레그램으로 알림 메시지 발송"""
    try:
        from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            return False
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        }, timeout=10)
        return resp.ok
    except Exception as e:
        print(f"  [텔레그램 오류] {e}")
        return False


def send_telegram_with_button(message, callback_data, button_text="발주하기"):
    """인라인 버튼 포함 텔레그램 메시지 발송"""
    try:
        from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            return False
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": f"🛒 {button_text}", "callback_data": callback_data}
                ]]
            },
        }, timeout=10)
        return resp.ok
    except Exception as e:
        print(f"  [텔레그램 오류] {e}")
        return False


def get_telegram_chat_id():
    """봇에 온 메시지에서 chat_id 자동 추출"""
    try:
        from config import TELEGRAM_BOT_TOKEN
        if not TELEGRAM_BOT_TOKEN:
            print("[오류] TELEGRAM_BOT_TOKEN이 설정되지 않았습니다.")
            return None
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data.get('ok') and data.get('result'):
            chat_id = data['result'][-1]['message']['chat']['id']
            return str(chat_id)
        else:
            print("[오류] 봇에 메시지를 먼저 보내주세요.")
            return None
    except Exception as e:
        print(f"[오류] chat_id 조회 실패: {e}")
        return None


def format_shipping_info(order):
    """배송지 정보를 복사용 텍스트로 포맷"""
    name = order['receiver_name'] or ''
    addr = order['receiver_address'] or ''
    return f"{name}\n{addr}"


def open_domeggook_product(domeggook_id):
    """도매꾹 상품 페이지 브라우저 오픈"""
    if domeggook_id:
        url = f"https://domeggook.com/{domeggook_id}"
        webbrowser.open(url)
        return True
    return False


def process_single_order(order, db):
    """
    주문 1건 반자동 발주 처리

    1. 주문 정보 표시
    2. 도매꾹 상품 페이지 오픈
    3. 배송지 클립보드 복사
    4. 사용자 확인 대기
    5. 상태 업데이트
    """
    print(f"\n{'='*60}")
    print(f"  주문 처리: {order['coupang_order_id']}")
    print(f"{'='*60}")
    print(f"  상품: {order['product_name']}")
    print(f"  수량: {order['quantity']}개")
    print(f"  금액: {order['order_price']:,}원")
    print(f"  수취인: {order['receiver_name']}")
    print(f"  주소: {order['receiver_address']}")

    if order['domeggook_id']:
        product = db.get_product_by_domeggook_id(order['domeggook_id'])
        if product:
            print(f"\n  [도매꾹 상품]")
            print(f"  ID: {product['domeggook_id']}")
            print(f"  상품명: {product['domeggook_name'][:50]}")
            print(f"  도매가: {product['domeggook_price']:,}원")
            print(f"  마진: {product['margin']:,}원")

        # 1. 브라우저 오픈
        print(f"\n  → 도매꾹 상품 페이지 열기...")
        open_domeggook_product(order['domeggook_id'])

        # 2. 배송지 클립보드 복사
        shipping_text = format_shipping_info(order)
        copy_to_clipboard(shipping_text)
        print(f"  → 배송지 클립보드 복사 완료!")
        print(f"     {order['receiver_name']} / {order['receiver_address'][:30]}")
    else:
        print(f"\n  [경고] 도매꾹 상품 매칭 안됨 - 수동 확인 필요")

    # 3. 사용자 입력 대기
    print(f"\n  발주 후 아래 중 하나를 입력하세요:")
    print(f"    Enter  = 발주 완료 (상태 → 발주)")
    print(f"    s      = 건너뛰기")
    print(f"    q      = 전체 중단")

    choice = input("  >> ").strip().lower()

    if choice == 'q':
        return 'quit'
    elif choice == 's':
        print(f"  → 건너뜀")
        return 'skip'
    else:
        # 발주 완료
        db.update_order_status(order['coupang_order_id'], 'ordered')
        print(f"  → 발주 완료! (상태: ordered)")

        # 운송장 바로 입력할지 확인
        tracking = input("  운송장 번호 (없으면 Enter): ").strip()
        if tracking:
            db.update_order_tracking(order['coupang_order_id'], tracking)
            print(f"  → 운송장 저장: {tracking}")
        return 'done'


def process_pending_orders(db):
    """미처리 주문 일괄 발주 처리"""
    pending = db.get_pending_orders()

    if not pending:
        print("\n미처리 주문이 없습니다.")
        return

    print(f"\n{'='*60}")
    print(f"  미처리 주문 {len(pending)}건 발주 시작")
    print(f"{'='*60}")

    done_count = 0
    skip_count = 0

    for i, order in enumerate(pending):
        print(f"\n  --- [{i+1}/{len(pending)}] ---")
        result = process_single_order(order, db)

        if result == 'quit':
            print(f"\n발주 중단.")
            break
        elif result == 'done':
            done_count += 1
        elif result == 'skip':
            skip_count += 1

    print(f"\n{'='*60}")
    print(f"  발주 결과: 완료 {done_count}건 / 건너뜀 {skip_count}건 / 전체 {len(pending)}건")
    print(f"{'='*60}")

    # 운송장 미입력 건 안내
    still_pending = db.get_orders(status='ordered')
    no_tracking = [o for o in still_pending if not o['tracking_number']]
    if no_tracking:
        print(f"\n  [안내] 운송장 미입력 {len(no_tracking)}건:")
        for o in no_tracking:
            print(f"    주문 {o['coupang_order_id']}: {o['product_name'][:30]}")
        print(f"\n  운송장 입력: python main.py db track <주문번호> <운송장번호>")
