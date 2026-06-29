// kvn_csv_email.gs — VKH CSV-by-email backend
// Deploy as: Web app → Execute as Me → Anyone can access
//
// doPost: receives {"email_address": "...", "request_source": "vkh_import_records"}
//         reads VFI_Import_Records tab, filters last 90 days, emails CSV attachment.
// doGet:  health check — returns {ok: true, service: "vkh-csv-email"}

var SHEET_ID = '1idbPiaK_Scd8znktn2cPutWP5Lg4azo1XBfXNyt5K2U';
var TAB_NAME = 'VFI_Import_Records';
var DATE_COLUMN = 'notification_date';  // column used for 90-day filter

function doGet(e) {
  return ContentService
    .createTextOutput(JSON.stringify({ ok: true, service: 'vkh-csv-email' }))
    .setMimeType(ContentService.MimeType.JSON);
}

function doPost(e) {
  try {
    // Parse request body.
    var body = JSON.parse(e.postData.contents);
    var email = body.email_address;
    if (!email) {
      throw new Error('email_address is required');
    }

    // Open sheet and read the import records tab.
    var ss      = SpreadsheetApp.openById(SHEET_ID);
    var ws      = ss.getSheetByName(TAB_NAME);
    if (!ws) {
      throw new Error('Tab not found: ' + TAB_NAME);
    }

    var allValues = ws.getDataRange().getValues();
    if (allValues.length < 2) {
      throw new Error('No data rows in ' + TAB_NAME);
    }

    // First row is headers.
    var headers    = allValues[0];
    var dataRows   = allValues.slice(1);
    var dateColIdx = headers.indexOf(DATE_COLUMN);
    if (dateColIdx === -1) {
      throw new Error('Column not found: ' + DATE_COLUMN);
    }

    // Compute cutoff: 90 days ago at midnight UTC.
    var cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - 90);
    cutoff.setHours(0, 0, 0, 0);

    // Filter rows within the last 90 days.
    var filtered = dataRows.filter(function (row) {
      var raw = row[dateColIdx];
      if (!raw) return false;
      var d = (raw instanceof Date) ? raw : new Date(raw);
      return !isNaN(d.getTime()) && d >= cutoff;
    });

    // Build CSV string from headers + filtered rows.
    var csvRows = [headers].concat(filtered);
    var csvString = csvRows.map(function (row) {
      return row.map(function (cell) {
        var s = (cell instanceof Date)
          ? Utilities.formatDate(cell, Session.getScriptTimeZone(), 'yyyy-MM-dd')
          : String(cell);
        // Quote cells that contain comma, double-quote, or newline.
        if (s.indexOf(',') !== -1 || s.indexOf('"') !== -1 || s.indexOf('\n') !== -1) {
          s = '"' + s.replace(/"/g, '""') + '"';
        }
        return s;
      }).join(',');
    }).join('\r\n');

    // Build attachment filename: import-records-YYYY-MM-DD.csv
    var today    = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd');
    var filename = 'import-records-' + today + '.csv';

    var blob = Utilities.newBlob(csvString, 'text/csv', filename);

    // Send email with CSV attachment.
    GmailApp.sendEmail(
      email,
      'Velvet Knowledge Hub — import records CSV',
      'Please find attached the MFDS import records for New Zealand deer velvet ' +
      'products entering the Korean market over the past 90 days. ' +
      'The file contains ' + filtered.length + ' records.',
      { attachments: [blob] }
    );

    Logger.log('CSV emailed to ' + email + ' — ' + filtered.length + ' rows');

    return ContentService
      .createTextOutput(JSON.stringify({ ok: true }))
      .setMimeType(ContentService.MimeType.JSON);

  } catch (err) {
    Logger.log('ERROR: ' + err.message);
    return ContentService
      .createTextOutput(JSON.stringify({ ok: false, error: err.message }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}
