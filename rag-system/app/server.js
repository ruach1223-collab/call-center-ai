const express = require('express');
const Database = require('better-sqlite3');
const cors = require('cors');
const https = require('https');
const path = require('path');
const cron = require('node-cron');
const { runAllChecks } = require('./compliance');
const { sendComplianceAlert } = require('./mailer');
const { createSheetTables, syncAllSheets, syncSheet } = require('./sheets');
const { sendBackupRequest, checkBalance } = require('./sms');

const app = express();
app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// SQLite DB 연결 (읽기/쓰기 — 구글시트 동기화용)
const DB_PATH = process.env.DB_PATH || '/data/walkwith.db';
let db;
try {
  db = new Database(DB_PATH, { fileMustExist: true });
  console.log(`[DB] 연결 성공: ${DB_PATH}`);
  // 구글시트 동기화용 테이블 생성
  createSheetTables(db);
} catch (err) {
  console.error(`[DB] 연결 실패: ${err.message}`);
}

// DB 스키마 정보 (Claude에게 전달)
const DB_SCHEMA = `
## SQLite DB 스키마 (워크위드 일용직 투입관리)

### 뷰 (Views) — 이것만 사용하세요 (테이블 아닌 뷰)
- 근로자
- 근태
- 인건비
- 지급

### 근로자 뷰 (약 5,387명) — 인적사항, 은행, 보험
컬럼: ID, 성명, 생년월일, 주민등록번호, 성별,
      은행명, 은행계좌번호,
      고용보험가입여부, 국민연금가입여부, 건강보험가입여부, 장기요양가입여부,
      지급구분(주급/월급/일급)
- 보험 가입여부 값: 'Y'(가입), 'N'(미가입), ''(빈값=미가입)
- 미가입 조건: WHERE 고용보험가입여부 != 'Y' (N이거나 빈값)

### 근태 뷰 (약 3,207건) — 근무 기록
컬럼: ID, 업쳬명, 날짜, 성명, 생년월일, 주민등록번호, 지급구분, 청구구분,
      근무시간, 주야(주/야), T(실근무시간), 잔업, 임금, 연차수당, 교통비, 청구액,
      지급여부, 비고, 파일명
- 주의: 컬럼명이 "업쳬명"(오타)입니다. 업체명이 아닌 업쳬명으로 쿼리하세요!
- 날짜 형식: ISO (예: 2026-01-05T00:00:00)
- 테스트 데이터: 업쳬명이 'a업체','b업체','c업체'인 것은 제외

### 인건비 뷰 (약 144건) — 거래처별 단가표
컬럼: ID, 업쳬명, 청구구분, 근무시간, 주야, T, 잔업, 임금, 청구액
- 주의: 컬럼명이 "업쳬명"(오타)입니다!

### 지급 뷰 (약 24건) — 급여 지급 기록
컬럼: ID, 지급구분, 시작일자, 종료일자, 성명, 생년월일, 은행명, 은행계좌번호,
      업쳬명, 임금합계, 고용보험, 사업소득세, 사대보험, 공제10, 공제합계, 실지급액,
      근무일수합계, 근무시간합계
- 주의: 컬럼명이 "업쳬명"(오타)입니다!

### 핵심 주의사항
- ⚠️ "업체명"이 아니라 "업쳬명"입니다! (원본 DB 오타)
- 테스트 데이터 제외: WHERE 업쳬명 NOT IN ('a업체','b업체','c업체')
- 날짜 비교: substr(날짜, 1, 7) = '2026-01'
- 은행 컬럼은 "은행명"이고 "은행"이 아닙니다

## 구글시트 연동 테이블 (gs_ 접두사)

### gs_전체명단 — 전체 근로자 명단 (연락처 포함)
컬럼: 성명, 성별, 생년월일, 주민등록번호, 연락처, 은행명, 계좌번호, 사는곳, 지원경로, 자차, 미성년취업동의, 채용일, 사업종료일
- 연락처/사는곳/채용일은 이 테이블에만 있음
- "홍길동 연락처" → SELECT 성명, 연락처, 사는곳 FROM gs_전체명단 WHERE 성명 LIKE '%홍길동%'
- "최근 채용자" → SELECT 성명, 채용일, 연락처 FROM gs_전체명단 WHERE 채용일 != '' ORDER BY 채용일 DESC LIMIT 20

### gs_견적서 — 견적 발행 이력
컬럼: 견적번호, 견적번호발행일, 수신인, 견적서제목, 담당자, 비고, 계약체결
- "서울식품 견적" → SELECT * FROM gs_견적서 WHERE 수신인 LIKE '%서울식품%'
- "계약 체결된 건" → SELECT * FROM gs_견적서 WHERE 계약체결 != '' AND 계약체결 IS NOT NULL

### gs_일투입현황 — 일별 투입 현황
컬럼: 투입일자, 업체, 투입구인인원, 요청인원, 투입인원명단
- "오늘 투입 현황" → SELECT * FROM gs_일투입현황 WHERE 투입일자 LIKE '%2026-03-04%' (날짜형식 확인 필요)
- "서울식품 투입" → SELECT * FROM gs_일투입현황 WHERE 업체 LIKE '%서울식품%' ORDER BY 투입일자 DESC

### gs_영업문의현황 — 영업 문의 이력
컬럼: NO, 접수일, 회사명, 담당자명, 연락처, 이메일, 유입경로, 문의유형, 문의내용, 담당자2, 진행상태, 처리내용, 비고
- "이번 달 영업 문의" → SELECT * FROM gs_영업문의현황 WHERE 접수일 LIKE '%2026-03%'
- "유입경로별 문의 수" → SELECT 유입경로, COUNT(*) as 건수 FROM gs_영업문의현황 GROUP BY 유입경로
- "진행 중인 문의" → SELECT * FROM gs_영업문의현황 WHERE 진행상태 LIKE '%진행%'

### gs_GPS출퇴근 — GPS 기반 출퇴근 기록
컬럼: 날짜, 이름, 구분(출근/퇴근), 시간, 위도, 경도, 현장명, 거리, 상태(정상/현장외/수동)
- "오늘 출근한 사람" → SELECT * FROM gs_GPS출퇴근 WHERE 날짜 = '2026-03-05' AND 구분 = '출근'
- "서울식품 출근 현황" → SELECT * FROM gs_GPS출퇴근 WHERE 현장명 LIKE '%서울식품%' ORDER BY 날짜 DESC
- "현장외 출근 기록" → SELECT * FROM gs_GPS출퇴근 WHERE 상태 = '현장외'
- "이번 주 출근 통계" → SELECT 이름, COUNT(*) as 출근일수 FROM gs_GPS출퇴근 WHERE 구분 = '출근' AND 날짜 >= '2026-03-01' GROUP BY 이름
`;

// Claude API 호출
function callClaude(messages, systemPrompt) {
  const apiKey = process.env.CLAUDE_API_KEY;
  if (!apiKey) {
    return Promise.reject(new Error('CLAUDE_API_KEY 환경변수가 설정되지 않았습니다'));
  }

  const body = JSON.stringify({
    model: process.env.CLAUDE_MODEL || 'claude-haiku-4-5-20251001',
    max_tokens: 4096,
    system: systemPrompt,
    messages: messages
  });

  return new Promise((resolve, reject) => {
    const req = https.request({
      hostname: 'api.anthropic.com',
      path: '/v1/messages',
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': apiKey,
        'anthropic-version': '2023-06-01'
      }
    }, (res) => {
      // Buffer로 수집 후 UTF-8 디코딩 (한글 깨짐 방지)
      const chunks = [];
      res.on('data', chunk => chunks.push(chunk));
      res.on('end', () => {
        try {
          const data = Buffer.concat(chunks).toString('utf8');
          const parsed = JSON.parse(data);
          if (parsed.error) {
            reject(new Error(parsed.error.message));
          } else {
            resolve(parsed.content[0].text);
          }
        } catch (e) {
          const data = Buffer.concat(chunks).toString('utf8');
          reject(new Error(`API 응답 파싱 실패: ${data.substring(0, 200)}`));
        }
      });
    });
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

// SQL 응답에서 코드블록 제거
function extractSQL(text) {
  // ```sql ... ``` 코드블록에서 SQL 추출
  const codeBlockMatch = text.match(/```(?:sql)?\s*\n?([\s\S]*?)```/);
  if (codeBlockMatch) {
    return codeBlockMatch[1].trim();
  }
  // CANNOT_QUERY는 그대로 반환
  if (text.startsWith('CANNOT_QUERY')) {
    return text;
  }
  // 코드블록 없으면 그대로 반환
  return text.trim();
}

// Step 1: 질문 분석 → SQL 생성
async function generateSQL(question) {
  const systemPrompt = `당신은 SQL 변환 전문가입니다. 사용자의 한국어 질문을 SQLite SELECT 쿼리로 변환합니다.
반드시 SELECT 쿼리만 출력하세요. 다른 텍스트는 절대 출력하지 마세요.

${DB_SCHEMA}

## 질문→뷰 매핑 가이드
- 단가/인건비/청구액 단가표 → 인건비 뷰 (예: SELECT * FROM 인건비 WHERE 업쳬명 LIKE '%파워드래그%')
- 근무/출근/투입/근태/급여/임금/얼마 벌었어 → 근태 뷰 (예: SELECT * FROM 근태 WHERE 성명 = '홍길동')
- 인적사항/계좌/은행/보험 → 근로자 뷰 (예: SELECT * FROM 근로자 WHERE 성명 LIKE '%홍길동%')
- 지급내역/실지급액/공제/정산 → 지급 뷰 (예: SELECT * FROM 지급 WHERE 성명 = '홍길동')
- 중요: 업체 관련 컬럼은 모두 "업쳬명"임 (오타)
- 중요: "급여", "얼마", "임금"은 근태 뷰의 임금 컬럼 합계로 조회 (지급 뷰는 정산 완료 기록만 있어 데이터가 적음)
- 특정 사람의 월별 급여 = SELECT 성명, SUM(임금) as 총임금, COUNT(*) as 근무일수 FROM 근태 WHERE 성명='이름' AND substr(날짜,1,7)='2026-02' GROUP BY 성명
- 연락처/사는곳/채용일 → gs_전체명단 (예: SELECT 성명, 연락처 FROM gs_전체명단 WHERE 성명 LIKE '%홍길동%')
- 견적/견적서/수신인 → gs_견적서 (예: SELECT * FROM gs_견적서 WHERE 수신인 LIKE '%서울%')
- 투입현황/투입인원/오늘 투입 → gs_일투입현황 (예: SELECT * FROM gs_일투입현황 ORDER BY 투입일자 DESC)
- 영업문의/문의/유입경로 → gs_영업문의현황 (예: SELECT * FROM gs_영업문의현황 ORDER BY 접수일 DESC)
- 출퇴근/GPS/출근한 사람/퇴근/현장외/수동출근 → gs_GPS출퇴근 (예: SELECT * FROM gs_GPS출퇴근 WHERE 날짜 = '2026-03-05' AND 구분 = '출근')

## 집계/분석 쿼리 가이드
- 거래처별 매출(청구액) 합계 = SELECT 업쳬명, SUM(청구액) as 총청구액, SUM(임금) as 총임금, COUNT(DISTINCT 성명) as 투입인원, COUNT(*) as 총근무일 FROM 근태 WHERE substr(날짜,1,7)='2026-02' AND 업쳬명 NOT IN ('a업체','b업체','c업체') GROUP BY 업쳬명 ORDER BY SUM(청구액) DESC
- 거래처별 표면마진율 = SELECT 업쳬명, SUM(청구액) as 총청구액, SUM(임금) as 총임금, ROUND((SUM(청구액)-SUM(임금))*100.0/SUM(청구액),1) as 표면마진율 FROM 근태 WHERE substr(날짜,1,7)='2026-02' AND 업쳬명 NOT IN ('a업체','b업체','c업체') GROUP BY 업쳬명 ORDER BY 표면마진율 DESC
- 마진 주의: 표면마진율은 4대보험 사업주부담분(약10~11%) 미포함. 답변 시 "4대보험 미반영 표면마진" 명시할 것
- 월별 추이 = SELECT substr(날짜,1,7) as 월, 업쳬명, SUM(청구액) as 청구액, COUNT(DISTINCT 성명) as 인원 FROM 근태 WHERE 업쳬명 NOT IN ('a업체','b업체','c업체') GROUP BY substr(날짜,1,7), 업쳬명 ORDER BY 월
- 근로자 TOP N = SELECT 성명, SUM(임금) as 총임금, COUNT(*) as 근무일수, COUNT(DISTINCT 업쳬명) as 거래처수 FROM 근태 WHERE substr(날짜,1,7)='2026-02' AND 업쳬명 NOT IN ('a업체','b업체','c업체') GROUP BY 성명 ORDER BY SUM(임금) DESC LIMIT 10

## 필수 규칙
- SELECT 쿼리만 출력 (설명, 코드블록 마크다운 없이 순수 SQL만)
- 뷰(근로자, 근태, 인건비, 지급) 및 구글시트 테이블(gs_전체명단, gs_견적서, gs_일투입현황, gs_영업문의현황) 사용
- WHERE 업쳬명 NOT IN ('a업체','b업체','c업체') 항상 포함 (업쳬명 컬럼이 있는 뷰)
- LIMIT 100 기본 적용 (집계 쿼리는 LIMIT 불필요)
- 날짜 비교: substr(날짜, 1, 7) = '2026-01'
- 쿼리 불가능 시만 "CANNOT_QUERY: 이유" 출력`;

  const result = await callClaude([
    { role: 'user', content: question }
  ], systemPrompt);

  return extractSQL(result);
}

// Step 2: SQL 실행
function executeSQL(sql) {
  if (!db) throw new Error('DB 연결 안됨');

  // 안전 검사: SELECT만 허용
  const normalized = sql.replace(/--.*$/gm, '').replace(/\/\*[\s\S]*?\*\//g, '').trim().toUpperCase();
  if (!normalized.startsWith('SELECT')) {
    throw new Error('읽기 전용: SELECT 쿼리만 허용됩니다');
  }

  // 위험한 키워드 차단
  const blocked = ['DROP', 'DELETE', 'UPDATE', 'INSERT', 'ALTER', 'CREATE', 'ATTACH', 'DETACH'];
  for (const keyword of blocked) {
    if (normalized.includes(keyword)) {
      throw new Error(`차단된 키워드: ${keyword}`);
    }
  }

  const stmt = db.prepare(sql);
  return stmt.all();
}

// Step 3: 결과 → 자연어 답변 생성
async function generateAnswer(question, sql, results) {
  const systemPrompt = `당신은 워크위드 아웃소싱 회사의 AI 어시스턴트입니다.
DB 조회 결과를 바탕으로 친절하고 정확한 답변을 생성합니다.

## 규칙
1. 한국어로 답변
2. 숫자는 천단위 쉼표 사용 (예: 1,234,567원)
3. 표 형태가 적절하면 마크다운 표 사용
4. 데이터가 없으면 "해당 데이터가 없습니다"로 답변
5. 4대보험, 연속근무 등 규정 관련이면 주의사항 안내
6. 간결하게 핵심만 답변`;

  const result = await callClaude([
    { role: 'user', content: `질문: ${question}\n\n실행된 SQL:\n${sql}\n\n조회 결과 (${results.length}건):\n${JSON.stringify(results, null, 2).substring(0, 8000)}` }
  ], systemPrompt);

  return result;
}

// 챗봇 API 엔드포인트
app.post('/api/chat', async (req, res) => {
  const { question } = req.body;
  if (!question) {
    return res.status(400).json({ error: '질문을 입력해주세요' });
  }

  console.log(`[질문] ${question}`);

  try {
    // Step 1: SQL 생성
    const sqlResult = await generateSQL(question);
    console.log(`[SQL] ${sqlResult}`);

    // SQL이 아닌 응답 처리
    if (sqlResult.startsWith('CANNOT_QUERY') || !sqlResult.toUpperCase().trimStart().startsWith('SELECT')) {
      return res.json({
        answer: sqlResult.startsWith('CANNOT_QUERY')
          ? sqlResult.replace('CANNOT_QUERY:', '').trim()
          : '질문을 이해하지 못했습니다. 좀 더 구체적으로 질문해주세요.\n\n예시:\n- "파워드래그 인건비 단가"\n- "홍길동 1월 근무일수"\n- "주급 지급 대상자 명단"',
        sql: null,
        rows: 0
      });
    }

    // Step 2: SQL 실행
    let results;
    try {
      results = executeSQL(sqlResult);
    } catch (sqlErr) {
      console.error(`[SQL 오류] ${sqlErr.message}`);
      // SQL 오류 시 Claude에게 다시 요청
      return res.json({
        answer: `SQL 실행 중 오류가 발생했습니다: ${sqlErr.message}\n\n생성된 SQL:\n\`\`\`sql\n${sqlResult}\n\`\`\``,
        sql: sqlResult,
        rows: 0,
        error: true
      });
    }

    console.log(`[결과] ${results.length}건`);

    // Step 3: 답변 생성
    const answer = await generateAnswer(question, sqlResult, results);

    res.json({
      answer: answer,
      sql: sqlResult,
      rows: results.length
    });

  } catch (err) {
    console.error(`[오류] ${err.message}`);
    res.status(500).json({ error: err.message });
  }
});

// DB 상태 확인
app.get('/api/status', (req, res) => {
  if (!db) {
    return res.json({ status: 'error', message: 'DB 연결 안됨' });
  }
  try {
    const tables = db.prepare("SELECT name FROM sqlite_master WHERE type='view'").all();
    const counts = {};
    for (const { name } of tables) {
      counts[name] = db.prepare(`SELECT COUNT(*) as cnt FROM "${name}"`).get().cnt;
    }
    res.json({ status: 'ok', views: counts });
  } catch (err) {
    res.json({ status: 'error', message: err.message });
  }
});

// === CSV 다운로드 API ===
app.post('/api/download', (req, res) => {
  const { sql } = req.body;
  if (!sql || !db) {
    return res.status(400).json({ error: 'SQL 또는 DB 없음' });
  }

  // 안전 검사: SELECT만 허용
  const normalized = sql.replace(/--.*$/gm, '').replace(/\/\*[\s\S]*?\*\//g, '').trim().toUpperCase();
  if (!normalized.startsWith('SELECT')) {
    return res.status(400).json({ error: 'SELECT 쿼리만 허용' });
  }
  const blocked = ['DROP', 'DELETE', 'UPDATE', 'INSERT', 'ALTER', 'CREATE', 'ATTACH', 'DETACH'];
  for (const keyword of blocked) {
    if (normalized.includes(keyword)) {
      return res.status(400).json({ error: `차단된 키워드: ${keyword}` });
    }
  }

  try {
    // LIMIT 제거하여 전체 데이터 다운로드
    const unlimitedSql = sql.replace(/\s+LIMIT\s+\d+/gi, '');
    const rows = db.prepare(unlimitedSql).all();
    if (rows.length === 0) {
      return res.status(404).json({ error: '데이터 없음' });
    }

    // CSV 생성 (BOM 포함 — 엑셀 한글 깨짐 방지)
    const headers = Object.keys(rows[0]);
    const csvLines = [headers.join(',')];
    for (const row of rows) {
      const values = headers.map(h => {
        const val = row[h] == null ? '' : String(row[h]);
        // 쉼표나 줄바꿈 포함 시 따옴표로 감싸기
        return val.includes(',') || val.includes('\n') || val.includes('"')
          ? `"${val.replace(/"/g, '""')}"` : val;
      });
      csvLines.push(values.join(','));
    }

    const bom = '\uFEFF';
    res.setHeader('Content-Type', 'text/csv; charset=utf-8');
    res.setHeader('Content-Disposition', 'attachment; filename=data.csv');
    res.send(bom + csvLines.join('\n'));
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// === 구글시트 동기화 API ===

// 전체 시트 동기화
app.post('/api/sheets/sync', async (req, res) => {
  if (!db) return res.status(500).json({ error: 'DB 연결 안됨' });
  try {
    const results = await syncAllSheets(db);
    res.json({ success: true, results });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// 특정 시트만 동기화
app.post('/api/sheets/sync/:sheetName', async (req, res) => {
  if (!db) return res.status(500).json({ error: 'DB 연결 안됨' });
  try {
    const result = await syncSheet(db, req.params.sheetName);
    res.json(result);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// 동기화 상태 확인
app.get('/api/sheets/status', (req, res) => {
  if (!db) return res.status(500).json({ error: 'DB 연결 안됨' });
  try {
    const status = {};
    const tables = ['gs_전체명단', 'gs_견적서', 'gs_일투입현황', 'gs_영업문의현황'];
    for (const t of tables) {
      try {
        const row = db.prepare(`SELECT COUNT(*) as cnt FROM ${t}`).get();
        status[t] = row.cnt;
      } catch {
        status[t] = 0;
      }
    }
    res.json(status);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// === GPS 출퇴근 대시보드 API ===

// 대시보드 데이터: 오늘 출근현황 + 결근자 + 대체 가능 인력
app.get('/api/gps/dashboard', (req, res) => {
  if (!db) return res.status(500).json({ error: 'DB 연결 안됨' });

  try {
    const today = new Date().toISOString().split('T')[0]; // 2026-03-05
    const region = req.query.region || ''; // 지역 필터 (예: 충주)

    // 1. 오늘 출근한 사람
    let checkedIn = [];
    try {
      checkedIn = db.prepare(`
        SELECT 이름, 구분, 시간, 현장명, 거리, 상태
        FROM gs_GPS출퇴근
        WHERE 날짜 = ?
        ORDER BY 시간 DESC
      `).all(today);
    } catch { /* 테이블 없으면 빈 배열 */ }

    const checkedInNames = [...new Set(checkedIn.filter(r => r.구분 === '출근').map(r => r.이름))];

    // 2. 오늘 투입 예정 인원 (gs_일투입현황에서)
    let scheduledRaw = [];
    try {
      scheduledRaw = db.prepare(`
        SELECT 업체, 투입인원명단, 투입구인인원, 요청인원
        FROM gs_일투입현황
        WHERE 투입일자 LIKE ?
      `).all(`%${today.replace(/-/g, '').slice(2)}%`);

      // 날짜 형식이 다를 수 있으므로 여러 패턴 시도
      if (scheduledRaw.length === 0) {
        scheduledRaw = db.prepare(`
          SELECT 업체, 투입인원명단, 투입구인인원, 요청인원
          FROM gs_일투입현황
          WHERE 투입일자 LIKE ?
        `).all(`%${today}%`);
      }
    } catch { /* 테이블 없으면 빈 배열 */ }

    // 투입인원명단 파싱 → 개별 근로자 + 출근 여부 매칭
    const scheduledWorkers = [];
    const nameCount = {}; // 동명이인 감지용
    scheduledRaw.forEach(row => {
      const names = (row.투입인원명단 || '')
        .split(/[,\n\r\/·]/)
        .map(n => n.trim())
        .filter(Boolean);

      names.forEach(name => {
        const checkinRecord = checkedIn.find(r => r.이름 === name && r.구분 === '출근');
        scheduledWorkers.push({
          업체: row.업체,
          이름: name,
          출근여부: !!checkinRecord,
          시간: checkinRecord ? checkinRecord.시간 : null,
          상태: checkinRecord ? checkinRecord.상태 : '미출근'
        });
        nameCount[name] = (nameCount[name] || 0) + 1;
      });
    });

    const absentCount = scheduledWorkers.filter(w => !w.출근여부).length;

    // 동명이인 감지: 전체명단에서 같은 이름이 2명 이상인 경우
    const allScheduledNames = [...new Set(scheduledWorkers.map(w => w.이름))];
    let duplicateNames = [];
    if (allScheduledNames.length > 0) {
      try {
        const placeholders = allScheduledNames.map(() => '?').join(',');
        const dupes = db.prepare(`
          SELECT 성명, COUNT(*) as cnt
          FROM gs_전체명단
          WHERE 성명 IN (${placeholders})
          GROUP BY 성명
          HAVING cnt > 1
        `).all(...allScheduledNames);
        duplicateNames = dupes.map(d => d.성명);
      } catch { /* 무시 */ }
    }

    // 동명이인 플래그 추가
    scheduledWorkers.forEach(w => {
      w.동명이인 = duplicateNames.includes(w.이름);
    });

    // 3. 최근 1개월 근무자 (대체 인력 후보)
    const oneMonthAgo = new Date();
    oneMonthAgo.setMonth(oneMonthAgo.getMonth() - 1);
    const monthAgoStr = oneMonthAgo.toISOString().split('T')[0];

    let recentWorkers = [];
    try {
      let sql = `
        SELECT
          g.성명, g.연락처, g.사는곳,
          MAX(t.날짜) as 마지막근무일,
          t.업쳬명 as 최근근무지,
          COUNT(*) as 근무일수
        FROM 근태 t
        LEFT JOIN gs_전체명단 g ON t.성명 = g.성명
        WHERE substr(t.날짜, 1, 10) >= ?
          AND t.업쳬명 NOT IN ('a업체','b업체','c업체')
          AND t.성명 NOT IN (${checkedInNames.map(() => '?').join(',') || "''"})
      `;
      const params = [monthAgoStr, ...checkedInNames];

      if (region) {
        sql += ` AND g.사는곳 LIKE ?`;
        params.push(`%${region}%`);
      }

      sql += `
        GROUP BY t.성명
        ORDER BY MAX(t.날짜) DESC
        LIMIT 30
      `;

      recentWorkers = db.prepare(sql).all(...params);
    } catch { /* 조인 실패 시 빈 배열 */ }

    // 4. 최근 지원자 (최근 채용일 기준)
    let recentApplicants = [];
    try {
      let sql = `
        SELECT 성명, 연락처, 사는곳, 채용일
        FROM gs_전체명단
        WHERE 채용일 != '' AND 채용일 IS NOT NULL
      `;
      const params = [];

      if (region) {
        sql += ` AND 사는곳 LIKE ?`;
        params.push(`%${region}%`);
      }

      sql += ` ORDER BY 채용일 DESC LIMIT 20`;
      recentApplicants = db.prepare(sql).all(...params);
    } catch { /* 테이블 없으면 빈 배열 */ }

    res.json({
      today,
      region: region || '전체',
      checkedIn,
      checkedInCount: checkedInNames.length,
      scheduledWorkers,
      scheduledCount: scheduledWorkers.length,
      absentCount,
      duplicateNames,
      recentWorkers,
      recentApplicants
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// === 결근 대체인력 문자 발송 API ===

// 선택된 인력 상세정보 조회 (문자 발송 전 확인용)
app.get('/api/gps/worker-detail', (req, res) => {
  if (!db) return res.status(500).json({ error: 'DB 연결 안됨' });

  const names = (req.query.names || '').split(',').filter(Boolean);
  if (names.length === 0) return res.json([]);

  try {
    const placeholders = names.map(() => '?').join(',');
    const workers = db.prepare(`
      SELECT 성명, 생년월일, 연락처, 사는곳
      FROM gs_전체명단
      WHERE 성명 IN (${placeholders})
    `).all(...names);

    res.json(workers);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// 대체인력에게 단체문자 발송
app.post('/api/gps/send-backup-sms', async (req, res) => {
  if (!db) return res.status(500).json({ error: 'DB 연결 안됨' });

  const { siteName, workerIds } = req.body;
  // siteName: 현장명 (예: "서울식품")
  // workerIds: 선택한 인력의 성명 배열 (예: ["김OO", "박OO"])

  if (!siteName || !workerIds || workerIds.length === 0) {
    return res.status(400).json({ error: '현장명과 발송 대상을 선택해주세요' });
  }

  try {
    // 선택된 인력의 연락처 조회
    const placeholders = workerIds.map(() => '?').join(',');
    const workers = db.prepare(`
      SELECT 성명, 연락처
      FROM gs_전체명단
      WHERE 성명 IN (${placeholders}) AND 연락처 IS NOT NULL AND 연락처 != ''
    `).all(...workerIds);

    if (workers.length === 0) {
      return res.status(400).json({ error: '연락처가 있는 대상이 없습니다' });
    }

    const result = await sendBackupRequest(siteName, workers);
    res.json(result);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// 알리고 잔여 건수 조회
app.get('/api/sms/balance', async (req, res) => {
  try {
    const balance = await checkBalance();
    res.json(balance);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// === 규정 위반 체크 API ===

// 수동 규정 위반 체크
app.get('/api/compliance/check', (req, res) => {
  if (!db) {
    return res.status(500).json({ error: 'DB 연결 안됨' });
  }
  const { month } = req.query; // ?month=2026-03 (선택)
  try {
    const result = runAllChecks(db, month);
    res.json(result);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// 수동 규정 위반 체크 + 이메일 발송
app.post('/api/compliance/send', async (req, res) => {
  if (!db) {
    return res.status(500).json({ error: 'DB 연결 안됨' });
  }
  const { month } = req.body;
  const recipients = process.env.ALERT_EMAIL || process.env.SMTP_USER;
  if (!recipients) {
    return res.status(400).json({ error: 'ALERT_EMAIL 또는 SMTP_USER 미설정' });
  }

  try {
    const result = runAllChecks(db, month);
    const mailResult = await sendComplianceAlert(result, recipients);
    res.json({ compliance: result.요약, mail: mailResult });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// === 자동 스케줄 (node-cron) ===

// 매월 1일 오전 9시 — 규정 위반 자동 체크 + 이메일 발송
cron.schedule('0 9 1 * *', async () => {
  console.log('[스케줄] 월간 규정 위반 체크 실행');
  if (!db) return;
  const recipients = process.env.ALERT_EMAIL || process.env.SMTP_USER;
  if (!recipients) {
    console.warn('[스케줄] 이메일 미설정 — 콘솔만 출력');
  }
  try {
    const result = runAllChecks(db);
    console.log(`[스케줄] 체크 완료: 총 ${result.요약.총위반건수}건`);
    if (recipients && result.요약.총위반건수 > 0) {
      await sendComplianceAlert(result, recipients);
    }
  } catch (err) {
    console.error('[스케줄] 체크 오류:', err.message);
  }
}, { timezone: 'Asia/Seoul' });

// 매주 월요일 오전 9시 — 주간 체크 (연속근무 위험 조기 감지)
cron.schedule('0 9 * * 1', async () => {
  console.log('[스케줄] 주간 규정 위반 체크 실행');
  if (!db) return;
  const recipients = process.env.ALERT_EMAIL || process.env.SMTP_USER;
  try {
    const result = runAllChecks(db);
    // 높음 등급만 알림
    if (recipients && result.요약.높음 > 0) {
      await sendComplianceAlert(result, recipients);
      console.log(`[스케줄] 주간 알림 발송: 높음 ${result.요약.높음}건`);
    } else {
      console.log('[스케줄] 주간 체크 완료 — 높은 위험 없음');
    }
  } catch (err) {
    console.error('[스케줄] 주간 체크 오류:', err.message);
  }
}, { timezone: 'Asia/Seoul' });

console.log('[스케줄] 월간(매월1일 9시) + 주간(매주월 9시) 자동 체크 등록');

const PORT = process.env.PORT || 3001;
app.listen(PORT, '0.0.0.0', () => {
  console.log(`[서버] http://0.0.0.0:${PORT} 시작`);
});
