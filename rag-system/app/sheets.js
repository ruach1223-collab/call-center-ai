// 구글시트 연동 모듈 — Google Sheets API → SQLite 동기화
// 각 시트가 별도 스프레드시트 + 실제 탭명 + 헤더 행 위치 반영
const { google } = require('googleapis');

// 시트별 설정 (각각 다른 스프레드시트)
const SHEET_CONFIG = {
  전체명단: {
    spreadsheetId: '1NAdjz94E9buyrBwiDAdKVwjaUTz6-MZyRBivPJqArII',
    range: '신규근로자!B3:N',  // B열부터 (A열은 빈칸), 3행부터
    headerRow: 0,
    table: 'gs_전체명단',
    columns: [
      '성명 TEXT', '성별 TEXT', '생년월일 TEXT', '주민등록번호 TEXT',
      '연락처 TEXT', '은행명 TEXT', '계좌번호 TEXT', '사는곳 TEXT',
      '지원경로 TEXT', '자차 TEXT', '미성년취업동의 TEXT',
      '채용일 TEXT', '사업종료일 TEXT'
    ]
  },
  견적서: {
    spreadsheetId: '17RdVVZWJKgsUQ4AKkYkiy7U40kbUCWJO_JI4hZPztAs',
    range: '★견적서발급번호!A2:G',  // 2행부터 (1행은 제목)
    headerRow: 0,
    table: 'gs_견적서',
    columns: [
      '견적번호 TEXT', '견적번호발행일 TEXT', '수신인 TEXT',
      '견적서제목 TEXT', '담당자 TEXT', '비고 TEXT', '계약체결 TEXT'
    ]
  },
  일투입현황: {
    spreadsheetId: '1WnxiYdT0CGKJQVQjoXelQ08eVmDfQjf2tThwQJtnETs',
    // 현재 월 탭 (동적으로 결정)
    range: null,
    headerRow: 0,
    table: 'gs_일투입현황',
    columns: [
      '투입일자 TEXT', '업체 TEXT', '투입구인인원 TEXT',
      '요청인원 TEXT', '투입인원명단 TEXT'
    ]
  },
  영업문의현황: {
    spreadsheetId: '1IGDXNuJpJd102TWXkKblAXz8fHaKV0EbYWTo68oFqn8',
    range: '영업 문의 List!A2:M',  // 2행부터
    headerRow: 0,
    table: 'gs_영업문의현황',
    columns: [
      'NO TEXT', '접수일 TEXT', '회사명 TEXT', '담당자명 TEXT',
      '연락처 TEXT', '이메일 TEXT', '유입경로 TEXT', '문의유형 TEXT',
      '문의내용 TEXT', '담당자2 TEXT', '진행상태 TEXT',
      '처리내용 TEXT', '비고 TEXT'
    ]
  },
  GPS출퇴근: {
    spreadsheetId: 'GPS_CHECKIN_SPREADSHEET_ID_HERE', // 구글시트 생성 후 ID 입력
    range: '체크인기록!A2:I',  // 2행부터
    headerRow: 0,
    table: 'gs_GPS출퇴근',
    columns: [
      '날짜 TEXT', '이름 TEXT', '구분 TEXT', '시간 TEXT',
      '위도 TEXT', '경도 TEXT', '현장명 TEXT', '거리 TEXT', '상태 TEXT'
    ]
  }
};

/**
 * Google Sheets API 인증 (서비스 계정)
 */
function getAuthClient() {
  const keyPath = process.env.GOOGLE_SERVICE_KEY || '/data/google-service-key.json';
  try {
    const auth = new google.auth.GoogleAuth({
      keyFile: keyPath,
      scopes: ['https://www.googleapis.com/auth/spreadsheets.readonly']
    });
    return auth;
  } catch (err) {
    console.error('[시트] 인증 실패:', err.message);
    return null;
  }
}

/**
 * 일투입현황 탭 이름 결정 (26년 3월 형식)
 */
function getCurrentMonthTab() {
  const now = new Date();
  const y = String(now.getFullYear()).slice(2); // 26
  const m = now.getMonth() + 1; // 3
  return `${y}년 ${m}월`;
}

/**
 * 구글시트에서 데이터 읽기
 * @param {string} sheetName - SHEET_CONFIG 키
 * @returns {Array<Array<string>>} rows
 */
async function fetchSheetData(sheetName) {
  const config = SHEET_CONFIG[sheetName];
  if (!config) throw new Error(`시트 설정 없음: ${sheetName}`);

  const auth = getAuthClient();
  if (!auth) throw new Error('Google 인증 실패 — 서비스 계정 키 확인');

  // 일투입현황은 현재 월 탭 자동 선택
  let range = config.range;
  if (sheetName === '일투입현황') {
    const tab = getCurrentMonthTab();
    range = `${tab}!A4:E`; // 4행부터 (1~3행은 제목/합계)
  }

  const sheets = google.sheets({ version: 'v4', auth });
  const res = await sheets.spreadsheets.values.get({
    spreadsheetId: config.spreadsheetId,
    range
  });

  return res.data.values || [];
}

/**
 * SQLite 테이블 생성 (없으면)
 * @param {import('better-sqlite3').Database} db
 */
function createSheetTables(db) {
  for (const [name, config] of Object.entries(SHEET_CONFIG)) {
    const cols = config.columns.join(', ');
    db.exec(`DROP TABLE IF EXISTS ${config.table}`);
    const sql = `CREATE TABLE IF NOT EXISTS ${config.table} (id INTEGER PRIMARY KEY AUTOINCREMENT, ${cols})`;
    try {
      db.exec(sql);
      console.log(`[시트] 테이블 생성: ${config.table}`);
    } catch (err) {
      console.error(`[시트] 테이블 생성 오류 (${config.table}):`, err.message);
    }
  }
}

/**
 * 특정 시트 데이터를 SQLite로 동기화
 * @param {import('better-sqlite3').Database} db
 * @param {string} sheetName
 * @returns {object} 동기화 결과
 */
async function syncSheet(db, sheetName) {
  const config = SHEET_CONFIG[sheetName];
  if (!config) return { error: `시트 설정 없음: ${sheetName}` };

  try {
    const rows = await fetchSheetData(sheetName);
    if (rows.length < 2) return { sheet: sheetName, rows: 0, message: '데이터 없음' };

    // 첫 행은 헤더, 나머지가 데이터
    const data = rows.slice(1).filter(row => row.some(cell => cell && cell.trim()));

    // 테이블 비우고 새로 삽입
    db.exec(`DELETE FROM ${config.table}`);

    const colNames = config.columns.map(c => c.split(' ')[0]);
    const placeholders = colNames.map(() => '?').join(',');
    const insertSQL = `INSERT INTO ${config.table} (${colNames.join(',')}) VALUES (${placeholders})`;
    const stmt = db.prepare(insertSQL);

    const insertMany = db.transaction((dataRows) => {
      for (const row of dataRows) {
        const padded = colNames.map((_, i) => (row[i] || '').trim());
        stmt.run(padded);
      }
    });

    insertMany(data);
    console.log(`[시트] ${sheetName} 동기화 완료: ${data.length}건`);
    return { sheet: sheetName, rows: data.length, success: true };

  } catch (err) {
    console.error(`[시트] ${sheetName} 동기화 오류:`, err.message);
    return { sheet: sheetName, error: err.message };
  }
}

/**
 * 전체 시트 동기화
 * @param {import('better-sqlite3').Database} db
 */
async function syncAllSheets(db) {
  console.log('[시트] 전체 동기화 시작');
  const results = {};
  for (const sheetName of Object.keys(SHEET_CONFIG)) {
    results[sheetName] = await syncSheet(db, sheetName);
  }
  console.log('[시트] 전체 동기화 완료');
  return results;
}

module.exports = {
  SHEET_CONFIG,
  createSheetTables,
  fetchSheetData,
  syncSheet,
  syncAllSheets
};
