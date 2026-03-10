// ============================================================
// 워크위드 제안서 저장소 - Google Apps Script (JSONP)
// ============================================================
// 설치 방법:
// 1. 구글시트 "제안서저장소" 생성
// 2. "제안서" 탭 생성, A1:F1 헤더 입력:
//    id | clientName | serviceType | savedAt | savedBy | jsonData
// 3. 확장프로그램 > Apps Script 클릭
// 4. 이 코드 전체 붙여넣기
// 5. 배포 > 새 배포 > 유형: 웹 앱
//    - 실행 주체: 나
//    - 액세스 권한: 모든 사용자
// 6. 배포 후 URL 복사 → proposal-template.html의 SCRIPT_URL에 붙여넣기
// ============================================================

function doGet(e) {
  var result;
  try {
    var action = e.parameter.action || 'list';
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = ss.getSheets()[0]; // 첫 번째 탭 사용

    // === 저장 ===
    if (action === 'save') {
      var rawData = e.parameter.data;
      if (!rawData) { result = { success: false, error: '데이터가 없습니다' }; }
      else {
        var data = JSON.parse(rawData);
        var savedBy = e.parameter.savedBy || '미지정';
        var id = e.parameter.id || '';
        var now = new Date();
        var savedAt = Utilities.formatDate(now, 'Asia/Seoul', 'yyyy-MM-dd HH:mm');
        var clientName = (data.form && data.form.clientName) || '미지정';
        var serviceType = (data.form && data.form.serviceType) || '';
        var jsonData = JSON.stringify(data);
        var found = false;

        if (id) {
          var rows = sheet.getDataRange().getValues();
          for (var i = 1; i < rows.length; i++) {
            if (rows[i][0] === id) {
              sheet.getRange(i + 1, 2, 1, 5).setValues([[clientName, serviceType, savedAt, savedBy, jsonData]]);
              result = { success: true, id: id, savedAt: savedAt };
              found = true;
              break;
            }
          }
        }
        if (!found) {
          var newId = Utilities.getUuid();
          sheet.appendRow([newId, clientName, serviceType, savedAt, savedBy, jsonData]);
          result = { success: true, id: newId, savedAt: savedAt };
        }
      }
    }

    // === 목록 ===
    else if (action === 'list') {
      var rows = sheet.getDataRange().getValues();
      var list = [];
      for (var i = 1; i < rows.length; i++) {
        list.push({
          id: rows[i][0],
          clientName: rows[i][1],
          serviceType: rows[i][2],
          savedAt: String(rows[i][3]),
          savedBy: rows[i][4]
        });
      }
      list.sort(function(a, b) { return b.savedAt > a.savedAt ? 1 : -1; });
      result = { success: true, list: list };
    }

    // === 단건 조회 ===
    else if (action === 'load') {
      var id = e.parameter.id;
      if (!id) { result = { success: false, error: 'id가 없습니다' }; }
      else {
        var rows = sheet.getDataRange().getValues();
        result = { success: false, error: '제안서를 찾을 수 없습니다' };
        for (var i = 1; i < rows.length; i++) {
          if (rows[i][0] === id) {
            result = {
              success: true,
              id: rows[i][0],
              data: JSON.parse(rows[i][5])
            };
            break;
          }
        }
      }
    }

    // === 삭제 ===
    else if (action === 'delete') {
      var id = e.parameter.id;
      if (!id) { result = { success: false, error: 'id가 없습니다' }; }
      else {
        var rows = sheet.getDataRange().getValues();
        result = { success: false, error: '제안서를 찾을 수 없습니다' };
        for (var i = 1; i < rows.length; i++) {
          if (rows[i][0] === id) {
            sheet.deleteRow(i + 1);
            result = { success: true };
            break;
          }
        }
      }
    }

    else {
      result = { success: false, error: '알 수 없는 action' };
    }

  } catch (err) {
    result = { success: false, error: err.message };
  }

  // JSONP 응답 (callback 파라미터가 있으면 JS로, 없으면 JSON으로)
  var callback = e.parameter.callback;
  if (callback) {
    return ContentService
      .createTextOutput(callback + '(' + JSON.stringify(result) + ')')
      .setMimeType(ContentService.MimeType.JAVASCRIPT);
  }
  return ContentService
    .createTextOutput(JSON.stringify(result))
    .setMimeType(ContentService.MimeType.JSON);
}
