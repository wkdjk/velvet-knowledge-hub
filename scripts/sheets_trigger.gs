/**
 * VKH Data Refresh — Google Apps Script
 *
 * Paste this entire file into the Google Sheet's Apps Script editor
 * (Extensions → Apps Script), then save and reload the sheet.
 *
 * One-time setup (do this before using the button):
 *   1. Open Apps Script editor → Project Settings (gear icon) → Script properties
 *   2. Add a property named:  GITHUB_PAT
 *      Value: your GitHub Personal Access Token
 *      (Token needs: repo + workflow scopes — create at github.com/settings/tokens)
 *   3. Save. Close settings.
 *   4. Return to the sheet — a "VKH" menu will appear in the top menu bar.
 */

const REPO_OWNER = "wkdjk";
const REPO_NAME  = "velvet-knowledge-hub";
const WORKFLOW   = "ingest_from_drive.yml";

/**
 * Adds the "VKH" menu to the sheet when it opens.
 * Runs automatically — no setup needed.
 */
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu("VKH")
    .addItem("Refresh data from Drive", "triggerDriveIngest")
    .addItem("Rebuild dashboard only", "triggerBuildOnly")
    .addToUi();
}

/**
 * Triggers the Drive ingest workflow (ingest_from_drive.yml).
 * Reads all new files from Google Drive and rebuilds the dashboard.
 * The site updates in approximately 3 minutes.
 */
function triggerDriveIngest() {
  _triggerWorkflow(WORKFLOW, { folder: "all" },
    "Drive ingest started — the dashboard will update in approximately 10 minutes (quota-safe pacing).");
}

/**
 * Triggers a dashboard rebuild without re-ingesting Drive files.
 * Use this after editing sheet data manually.
 */
function triggerBuildOnly() {
  _triggerWorkflow("build_site.yml", {},
    "Dashboard rebuild started — the site will update in approximately 3 minutes.");
}

function _triggerWorkflow(workflow, inputs, successMessage) {
  const pat = PropertiesService.getScriptProperties().getProperty("GITHUB_PAT");
  if (!pat) {
    SpreadsheetApp.getUi().alert(
      "Setup required",
      "GITHUB_PAT not found in Script Properties.\n\n" +
      "Go to Extensions → Apps Script → Project Settings → Script properties\n" +
      "and add a property named GITHUB_PAT with your GitHub token.",
      SpreadsheetApp.getUi().ButtonSet.OK
    );
    return;
  }

  const url = `https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/actions/workflows/${workflow}/dispatches`;
  const payload = JSON.stringify({ ref: "main", inputs: inputs });

  const response = UrlFetchApp.fetch(url, {
    method: "post",
    headers: {
      Authorization: `Bearer ${pat}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    contentType: "application/json",
    payload: payload,
    muteHttpExceptions: true,
  });

  if (response.getResponseCode() === 204) {
    SpreadsheetApp.getActiveSpreadsheet().toast(
      successMessage, "VKH update", 8
    );
  } else {
    const body = response.getContentText();
    SpreadsheetApp.getUi().alert(
      "Update failed",
      `GitHub returned status ${response.getResponseCode()}.\n\n${body}\n\n` +
      "Check that your GITHUB_PAT token has repo and workflow permissions " +
      "and has not expired.",
      SpreadsheetApp.getUi().ButtonSet.OK
    );
  }
}
