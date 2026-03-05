// ============================================================
// 워크위드 GPS 출퇴근 - Google Apps Script (v2)
// ============================================================
// 설치 방법:
// 1. 구글시트 열기 (GPS출퇴근 스프레드시트)
// 2. 확장프로그램 > Apps Script 클릭
// 3. 이 코드 전체 붙여넣기
// 4. 배포 > 새 배포 > 유형: 웹 앱
//    - 실행 주체: 나
//    - 액세스 권한: 모든 사용자
// 5. 배포 후 URL 복사 → gps-checkin.html의 SCRIPT_URL에 붙여넣기
//
// 구글시트 "근로자" 탭 컬럼:
// A: 토큰 | B: 이름 | C: 전화번호 | D: 소속현장 | E: 기기ID
// ============================================================

function doPost(e) {
  try {
    const data = JSON.parse(e.postData.contents);
    const { token, type, lat, lng, manual, deviceId } = data;

    // 1. 근로자 탭에서 토큰으로 이름 조회
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const workerSheet = ss.getSheetByName('근로자');
    const workers = workerSheet.getDataRange().getValues();

    let workerName = null;
    let workerSite = null;
    let workerRow = -1;
    let savedDeviceId = null;
    for (let i = 1; i < workers.length; i++) {
      if (workers[i][0] === token) {
        workerName = workers[i][1];
        workerSite = workers[i][3]; // 소속현장
        savedDeviceId = workers[i][4] || ''; // 기기ID (E열)
        workerRow = i + 1; // 시트 행번호 (1-based)
        break;
      }
    }

    if (!workerName) {
      return jsonResponse({ success: false, error: '등록되지 않은 토큰' });
    }

    // 2. 기기 바인딩 확인
    let deviceMismatch = false;
    if (deviceId) {
      if (!savedDeviceId) {
        // 첫 체크인 — 기기ID 저장
        workerSheet.getRange(workerRow, 5).setValue(deviceId);
      } else if (savedDeviceId !== deviceId) {
        deviceMismatch = true;
      }
    }

    // 3. 중복 체크인 방지
    const recordSheet = ss.getSheetByName('체크인기록');
    const now = new Date();
    const dateStr = Utilities.formatDate(now, 'Asia/Seoul', 'yyyy-MM-dd');
    const timeStr = Utilities.formatDate(now, 'Asia/Seoul', 'HH:mm:ss');

    const records = recordSheet.getDataRange().getValues();
    for (let i = 1; i < records.length; i++) {
      // 같은 날짜 + 같은 이름 + 같은 구분이면 중복
      if (records[i][0] === dateStr && records[i][1] === workerName && records[i][2] === type) {
        return jsonResponse({
          success: false,
          error: `이미 ${type} 처리되었습니다 (${records[i][3]})`,
          duplicate: true,
          existingTime: records[i][3]
        });
      }
    }

    // 퇴근 시 출근 기록 필수
    if (type === '퇴근') {
      const hasCheckin = records.some(r => r[0] === dateStr && r[1] === workerName && r[2] === '출근');
      if (!hasCheckin) {
        return jsonResponse({ success: false, error: '출근 기록이 없습니다. 먼저 출근을 눌러주세요.' });
      }
    }

    // 4. 현장 탭에서 가장 가까운 현장 찾기
    let nearestSite = '위치없음';
    let nearestDist = 99999;

    if (lat && lng && !manual) {
      const siteSheet = ss.getSheetByName('현장');
      const sites = siteSheet.getDataRange().getValues();

      for (let i = 1; i < sites.length; i++) {
        const siteName = sites[i][0];
        const siteLat = parseFloat(sites[i][1]);
        const siteLng = parseFloat(sites[i][2]);

        if (!siteLat || !siteLng) continue;

        const dist = calcDistance(lat, lng, siteLat, siteLng);
        if (dist < nearestDist) {
          nearestDist = Math.round(dist);
          nearestSite = siteName;
        }
      }
    }

    // 5. 상태 결정
    let status = manual ? '수동' : (nearestDist <= 500 ? '정상' : '현장외');
    if (deviceMismatch) status += '/기기불일치';

    // 6. 체크인기록 탭에 기록
    recordSheet.appendRow([
      dateStr,
      workerName,
      type,
      timeStr,
      lat || '',
      lng || '',
      nearestSite,
      manual ? '' : nearestDist,
      status
    ]);

    return jsonResponse({
      success: true,
      name: workerName,
      site: nearestSite,
      distance: nearestDist,
      status: status,
      time: timeStr,
      deviceMismatch: deviceMismatch
    });

  } catch (err) {
    return jsonResponse({ success: false, error: err.message });
  }
}

// GET 요청 처리 (토큰 확인 + 이력 조회)
function doGet(e) {
  try {
    const token = e.parameter.token;
    const action = e.parameter.action || 'verify';

    if (!token) {
      return jsonResponse({ success: false, error: '토큰 없음' });
    }

    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const workerSheet = ss.getSheetByName('근로자');
    const workers = workerSheet.getDataRange().getValues();

    // 토큰으로 근로자 이름 찾기
    let workerName = null;
    let workerSite = null;
    for (let i = 1; i < workers.length; i++) {
      if (workers[i][0] === token) {
        workerName = workers[i][1];
        workerSite = workers[i][3];
        break;
      }
    }

    if (!workerName) {
      return jsonResponse({ success: false, error: '등록되지 않은 토큰' });
    }

    // 토큰 확인만
    if (action === 'verify') {
      // 오늘 체크인 상태도 함께 반환
      const recordSheet = ss.getSheetByName('체크인기록');
      const records = recordSheet.getDataRange().getValues();
      const today = Utilities.formatDate(new Date(), 'Asia/Seoul', 'yyyy-MM-dd');

      let todayCheckin = null;
      let todayCheckout = null;
      for (let i = 1; i < records.length; i++) {
        if (records[i][0] === today && records[i][1] === workerName) {
          if (records[i][2] === '출근') todayCheckin = records[i][3]; // 시간
          if (records[i][2] === '퇴근') todayCheckout = records[i][3];
        }
      }

      return jsonResponse({
        success: true,
        name: workerName,
        site: workerSite,
        todayCheckin: todayCheckin,
        todayCheckout: todayCheckout
      });
    }

    // 이번 달 출근 이력 조회
    if (action === 'history') {
      const recordSheet = ss.getSheetByName('체크인기록');
      const records = recordSheet.getDataRange().getValues();
      const now = new Date();
      const yearMonth = Utilities.formatDate(now, 'Asia/Seoul', 'yyyy-MM');

      const history = [];
      for (let i = 1; i < records.length; i++) {
        if (records[i][1] === workerName && String(records[i][0]).startsWith(yearMonth)) {
          history.push({
            날짜: records[i][0],
            구분: records[i][2],
            시간: records[i][3],
            현장명: records[i][6],
            상태: records[i][8]
          });
        }
      }

      // 날짜별로 출근/퇴근 합치기
      const dailyMap = {};
      history.forEach(h => {
        if (!dailyMap[h.날짜]) dailyMap[h.날짜] = {};
        dailyMap[h.날짜][h.구분] = { 시간: h.시간, 현장명: h.현장명, 상태: h.상태 };
      });

      const daily = Object.keys(dailyMap).sort().reverse().map(date => ({
        날짜: date,
        출근시간: dailyMap[date]['출근'] ? dailyMap[date]['출근'].시간 : '',
        퇴근시간: dailyMap[date]['퇴근'] ? dailyMap[date]['퇴근'].시간 : '',
        현장명: (dailyMap[date]['출근'] || dailyMap[date]['퇴근']).현장명,
        상태: (dailyMap[date]['출근'] || dailyMap[date]['퇴근']).상태
      }));

      return jsonResponse({
        success: true,
        name: workerName,
        month: yearMonth,
        workDays: daily.length,
        history: daily
      });
    }

    return jsonResponse({ success: false, error: '알 수 없는 action' });

  } catch (err) {
    return jsonResponse({ success: false, error: err.message });
  }
}

// JSON 응답 헬퍼
function jsonResponse(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

// Haversine 공식 - 두 GPS 좌표 간 거리 계산 (미터)
function calcDistance(lat1, lon1, lat2, lon2) {
  const R = 6371000;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
            Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) *
            Math.sin(dLon / 2) * Math.sin(dLon / 2);
  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  return R * c;
}

function toRad(deg) {
  return deg * (Math.PI / 180);
}
