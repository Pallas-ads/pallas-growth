/**
 * Demo sign-ups for the homepage: each request becomes a row in a Google Sheet.
 *
 * Setup, once:
 * 1. Create a Google Sheet. Put these headers in row 1: Time, Name, Email, Page.
 * 2. In the Sheet, open Extensions > Apps Script, replace the code with this file, and save.
 * 3. Deploy > New deployment > Web app. Execute as: Me. Who has access: Anyone.
 * 4. Copy the web app URL (it ends in /exec) into SIGNUP_ENDPOINT in site/index.html.
 *
 * After changing this code, deploy a new version of the same deployment so the URL stays the same.
 */

const MAX_LEN = 200;

function doPost(e) {
  const p = (e && e.parameter) || {};
  const name = clean(p.name);
  const email = clean(p.email).toLowerCase();
  if (!name || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
    return reply({ ok: false, error: "Add your name and a work email." });
  }

  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    SpreadsheetApp.getActiveSpreadsheet().getSheets()[0]
      .appendRow([new Date(), name, email, clean(p.page)]);
  } finally {
    lock.releaseLock();
  }
  return reply({ ok: true });
}

// Trims, caps the length, and stops a value from being read as a spreadsheet formula.
function clean(value) {
  const s = String(value || "").trim().slice(0, MAX_LEN);
  return /^[=+\-@]/.test(s) ? "'" + s : s;
}

function reply(body) {
  return ContentService.createTextOutput(JSON.stringify(body))
    .setMimeType(ContentService.MimeType.JSON);
}
