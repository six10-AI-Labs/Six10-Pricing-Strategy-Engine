/**
 * Six10 Pricing Engine — Email Action Handler
 * ============================================
 * Deploy as a Google Apps Script Web App to enable 1-click Dismiss and
 * "Added to Pipeline" logging directly from the weekly pricing email.
 *
 * HOW TO DEPLOY (one-time, ~5 minutes):
 * ──────────────────────────────────────
 * 1. Open the Six10 Pricing Google Sheet.
 * 2. Extensions → Apps Script.
 * 3. Delete the default `myFunction` code.
 * 4. Paste this entire file.
 * 5. The SHEET_ID below is already set — do not change it.
 * 6. Click Deploy → New deployment.
 *    - Type: Web app
 *    - Execute as: Me
 *    - Who has access: Anyone   ← required for email links to work
 * 7. Authorise when prompted (Sheets + Script permissions).
 * 8. Copy the Web App URL shown after deployment.
 * 9. Paste it into config.yaml under  email.dismiss_script_url
 * 10. Re-run python main.py — email links are now live.
 *
 * FLOW — Dismiss:
 *   Click "✕ Dismiss" → logs to "Dismissed ASINs" tab → tab closes in 2s.
 *
 * FLOW — Actioned:
 *   Click "✓ Actioned" → small compact card opens with two options:
 *     [Skip — I'll go with what's recommended]  →  logs "As recommended", closes.
 *     [text box + Submit]                        →  logs their note + optional price, closes.
 *   If they click Actioned again from the same email later (came back to add notes):
 *     → Detects existing row for that ASIN and UPDATES it instead of duplicating.
 *
 * TABS WRITTEN:
 *   "Dismissed ASINs"  — Brand, ASIN, Title, Recommendation, Dismissed Date
 *   "Pipeline Actions" — Brand, ASIN, Title, Recommendation, Actioned Date,
 *                        Actual Action Taken, Actual Price Implemented
 *
 * Both tabs are auto-created on first use.
 * Recovery: "Dismissed ASINs" tab → delete the row to un-dismiss.
 */

// ── Configuration ─────────────────────────────────────────────────────────────
const SHEET_ID = "1xtyF6SDcQNA5H1wJ9itQm6WIHN0_NL9DAO7DA44NaEA";

// ── Column layout for Pipeline Actions tab ────────────────────────────────────
// "By" column added at the end — safe to add to existing tabs (old rows get blank).
const PA_HEADERS = [
  "Brand", "ASIN", "Title", "Recommendation",
  "Actioned Date", "Actual Action Taken", "Actual Price Implemented", "By"
];
const PA_COL = { brand:0, asin:1, title:2, rec:3, date:4, notes:5, price:6, by:7 };

// ── Column layout for Dismissed ASINs tab ─────────────────────────────────────
const DA_HEADERS = ["Brand", "ASIN", "Title", "Recommendation", "Dismissed Date", "By"];

// ── Main handler ──────────────────────────────────────────────────────────────
function doGet(e) {
  const p      = e.parameter || {};
  const action = (p.action || "").trim();
  const asin   = (p.asin   || "").trim();
  const brand  = (p.brand  || "").trim();
  const title  = (p.title  || "").trim();
  const rec    = (p.rec    || "").trim();
  // "by" is pre-filled with the recipient's display name from the personalised
  // email URL (e.g. &by=Shashank).  Falls back to "" for older email links.
  const by     = (p.by     || "").trim();

  if (!asin) {
    return _page("Error", "ASIN parameter missing.", "#c0392b", false);
  }

  try {
    const ss  = SpreadsheetApp.openById(SHEET_ID);
    const now = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), "yyyy-MM-dd");

    // ── Dismiss ───────────────────────────────────────────────────────────
    if (action === "dismiss") {
      _appendRow(ss, "Dismissed ASINs", DA_HEADERS,
        [brand, asin, title, rec, now, by]
      );
      return _page(
        "✕ Dismissed",
        "<strong>" + _esc(asin) + "</strong>" + (brand ? " (" + _esc(brand) + ")" : "")
        + "<br/>Won't appear next week."
        + (by ? "<br/><span style='font-size:11px;color:#aaa;'>Logged by " + _esc(by) + "</span>" : ""),
        "#c0392b", true
      );
    }

    // ── Pipeline: show compact note card ─────────────────────────────────
    if (action === "pipeline") {
      const existingNote = _findExistingNote(ss, asin, brand);
      return _noteCard(asin, brand, title, rec, by, existingNote);
    }

    // ── Pipeline All: bulk-approve multiple ASINs as "As recommended" ────
    // Called by the "Approve All Top 5 Raises" button in the email.
    // Params: asins, brands, titles, recs — all pipe-separated (|), same index order.
    if (action === "pipeline_all") {
      const asins  = (p.asins  || "").split("|").map(s => s.trim()).filter(Boolean);
      const brands = (p.brands || "").split("|").map(s => s.trim());
      const titles = (p.titles || "").split("|").map(s => s.trim());
      const recs   = (p.recs   || "").split("|").map(s => s.trim());

      if (!asins.length) {
        return _page("Error", "No ASINs provided.", "#c0392b", false);
      }

      let logged = 0;
      const now = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), "yyyy-MM-dd");
      for (let i = 0; i < asins.length; i++) {
        _upsertPipelineRow(
          ss,
          brands[i] || "", asins[i],
          titles[i] || "", recs[i] || "RAISE",
          now, "As recommended (bulk approve)", "", by
        );
        logged++;
      }

      return _page(
        "&#10003; All Approved",
        logged + " price action" + (logged !== 1 ? "s" : "") + " logged as <em>As recommended</em>."
        + (by ? "<br/><span style='font-size:11px;color:#aaa;'>Logged by " + _esc(by) + "</span>" : ""),
        "#155724", true
      );
    }

    // ── Pipeline confirm: log or update ───────────────────────────────────
    if (action === "pipeline_confirm") {
      const notes       = (p.notes        || "").trim();
      const actualPrice = (p.actual_price || "").trim();
      const confirmedBy = (p.by           || "").trim();   // passed through form
      const finalNotes  = notes || "As recommended";

      _upsertPipelineRow(ss, brand, asin, title, rec, now, finalNotes, actualPrice, confirmedBy);

      const skipped = !notes;
      return _page(
        "✓ Logged",
        "<strong>" + _esc(asin) + "</strong><br/>"
        + (skipped
          ? "Going with the recommendation."
          : _esc(finalNotes) + (actualPrice ? " @ $" + _esc(actualPrice) : ""))
        + (confirmedBy ? "<br/><span style='font-size:11px;color:#aaa;'>Logged by " + _esc(confirmedBy) + "</span>" : "")
        + "<br/><span style='font-size:11px;color:#aaa;'>You can update this by clicking Actioned again.</span>",
        "#155724", true
      );
    }

    return _page("Error", "Unknown action: " + action, "#888", false);

  } catch (err) {
    return _page("Error", err.message, "#c0392b", false);
  }
}

// ── Upsert: update existing row if found, else append ────────────────────────
function _upsertPipelineRow(ss, brand, asin, title, rec, date, notes, price, by) {
  let sheet = ss.getSheetByName("Pipeline Actions");
  if (!sheet) {
    sheet = ss.insertSheet("Pipeline Actions");
    sheet.appendRow(PA_HEADERS);
    sheet.getRange(1, 1, 1, PA_HEADERS.length).setFontWeight("bold").setBackground("#f0f4f8");
    sheet.setFrozenRows(1);
  }

  // Ensure "By" column header exists on older tabs (migration-safe)
  const headerRow = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  if (!headerRow.includes("By")) {
    const nextCol = sheet.getLastColumn() + 1;
    sheet.getRange(1, nextCol).setValue("By").setFontWeight("bold").setBackground("#f0f4f8");
  }

  const data = sheet.getDataRange().getValues();
  // Search from row 2 (index 1) for matching ASIN + brand
  for (let i = 1; i < data.length; i++) {
    const rowAsin  = String(data[i][PA_COL.asin]  || "").trim();
    const rowBrand = String(data[i][PA_COL.brand] || "").trim();
    if (rowAsin === asin && rowBrand === brand) {
      // Found — update notes, price, date, and by (in case they came back later)
      sheet.getRange(i + 1, PA_COL.date  + 1).setValue(date);
      sheet.getRange(i + 1, PA_COL.notes + 1).setValue(notes);
      sheet.getRange(i + 1, PA_COL.price + 1).setValue(price || "");
      sheet.getRange(i + 1, PA_COL.by   + 1).setValue(by    || "");
      return;
    }
  }
  // Not found — append new row
  sheet.appendRow([brand, asin, title, rec, date, notes, price || "", by || ""]);
}

// ── Find existing note for an ASIN (to pre-fill form on re-open) ──────────────
function _findExistingNote(ss, asin, brand) {
  const sheet = ss.getSheetByName("Pipeline Actions");
  if (!sheet) return null;
  const data = sheet.getDataRange().getValues();
  for (let i = 1; i < data.length; i++) {
    if (String(data[i][PA_COL.asin]).trim()  === asin &&
        String(data[i][PA_COL.brand]).trim() === brand) {
      return {
        notes: String(data[i][PA_COL.notes] || ""),
        price: String(data[i][PA_COL.price] || ""),
        date:  String(data[i][PA_COL.date]  || ""),
      };
    }
  }
  return null;
}

// ── Compact note card ─────────────────────────────────────────────────────────
function _noteCard(asin, brand, title, rec, by, existing) {
  const scriptUrl  = ScriptApp.getService().getUrl();

  // skipUrl bakes the current `by` value into the URL; if the user edits their
  // name via "Not you?" and then clicks Skip, JS updates the skip link too.
  const baseConfirmUrl = scriptUrl
    + "?action=pipeline_confirm"
    + "&asin="  + encodeURIComponent(asin)
    + "&brand=" + encodeURIComponent(brand)
    + "&title=" + encodeURIComponent(title)
    + "&rec="   + encodeURIComponent(rec);

  const isUpdate    = !!existing;
  const prefillNote = isUpdate && existing.notes !== "As recommended" ? existing.notes : "";
  const prefillPrice= isUpdate ? existing.price : "";
  const headerText  = isUpdate
    ? "Update what you did — <em>" + _esc(asin) + "</em>"
    : "What will you do? — <em>" + _esc(asin) + "</em>";
  const subText = isUpdate
    ? "Previously logged on " + _esc(existing.date) + ". Edit below to update."
    : "Leave blank and Skip if not sure yet — you can always come back to this email and click Actioned again.";

  // "Logging as" row — only shown when by is known.
  // "Not you?" reveals a name input; typing there overrides the hidden by field
  // and updates the Skip link in real time.  Zero impact when by is empty.
  const bySection = by ? `
  <div style="font-size:11px; color:#888; margin:-8px 0 12px 0; line-height:1.8;">
    Logging as <strong id="by-display">${_esc(by)}</strong>
    <a href="#" id="not-you-link"
       style="color:#aaa; font-size:10px; margin-left:6px; text-decoration:underline;"
       onclick="toggleByEdit(event)">Not you?</a>
    <span id="by-edit-row" style="display:none; margin-top:4px;">
      <input type="text" id="by-input" value="${_esc(by)}"
             style="width:130px; padding:3px 7px; border:1px solid #ccc;
                    border-radius:3px; font-size:12px; color:#333;"
             oninput="onByInput(this.value)"
             placeholder="Your name"/>
      <a href="#" onclick="toggleByEdit(event)"
         style="font-size:10px; color:#888; margin-left:5px;">Done</a>
    </span>
  </div>` : "";

  const html = `<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Log Action</title>
  <style>
    * { box-sizing:border-box; margin:0; padding:0; }
    body {
      font-family: Arial, sans-serif;
      background: #f0f4f8;
      display: flex; align-items: flex-start; justify-content: center;
      min-height: 100vh; padding: 24px 16px;
    }
    .card {
      background: #fff;
      border-radius: 8px;
      padding: 20px 22px 16px;
      box-shadow: 0 2px 12px rgba(0,0,0,0.12);
      width: 100%; max-width: 400px;
    }
    .header { font-size: 13px; font-weight: bold; color: #155724; margin-bottom: 3px; }
    .sub    { font-size: 11px; color: #888; margin-bottom: 10px; line-height: 1.5; }
    .chip {
      display:inline-block; background:#d4edda; color:#155724;
      border:1px solid #c3e6cb; border-radius:3px;
      font-size:10px; font-weight:bold; padding:1px 6px; margin-left:5px;
    }
    label { font-size: 11px; font-weight: bold; color: #555; display:block; margin-bottom:4px; }
    textarea {
      width:100%; border:1px solid #ccc; border-radius:4px;
      padding:8px 10px; font-size:12px; color:#333;
      font-family:Arial,sans-serif; resize:vertical; min-height:64px;
    }
    textarea:focus { outline:none; border-color:#155724; }
    .price-row { display:flex; gap:10px; margin-top:10px; align-items:flex-end; }
    .price-row > div { flex:1; }
    input[type=number] {
      width:100%; border:1px solid #ccc; border-radius:4px;
      padding:7px 10px; font-size:12px; color:#333;
    }
    input[type=number]:focus { outline:none; border-color:#155724; }
    .btn-row { display:flex; gap:8px; margin-top:14px; }
    .btn-submit {
      flex:2; padding:9px 0; background:#155724; color:#fff;
      border:none; border-radius:4px; font-size:13px; font-weight:bold; cursor:pointer;
    }
    .btn-submit:hover { background:#1e7e34; }
    .btn-skip {
      flex:1; padding:9px 0; background:#f5f5f5; color:#666;
      border:1px solid #ccc; border-radius:4px; font-size:12px; cursor:pointer;
      text-decoration:none; text-align:center; display:block; line-height:1.2;
    }
    .btn-skip:hover { background:#eee; }
    .footer-note { font-size:10px; color:#bbb; text-align:center; margin-top:10px; }
  </style>
</head>
<body>
  <div class="card">
    <div class="header">${headerText}<span class="chip">${_esc(rec)}</span></div>
    <div class="sub">${subText}</div>
    ${bySection}

    <form method="GET" action="${_esc(scriptUrl)}" id="action-form">
      <input type="hidden" name="action"  value="pipeline_confirm"/>
      <input type="hidden" name="asin"    value="${_esc(asin)}"/>
      <input type="hidden" name="brand"   value="${_esc(brand)}"/>
      <input type="hidden" name="title"   value="${_esc(title)}"/>
      <input type="hidden" name="rec"     value="${_esc(rec)}"/>
      <input type="hidden" name="by"      id="by-hidden" value="${_esc(by)}"/>

      <label>What did / will you do?</label>
      <textarea name="notes" placeholder="e.g. Raised to $62.99 (+5%) as suggested&#10;or: Raised only 3% — testing first&#10;or: Decided to hold — watching velocity">${_esc(prefillNote)}</textarea>

      <div class="price-row">
        <div>
          <label>Actual price set <span style="font-weight:normal;">(optional)</span></label>
          <input type="number" name="actual_price" step="0.01" min="0"
                 placeholder="e.g. 61.50"
                 value="${_esc(prefillPrice)}"/>
        </div>
      </div>

      <div class="btn-row">
        <button type="submit" class="btn-submit">&#10003; Submit</button>
        <a href="#" id="skip-link" class="btn-skip"
           onclick="doSkip(event)">Skip<br/><span style="font-size:10px;">use recommendation</span></a>
      </div>
    </form>

    <div class="footer-note">Come back and click Actioned again to update this.</div>
  </div>

  <script>
    // Base URL for skip — by= appended dynamically so it stays in sync with
    // whatever name the user has set (original or overridden via Not you?).
    var _baseSkip = ${JSON.stringify(baseConfirmUrl + "&notes=")};
    var _currentBy = ${JSON.stringify(by || "")};

    function _buildSkipUrl(name) {
      return _baseSkip + (name ? "&by=" + encodeURIComponent(name) : "");
    }

    // Initialise skip link with the pre-filled name
    document.getElementById("skip-link").href = _buildSkipUrl(_currentBy);

    function toggleByEdit(e) {
      e.preventDefault();
      var link = document.getElementById("not-you-link");
      var row  = document.getElementById("by-edit-row");
      var open = row.style.display === "none";
      row.style.display = open ? "inline" : "none";
      link.style.display = open ? "none" : "inline";
      if (open) { document.getElementById("by-input").focus(); }
    }

    function onByInput(val) {
      _currentBy = val.trim();
      document.getElementById("by-hidden").value   = _currentBy;
      document.getElementById("by-display").textContent = _currentBy || "—";
      document.getElementById("skip-link").href    = _buildSkipUrl(_currentBy);
    }

    function doSkip(e) {
      e.preventDefault();
      window.location.href = _buildSkipUrl(_currentBy);
    }
  </script>
</body>
</html>`;

  return HtmlService
    .createHtmlOutput(html)
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function _appendRow(ss, tabName, headers, row) {
  let sheet = ss.getSheetByName(tabName);
  if (!sheet) {
    sheet = ss.insertSheet(tabName);
    sheet.appendRow(headers);
    sheet.getRange(1, 1, 1, headers.length).setFontWeight("bold").setBackground("#f0f4f8");
    sheet.setFrozenRows(1);
  } else {
    // Migration: ensure "By" header exists on older tabs
    const existingHeaders = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
    if (!existingHeaders.includes("By")) {
      const nextCol = sheet.getLastColumn() + 1;
      sheet.getRange(1, nextCol).setValue("By").setFontWeight("bold").setBackground("#f0f4f8");
    }
  }
  sheet.appendRow(row);
}

function _esc(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function _page(title, msg, color, autoClose) {
  const closeScript = autoClose
    ? "setTimeout(function(){ try{ window.close(); }catch(e){} }, 2200);"
    : "";
  const fallback = autoClose
    ? "<p class='note'>Closing… <a href='javascript:window.close()'>close now</a></p>"
    : "";

  const html = `<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${_esc(title)}</title>
  <style>
    * { box-sizing:border-box; margin:0; padding:0; }
    body {
      font-family:Arial,sans-serif; background:#f0f4f8;
      display:flex; align-items:center; justify-content:center; min-height:100vh;
    }
    .card {
      background:#fff; border-radius:8px; padding:32px 36px;
      text-align:center; box-shadow:0 2px 12px rgba(0,0,0,0.12); max-width:320px;
    }
    h2 { color:${color}; font-size:18px; margin-bottom:10px; }
    p  { color:#555; font-size:13px; line-height:1.6; margin-bottom:8px; }
    p.note { color:#bbb; font-size:11px; margin-top:14px; }
    a  { color:${color}; }
  </style>
</head>
<body>
  <div class="card">
    <h2>${_esc(title)}</h2>
    <p>${msg}</p>
    ${fallback}
  </div>
  <script>${closeScript}</script>
</body>
</html>`;

  return HtmlService
    .createHtmlOutput(html)
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}
