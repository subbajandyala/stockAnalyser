/**
 * Google Apps Script web app that receives the daily rows from
 * scripts/sheets_daily_log.py and writes them to this Google Sheet.
 *
 * Setup (once, about 5 minutes):
 *  1. Create a new Google Sheet, e.g. "Candle Bot Journal".
 *  2. Extensions → Apps Script. Delete what is there, paste this whole file, Save.
 *  3. Change SECRET below to your own word (letters and digits, 16+ characters).
 *  4. Deploy → New deployment → type "Web app".
 *       Execute as: Me
 *       Who has access: Anyone
 *     Deploy, allow the permissions, copy the "Web app URL" (ends in /exec).
 *  5. On every machine, add to /root/stockAnalyser/.env:
 *       SHEETS_WEBHOOK_URL=<that URL>
 *       SHEETS_SECRET=<the same word as SECRET>
 *
 * The tabs "Trades" and "Daily" are created on the first delivery. A re-sent day
 * updates its rows (Trades by Order ID, Daily by Day) instead of adding duplicates.
 * After changing this code: Deploy → Manage deployments → edit → Version: New version.
 */
const SECRET = 'change-me-to-a-long-secret-word';

const TRADE_COLS = ['Order ID', 'Account', 'Date', 'Time', 'Contract', 'Symbol', 'Direction', 'Candle', 'Ride',
                    'Qty', 'Entry', 'SL', 'Target', 'Exit', 'P&L', 'Result'];
const DAILY_COLS = ['Day', 'Account', 'Date', 'Signals', 'Trades', 'Wins', 'Losses', 'Open', 'Day P&L',
                    'Target hit first', 'SL hit first', 'Notes'];

function doPost(e) {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);                       // several machines may send at once
  try {
    const body = JSON.parse(e.postData.contents);
    if (body.secret !== SECRET) return reply({ok: false, error: 'wrong secret'});
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const t = upsert(sheetFor(ss, 'Trades', TRADE_COLS), TRADE_COLS, 'Order ID', body.trades || []);
    const d = upsert(sheetFor(ss, 'Daily', DAILY_COLS), DAILY_COLS, 'Day', body.daily ? [body.daily] : []);
    return reply({ok: true, trades_added: t.added, trades_updated: t.updated, daily_added: d.added});
  } catch (err) {
    return reply({ok: false, error: String(err)});
  } finally {
    lock.releaseLock();
  }
}

function sheetFor(ss, name, cols) {
  let sh = ss.getSheetByName(name);
  if (!sh) {
    sh = ss.insertSheet(name);
    sh.getRange(1, 1, 1, cols.length).setValues([cols]).setFontWeight('bold');
    sh.setFrozenRows(1);
  }
  return sh;
}

function upsert(sh, cols, keyCol, rows) {
  const keyIdx = cols.indexOf(keyCol);
  const last = sh.getLastRow();
  const keys = last > 1 ? sh.getRange(2, keyIdx + 1, last - 1, 1).getValues().map(r => String(r[0])) : [];
  let added = 0, updated = 0;
  rows.forEach(row => {
    const values = cols.map(c => (row[c] === null || row[c] === undefined) ? '' : row[c]);
    // Keep IDs and dates as text, so the sheet doesn't reformat them
    values[keyIdx] = "'" + String(row[keyCol]);
    const di = cols.indexOf('Date');
    if (di >= 0 && row['Date']) values[di] = "'" + row['Date'];
    const at = keys.indexOf(String(row[keyCol]));
    if (at >= 0) {
      sh.getRange(at + 2, 1, 1, cols.length).setValues([values]);
      updated++;
    } else {
      sh.appendRow(values);
      keys.push(String(row[keyCol]));
      added++;
    }
  });
  return {added, updated};
}

function reply(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
