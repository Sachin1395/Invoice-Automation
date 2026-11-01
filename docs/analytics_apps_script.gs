/**
 * Paste into the Google Sheet: Extensions > Apps Script. Then Deploy > New deployment > Web app
 * (Execute as: Me, Who has access: Anyone) and copy the web app URL into Render as ANALYTICS_WEBHOOK_URL.
 * Put the SAME secret in Render as ANALYTICS_SECRET. Redeploy (New version) after editing this script.
 * Sheet: first tab named "Events", row 1 = time | event | visitor | src | session
 */
const SECRET = "PASTE_YOUR_SECRET_HERE";
const ALLOWED = ["opened", "started", "completed", "downloaded"];

function doPost(e) {
  try {
    const d = JSON.parse(e.postData.contents);
    if (d.secret !== SECRET || ALLOWED.indexOf(d.event) < 0) return out("ignored");
    const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("Events");
    sheet.appendRow([d.time, d.event, d.visitor, d.src, d.session]);
    return out("ok");
  } catch (err) {
    return out("error");
  }
}

function out(s) { return ContentService.createTextOutput(s); }
