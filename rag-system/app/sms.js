// 알리고 SMS 발송 모듈
// API 문서: https://smartsms.aligo.in/admin/api/spec.html
const https = require('https');
const querystring = require('querystring');

/**
 * 알리고 SMS 단건/단체 발송
 * @param {string|string[]} receivers - 수신 번호 (배열이면 단체 발송)
 * @param {string} message - 메시지 내용 (SMS: 90바이트, LMS: 2000바이트)
 * @param {object} options - { title: LMS 제목 }
 * @returns {Promise<object>} 알리고 응답
 */
async function sendSMS(receivers, message, options = {}) {
  const apiKey = process.env.ALIGO_API_KEY;
  const userId = process.env.ALIGO_USER_ID;
  const sender = process.env.ALIGO_SENDER;

  if (!apiKey || !userId || !sender) {
    throw new Error('알리고 환경변수 미설정 (ALIGO_API_KEY, ALIGO_USER_ID, ALIGO_SENDER)');
  }

  // 수신자 처리 (배열 → 콤마 구분)
  const receiverStr = Array.isArray(receivers) ? receivers.join(',') : receivers;

  // 메시지 길이에 따라 SMS/LMS 자동 선택
  const byteLength = Buffer.byteLength(message, 'euc-kr');
  const msgType = byteLength > 90 ? 'LMS' : 'SMS';

  const postData = querystring.stringify({
    key: apiKey,
    user_id: userId,
    sender: sender,
    receiver: receiverStr,
    msg: message,
    msg_type: msgType,
    title: options.title || (msgType === 'LMS' ? '워크위드 알림' : ''),
    testmode_yn: process.env.ALIGO_TEST_MODE || 'N' // 테스트 시 'Y'
  });

  return new Promise((resolve, reject) => {
    const req = https.request({
      hostname: 'apis.aligo.in',
      path: '/send/',
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Content-Length': Buffer.byteLength(postData)
      }
    }, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try {
          const result = JSON.parse(data);
          if (result.result_code === '1') {
            resolve({
              success: true,
              msgId: result.msg_id,
              count: result.success_cnt,
              type: msgType
            });
          } else {
            reject(new Error(`알리고 오류: ${result.message} (코드: ${result.result_code})`));
          }
        } catch (err) {
          reject(new Error('알리고 응답 파싱 실패: ' + data));
        }
      });
    });

    req.on('error', reject);
    req.write(postData);
    req.end();
  });
}

/**
 * 결근 대체인력 요청 문자 발송
 * @param {string} siteName - 현장명 (예: 서울식품)
 * @param {Array<{성명: string, 연락처: string}>} workers - 대체인력 목록
 * @returns {Promise<object>} 발송 결과
 */
async function sendBackupRequest(siteName, workers) {
  const validWorkers = workers.filter(w => w.연락처 && w.연락처.trim());
  if (validWorkers.length === 0) {
    throw new Error('연락처가 있는 대체인력이 없습니다');
  }

  const receivers = validWorkers.map(w => w.연락처.replace(/-/g, ''));
  const message = `[워크위드] ${siteName} 현장 금일 인원이 필요합니다. 출근 가능하시면 회신 부탁드립니다. (워크위드 담당자)`;

  const result = await sendSMS(receivers, message);

  return {
    ...result,
    siteName,
    sentTo: validWorkers.map(w => ({ name: w.성명, phone: w.연락처 })),
    sentAt: new Date().toISOString()
  };
}

/**
 * 알리고 잔여 건수 조회
 * @returns {Promise<object>} { remain_cnt: 남은 건수 }
 */
async function checkBalance() {
  const apiKey = process.env.ALIGO_API_KEY;
  const userId = process.env.ALIGO_USER_ID;

  if (!apiKey || !userId) {
    throw new Error('알리고 환경변수 미설정');
  }

  const postData = querystring.stringify({
    key: apiKey,
    user_id: userId,
  });

  return new Promise((resolve, reject) => {
    const req = https.request({
      hostname: 'apis.aligo.in',
      path: '/remain/',
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Content-Length': Buffer.byteLength(postData)
      }
    }, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try {
          resolve(JSON.parse(data));
        } catch {
          reject(new Error('알리고 응답 파싱 실패'));
        }
      });
    });

    req.on('error', reject);
    req.write(postData);
    req.end();
  });
}

module.exports = { sendSMS, sendBackupRequest, checkBalance };
