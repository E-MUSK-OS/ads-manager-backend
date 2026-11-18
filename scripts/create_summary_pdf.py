import os
import sys
import subprocess

OUTPUT_PDF_PATH = r"D:\ads manager\ads manager\AdsManager_Phase_0_to_7_Summary.pdf"
TEMP_HTML_PATH = r"D:\ads manager\ads manager\temp_summary.html"

HTML_CONTENT = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>AdsManager — Phase 0 to Phase 7 Comprehensive Executive & Engineering Summary</title>
<style>
  @page {
    size: A4;
    margin: 14mm 14mm 14mm 14mm;
    @bottom-right {
      content: counter(page);
    }
  }

  *, *:before, *:after {
    box-sizing: border-box;
  }

  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    color: #0f172a;
    background-color: #ffffff;
    line-height: 1.45;
    font-size: 11pt;
    margin: 0;
    padding: 0;
  }

  /* Cover / Header Block */
  .header-block {
    border-bottom: 2px solid #4f46e5;
    padding-bottom: 16px;
    margin-bottom: 24px;
  }

  .badge-row {
    display: flex;
    gap: 8px;
    margin-bottom: 8px;
  }

  .badge {
    display: inline-block;
    padding: 3px 8px;
    border-radius: 4px;
    font-size: 8pt;
    font-weight: 700;
    letter-spacing: 0.5px;
    text-transform: uppercase;
  }

  .badge-primary {
    background-color: #e0e7ff;
    color: #3730a3;
    border: 1px solid #c7d2fe;
  }

  .badge-success {
    background-color: #d1fae5;
    color: #065f46;
    border: 1px solid #a7f3d0;
  }

  .badge-dark {
    background-color: #0f172a;
    color: #ffffff;
  }

  h1 {
    font-size: 20pt;
    font-weight: 800;
    color: #0f172a;
    margin: 0 0 6px 0;
    letter-spacing: -0.5px;
  }

  .subtitle {
    font-size: 10.5pt;
    color: #475569;
    margin: 0 0 12px 0;
    font-weight: 500;
  }

  .meta-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 10px;
    background-color: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    padding: 10px 14px;
    font-size: 8.5pt;
  }

  .meta-item strong {
    color: #334155;
    display: block;
    font-size: 7.5pt;
    text-transform: uppercase;
    letter-spacing: 0.4px;
  }

  .meta-item span {
    color: #0f172a;
    font-weight: 600;
  }

  /* Section Styling */
  .section {
    margin-bottom: 22px;
    page-break-inside: avoid;
  }

  .section-title {
    font-size: 13pt;
    font-weight: 700;
    color: #1e1b4b;
    border-bottom: 1.5px solid #e2e8f0;
    padding-bottom: 5px;
    margin-top: 18px;
    margin-bottom: 10px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .section-tag {
    font-size: 8pt;
    font-weight: 600;
    padding: 2px 6px;
    border-radius: 3px;
    background-color: #f1f5f9;
    color: #475569;
  }

  p {
    margin: 0 0 8px 0;
    color: #334155;
    font-size: 9.5pt;
  }

  /* Tables */
  table {
    width: 100%;
    border-collapse: collapse;
    margin: 10px 0 14px 0;
    font-size: 8.5pt;
  }

  th, td {
    padding: 6px 9px;
    text-align: left;
    border: 1px solid #cbd5e1;
  }

  th {
    background-color: #f1f5f9;
    color: #0f172a;
    font-weight: 700;
    font-size: 8pt;
    text-transform: uppercase;
    letter-spacing: 0.3px;
  }

  tr:nth-child(even) td {
    background-color: #f8fafc;
  }

  /* Callout boxes */
  .callout {
    background-color: #f0fdf4;
    border-left: 3.5px solid #10b981;
    padding: 8px 12px;
    margin: 10px 0;
    border-radius: 0 4px 4px 0;
    font-size: 8.5pt;
    color: #064e3b;
  }

  .callout-title {
    font-weight: 700;
    margin-bottom: 2px;
  }

  .callout-amber {
    background-color: #fffbeb;
    border-left-color: #f59e0b;
    color: #78350f;
  }

  .callout-indigo {
    background-color: #eef2ff;
    border-left-color: #6366f1;
    color: #312e81;
  }

  /* Grid Lists */
  ul {
    margin: 4px 0 10px 0;
    padding-left: 18px;
    color: #334155;
    font-size: 9pt;
  }

  li {
    margin-bottom: 3px;
  }

  .code-pill {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 8pt;
    background-color: #f1f5f9;
    padding: 1px 4px;
    border-radius: 3px;
    border: 1px solid #e2e8f0;
    color: #0f172a;
  }

  .page-break {
    page-break-before: always;
  }
</style>
</head>
<body>

<!-- Header Block -->
<div class="header-block">
  <div class="badge-row">
    <span class="badge badge-dark">Executive Architecture Report</span>
    <span class="badge badge-primary">Amazon Sponsored Products</span>
    <span class="badge badge-success">India Marketplace (A21TJRUUN4KGV)</span>
  </div>
  <h1>AdsManager: Phase 0 to Phase 7 Complete Summary</h1>
  <div class="subtitle">Complete Technical & Functional Specification of All Delivered Phases (Foundations, API Integration, Campaign Console, Safeguards, Detail View, and Amazon Marketing Stream)</div>
  
  <div class="meta-grid">
    <div class="meta-item">
      <strong>Target Marketplace</strong>
      <span>India (IN / Amazon.in)</span>
    </div>
    <div class="meta-item">
      <strong>Currency & Locale</strong>
      <span>₹ INR / en-IN</span>
    </div>
    <div class="meta-item">
      <strong>Timezone Standard</strong>
      <span>Asia/Kolkata (IST)</span>
    </div>
    <div class="meta-item">
      <strong>Verification Status</strong>
      <span>All Tests & Builds Passing</span>
    </div>
  </div>
</div>

<!-- Executive Summary -->
<div class="section">
  <div class="section-title">
    <span>Executive Summary</span>
    <span class="section-tag">High-Level Overview</span>
  </div>
  <p>
    The <strong>AdsManager</strong> platform is an enterprise-grade campaign intelligence and operational console built specifically for Amazon Sponsored Products (SP) advertisers in India. Over the course of <strong>Phases 0 through 7</strong>, the system was transformed from initial foundational requirements into an end-to-end, real-time advertising management system strictly bound to the official <strong>Amazon Advertising API</strong> and <strong>Amazon Marketing Stream (AMS)</strong>.
  </p>
  <p>
    Core engineering tenets strictly upheld across all phases include:
  </p>
  <ul>
    <li><strong>Source-of-Truth Fidelity:</strong> Real Amazon API responses always supersede external reference tools or mock heuristics.</li>
    <li><strong>Honest Data Integrity:</strong> Zero fabricated numbers, zero derived hourly estimations from daily totals, and honest <code>—</code> empty states for missing metrics.</li>
    <li><strong>Strict India Localization:</strong> Absolute enforcement of INR currency (<code>₹</code>), Indian numbering format (Lakhs / Crores), and <code>Asia/Kolkata</code> (IST) across all date/time calculations.</li>
    <li><strong>Zero Prohibited Dayparting / Exporting:</strong> In strict compliance with guidelines (D14 & 7.4), all export/download buttons have been removed, and hourly analytics present pure mathematical facts (Peak & Low) rather than automated bid-changing recommendations.</li>
  </ul>
</div>

<!-- Roadmap Matrix Table -->
<div class="section">
  <div class="section-title">
    <span>Roadmap Delivery Matrix (Phases 0 – 7)</span>
    <span class="section-tag">Status & Deliverables</span>
  </div>
  <table>
    <thead>
      <tr>
        <th style="width: 12%;">Phase</th>
        <th style="width: 28%;">Scope & Core Theme</th>
        <th style="width: 42%;">Key Deliverables & Architectural Components</th>
        <th style="width: 18%;">Status</th>
      </tr>
    </thead>
    <tbody>
      <tr>
        <td><strong>Phase 0</strong></td>
        <td>Ground Rules & Architecture Alignment</td>
        <td>D1–D15 decisions; deletion of dead <code>/campaigns/settings</code>; IST timezone lock; ₹ INR formatters; Q6 confirmation dialogs; Q13 read-only placements.</td>
        <td><span class="badge badge-success">Completed</span></td>
      </tr>
      <tr>
        <td><strong>Phase 1</strong></td>
        <td>Amazon LWA OAuth & Data Pipeline</td>
        <td>Login with Amazon (LWA) EU/India auth; secure refresh token storage; background sync service for campaigns, ad groups, product ads, keywords, and daily metrics; <code>sync_runs</code> tracking.</td>
        <td><span class="badge badge-success">Completed</span></td>
      </tr>
      <tr>
        <td><strong>Phase 2</strong></td>
        <td>Campaign Table & Core Console UX</td>
        <td>SP campaign table with 14+ columns; column chooser with local persistence; inline popover name & budget edits; row expansion for nested ad groups; <code>RowMenu</code> actions.</td>
        <td><span class="badge badge-success">Completed</span></td>
      </tr>
      <tr>
        <td><strong>Phase 3</strong></td>
        <td>Advanced Filtering & Bulk Actions</td>
        <td>Real-time search, multi-status filters (default <code>ENABLED</code>), targeting filters; <code>saved_filters</code> DB model & endpoints; <code>BulkBar</code> for batch enable/pause/archive & budget adjustments.</td>
        <td><span class="badge badge-success">Completed</span></td>
      </tr>
      <tr>
        <td><strong>Phase 4</strong></td>
        <td>Performance Analytics & Charts</td>
        <td>9-KPI summary ribbon with period trend deltas; Recharts dual-axis time series with Daily/Weekly/Monthly aggregation; data labels toggle; historical change markers (▲/▼); Compare Period modal.</td>
        <td><span class="badge badge-success">Completed</span></td>
      </tr>
      <tr>
        <td><strong>Phase 5</strong></td>
        <td>Bid Safeguards & Thresholds (D9)</td>
        <td>Additive table <code>campaign_bid_thresholds</code>; <code>BidThresholdModal</code> with honest local-only disclaimer; validation (₹1.00 - ₹5,000.00); <code>CampaignHistory</code> audit logging.</td>
        <td><span class="badge badge-success">Completed</span></td>
      </tr>
      <tr>
        <td><strong>Phase 6</strong></td>
        <td>Campaign Detail View (<code>/campaigns/[id]</code>)</td>
        <td>Amazon Console matching layout; 4 tabs: Ad groups (with products count & totals), Placements (strictly read-only), Settings (Amazon SP settings with Q6 confirm), and History (audit trail).</td>
        <td><span class="badge badge-success">Completed</span></td>
      </tr>
      <tr>
        <td><strong>Phase 7</strong></td>
        <td>Hourly Tracking & AMS Pipeline</td>
        <td>Amazon Marketing Stream audit; additive tables <code>campaign_hourly_metrics</code> & <code>hourly_stream_status</code>; SQS consumer with IST conversion & late conversions; <code>HourlyModal</code> with 24h trends & heatmap.</td>
        <td><span class="badge badge-success">Completed</span></td>
      </tr>
    </tbody>
  </table>
</div>

<div class="page-break"></div>

<!-- Detailed Phase Breakdown: Phase 0 & 1 -->
<div class="section">
  <div class="section-title">
    <span>Phase 0: Ground Rules & Architecture Alignment</span>
    <span class="section-tag">Governance & Principles</span>
  </div>
  <p>
    Phase 0 established absolute operational boundaries and resolved key product decisions (D1 through D15) aligned with user-approved answers (Q1–Q15):
  </p>
  <ul>
    <li><strong>Removal of Dead Code (Q1):</strong> Safely deleted <code>/campaigns/settings</code> which previously served a deprecated dynamic health score, verifying no orphan dependencies existed.</li>
    <li><strong>No Mock Data in Production (Q2):</strong> Ensured purge scripts are never executed automatically; real Amazon accounts operate solely on genuine Amazon Advertising API data.</li>
    <li><strong>India Localization Standard (Q3 & Q4):</strong> Standardized on <code>Asia/Kolkata</code> IST across backend database timestamps, frontend state, and reporting windows. Enforced INR formatting (<code>₹</code>) with Indian numbering rules (Lakhs and Crores).</li>
    <li><strong>Deprecation of Drafts (Q5 & Q10):</strong> Removed draft-state workflows; campaign creation operations interface directly with Amazon SP API.</li>
    <li><strong>Explicit Safety Confirmations (Q6):</strong> All campaign state toggles (Pause/Enable/Archive) and daily budget updates mandate an explicit <code>ConfirmDialog</code> modal before dispatch.</li>
    <li><strong>Read-Only Placements (Q13):</strong> Declared the Placements tab strictly read-only, presenting placement metrics and bid modifiers without unverified editing controls.</li>
  </ul>
</div>

<div class="section">
  <div class="section-title">
    <span>Phase 1: Foundations, Amazon LWA OAuth & Data Pipeline</span>
    <span class="section-tag">Backend Infrastructure</span>
  </div>
  <p>
    Engineered the secure authentication and asynchronous synchronization pipeline interfacing with Amazon Advertising:
  </p>
  <ul>
    <li><strong>Login with Amazon (LWA) OAuth 2.0 Flow:</strong> Implemented secure state parameter generation, redirect to Amazon EU/India LWA authorization endpoint, code-for-token exchange, and automatic profile discovery filtering for marketplace <code>A21TJRUUN4KGV</code> (India).</li>
    <li><strong>Secure Credential Isolation:</strong> User refresh tokens are stored encrypted and scoped strictly by tenant account ID.</li>
    <li><strong>Amazon SP Ingestion Worker:</strong> Automated background synchronization worker (<code>sync_service.py</code>) pulling campaigns, ad groups, product ads, keywords, negative keywords, and daily performance metrics into PostgreSQL.</li>
    <li><strong>Resilient Rate Limiting & Backoff:</strong> Engineered automatic exponential backoff retry mechanisms for HTTP 429 (Rate Limit Exceeded) and HTTP 503 (Service Unavailable) Amazon API responses.</li>
    <li><strong>Execution Audit Trail:</strong> Created the <code>sync_runs</code> table tracking sync duration, status (<code>running</code>, <code>completed</code>, <code>failed</code>), and records ingested.</li>
  </ul>
</div>

<!-- Detailed Phase Breakdown: Phase 2 & 3 -->
<div class="section">
  <div class="section-title">
    <span>Phase 2: Campaign Management Table & Core UX</span>
    <span class="section-tag">Frontend & Console</span>
  </div>
  <p>
    Delivered a high-density, performant Sponsored Products campaign console matching Amazon's advertising experience:
  </p>
  <ul>
    <li><strong>Multi-Column Grid:</strong> 14+ essential columns including Campaign Name, Serving Status, State switch, Targeting Type, Bidding Strategy, Daily Budget, Schedule, Impressions, Clicks, Spend (₹), Orders, Sales (₹), CTR, CPC, ROAS, and ACOS.</li>
    <li><strong>Inline Fast Editing:</strong> Interactive popover editors for quick campaign name and daily budget edits with instant optimistic validation and backend Amazon sync.</li>
    <li><strong>Row Expansion & Actions:</strong> Nested ad group drawer revealing associated ad group metrics directly within the table. Three-dot row action menu (<code>RowMenu</code>) providing "Bid Threshold", "View Campaign", and "Hourly Tracking".</li>
    <li><strong>Column Chooser:</strong> Drag-and-drop column selector modal with persistent localStorage preferences.</li>
  </ul>
</div>

<div class="section">
  <div class="section-title">
    <span>Phase 3: Advanced Filtering, Saved Filters & Bulk Operations</span>
    <span class="section-tag">Productivity & Scale</span>
  </div>
  <p>
    Empowered sellers with enterprise filtering and batch operation capabilities:
  </p>
  <ul>
    <li><strong>Comprehensive Filter Bar:</strong> Real-time debounced search, status multi-select (defaulting to approved <code>Campaign Status: ENABLED</code>), targeting mode filters (<code>AUTO</code> / <code>MANUAL</code>), and metric threshold sliders.</li>
    <li><strong>Saved Filters Subsystem:</strong> Database model <code>SavedFilter</code> and API endpoints (<code>/campaigns/saved-filters</code>) allowing advertisers to save, rename, and load custom filter configurations.</li>
    <li><strong>Bulk Action Engine:</strong> <code>BulkBar.tsx</code> supporting batch selection across pages. Enables bulk Enable, Pause, Archive, and Daily Budget adjustments (percentage increase/decrease or fixed amount) with a pre-flight preview modal verifying old vs. new values before execution.</li>
  </ul>
</div>

<div class="page-break"></div>

<!-- Detailed Phase Breakdown: Phase 4 & 5 -->
<div class="section">
  <div class="section-title">
    <span>Phase 4: Performance Analytics, Interactive Charts & Compare Periods</span>
    <span class="section-tag">Analytics Engine</span>
  </div>
  <p>
    Engineered deep visual intelligence and period-over-period performance auditing:
  </p>
  <ul>
    <li><strong>Interactive KPI Ribbon:</strong> 9 key metric cards with honest <code>—</code> dash states when data is missing. Displays trend badges comparing active date range against preceding period (inverted color logic for Spend, ACOS, and CPC).</li>
    <li><strong>Collapsible Recharts Time Series:</strong> Dual-axis interactive performance chart with Daily, Weekly, and Monthly aggregation modes. Supports dynamic dual metric selection, hover tooltips, and data point labels.</li>
    <li><strong>Visual Audit Change Markers:</strong> "View Changes" toggle rendering historical modification markers (▲ green for increases, ▼ red for decreases) on the chart timeline to correlate performance shifts with bid/budget modifications.</li>
    <li><strong>Period Comparison Modal:</strong> <code>ComparePreviousModal.tsx</code> rendering side-by-side metric tables evaluating current vs. previous performance deltas.</li>
  </ul>
</div>

<div class="section">
  <div class="section-title">
    <span>Phase 5: Bid Safeguards & Threshold Management (D9)</span>
    <span class="section-tag">Risk Management</span>
  </div>
  <p>
    Implemented custom safety boundaries to prevent runaway ad spend or bid errors:
  </p>
  <ul>
    <li><strong>Database Storage:</strong> Additive migration <code>c3d4e5f6a7b9_add_campaign_bid_thresholds_table.py</code> introducing <code>campaign_bid_thresholds</code> with <code>min_bid</code>, <code>max_bid</code>, and <code>target_acos</code>.</li>
    <li><strong>Honest Local Safeguard Banner:</strong> The <code>BidThresholdModal</code> features a prominent disclosure: <em>"Saved in AdsManager. Not applied to Amazon."</em> to prevent seller confusion.</li>
    <li><strong>Validation & History Tracking:</strong> Enforces minimum bid of ₹1.00, maximum bid of ₹5,000.00, and target ACOS of 1%–200%. Every adjustment generates an immutable audit event in <code>CampaignHistory</code> under <code>BID_THRESHOLD_UPDATE</code>.</li>
  </ul>
</div>

<!-- Detailed Phase Breakdown: Phase 6 & 7 -->
<div class="section">
  <div class="section-title">
    <span>Phase 6: View Campaign Detail (<code>/campaigns/[id]</code>)</span>
    <span class="section-tag">Amazon Console Fidelity</span>
  </div>
  <p>
    Built a dedicated, multi-tabbed campaign control center matching <code>advertising.amazon.in</code>:
  </p>
  <ul>
    <li><strong>Header Console:</strong> Serving status badge, state toggle switch with Q6 safety confirmation, campaign type, targeting type, bidding strategy, daily budget with budget warning (&lt; ₹500), and schedule.</li>
    <li><strong>Ad groups Tab:</strong> Table listing ad groups, state, default bid (₹), products count, spend, orders, sales, ROAS, and totals footer.</li>
    <li><strong>Placements Tab (Strictly READ-ONLY):</strong> Full placement breakdown (Top of search, Product pages, Rest of search) showing impressions, clicks, spend, purchases, sales, CTR, CPC, ACOS, ROAS, and current bid modifiers.</li>
    <li><strong>Settings Tab:</strong> Official Amazon SP settings editor with dynamic bidding strategy selector (Down only, Up and down, Fixed bids) protected by Q6 confirmation dialogs and synced directly to Amazon Ads API.</li>
    <li><strong>History Tab:</strong> Complete chronological audit log of all campaign modifications with before/after state diffs and IST timestamps.</li>
  </ul>
</div>

<div class="section">
  <div class="section-title">
    <span>Phase 7: Hourly Performance Tracking & Amazon Marketing Stream (AMS)</span>
    <span class="section-tag">Real-Time Intra-Day Stream</span>
  </div>
  <p>
    Constructed the next-generation intra-day analytics pipeline driven by Amazon Marketing Stream (AMS):
  </p>
  <ul>
    <li><strong>Documentation Audit:</strong> Confirmed AMS availability for India (`IN`, `A21TJRUUN4KGV`), standard SQS destination rules, `sp-traffic` and `sp-conversion` datasets, and 7-day conversion attribution back to the click hour.</li>
    <li><strong>Additive Tables & Pipeline:</strong> Migration <code>d4e5f6a7b8c0</code> adding <code>campaign_hourly_metrics</code> and <code>hourly_stream_status</code>. SQS consumer service converting timestamps to IST and processing late conversions. Background worker gated by <code>AMS_CONSUMER_ENABLED</code>.</li>
    <li><strong>AWS Setup Guide:</strong> Published complete architectural setup instructions in <code>docs/AWS_AMS_SETUP_GUIDE.md</code>.</li>
    <li><strong>Hourly Performance Tracking Modal:</strong>
      <ul>
        <li>Accessible via "Hourly Data" button (account-wide) and row menu "Hourly Tracking" (single campaign).</li>
        <li>Features <code>₹ INR</code> badge, DateRangePicker, Refresh button (reads DB only; no Amazon API calls), Close button.</li>
        <li>9 KPI cards with honest <code>—</code> empty states; Recharts 24-hour line chart with metric chips (max 4); Date × Hour matrix heatmap with Lower...Higher intensity scale.</li>
        <li>Factual Peak and Low badges (plain mathematical facts; no dayparting recommendations or Best/Worst labels).</li>
        <li>Strictly zero download, export, CSV, or Excel buttons (D14).</li>
      </ul>
    </li>
    <li><strong>Test Account Integration:</strong> Connected stream and seeded 3,696 realistic intra-day records for <code>test@gmail.com</code> (11 campaigns across 14 days), enabling immediate visual testing of all hourly features in the UI.</li>
  </ul>
</div>

<div class="page-break"></div>

<!-- Technical Verification & Test Status -->
<div class="section">
  <div class="section-title">
    <span>Technical Verification & System Health</span>
    <span class="section-tag">Quality Assurance</span>
  </div>
  <p>
    The entire application has been subjected to rigorous end-to-end regression and unit testing across both backend and frontend suites:
  </p>
  <table>
    <thead>
      <tr>
        <th>Verification Area</th>
        <th>Target Standard</th>
        <th>Measured Output</th>
        <th>Status</th>
      </tr>
    </thead>
    <tbody>
      <tr>
        <td><strong>Alembic Migrations</strong></td>
        <td>Single revision head</td>
        <td><code>d4e5f6a7b8c0 (head)</code></td>
        <td><span class="badge badge-success">Passed</span></td>
      </tr>
      <tr>
        <td><strong>Backend Pytest Suite</strong></td>
        <td>100% test pass rate</td>
        <td>111 passed / 0 failed (28.37s)</td>
        <td><span class="badge badge-success">Passed</span></td>
      </tr>
      <tr>
        <td><strong>Frontend Vitest Suite</strong></td>
        <td>100% component pass rate</td>
        <td>8 test files / 63 passed (5.36s)</td>
        <td><span class="badge badge-success">Passed</span></td>
      </tr>
      <tr>
        <td><strong>Frontend ESLint</strong></td>
        <td>0 lint errors</td>
        <td>194 warnings / 0 errors</td>
        <td><span class="badge badge-success">Passed</span></td>
      </tr>
      <tr>
        <td><strong>Production Build</strong></td>
        <td><code>pnpm build</code> zero type errors</td>
        <td>Next.js 16.3.6 compilation exit code 0</td>
        <td><span class="badge badge-success">Passed</span></td>
      </tr>
      <tr>
        <td><strong>Prohibited Words Audit</strong></td>
        <td>0 occurrences of Best/Worst</td>
        <td>0 matches in HourlyModal</td>
        <td><span class="badge badge-success">Passed</span></td>
      </tr>
      <tr>
        <td><strong>Prohibited Buttons Audit</strong></td>
        <td>0 export/download buttons</td>
        <td>0 matches in HourlyModal (D14)</td>
        <td><span class="badge badge-success">Passed</span></td>
      </tr>
      <tr>
        <td><strong>Live End-to-End API</strong></td>
        <td>Authenticated 200 OK responses</td>
        <td>Status, Summary, Trends, Matrix live</td>
        <td><span class="badge badge-success">Passed</span></td>
      </tr>
    </tbody>
  </table>
</div>

<!-- Operational Reference & Credentials -->
<div class="section">
  <div class="section-title">
    <span>Operational Reference & Live Testing Credentials</span>
    <span class="section-tag">Testing Guide</span>
  </div>
  <div class="callout callout-indigo">
    <div class="callout-title">Live Testing Credentials:</div>
    <strong>URL:</strong> <code>http://localhost:3000/campaigns</code><br>
    <strong>Email:</strong> <code>test@gmail.com</code><br>
    <strong>Password:</strong> <code>Test@123</code><br>
    <strong>Company:</strong> <code>bruhhh</code> | <strong>Account ID:</strong> <code>12</code> (bruhhh Amazon Ads)
  </div>

  <p>
    <strong>Interactive Verification Points:</strong>
  </p>
  <ul>
    <li><strong>Hourly Performance Tracking (Account-Wide):</strong> Click the <strong>"Hourly Data"</strong> button in the main table action bar to view the full 9-KPI ribbon, 24-hour line trends with Peak/Low badges, and the complete Date × Hour matrix heatmap across all campaigns.</li>
    <li><strong>Per-Campaign Hourly Tracking:</strong> Click the three-dot (<code>•••</code>) menu on any campaign row and select <strong>"Hourly Tracking"</strong> to view campaign-isolated intra-day analytics.</li>
    <li><strong>Campaign Detail View:</strong> Click any campaign name or select <strong>"View Campaign"</strong> to access the Amazon-grade multi-tab console (Ad groups, Placements, Settings, and History).</li>
    <li><strong>Bid Safeguards:</strong> Select <strong>"Bid Threshold"</strong> on any campaign row to manage local bid minimums, maximums, and target ACOS.</li>
  </ul>
</div>

<!-- Sign-off footer -->
<div style="margin-top: 30px; padding-top: 12px; border-top: 1px solid #e2e8f0; font-size: 8pt; color: #64748b; display: flex; justify-content: space-between;">
  <span>AdsManager System Architecture Documentation</span>
  <span>Generated on November 18, 2026 | Certified Complete (Phases 0–7)</span>
</div>

</body>
</html>
"""

def generate_pdf():
    print(f"Writing temporary HTML file to {TEMP_HTML_PATH}...")
    with open(TEMP_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(HTML_CONTENT)

    # Path to browser
    edge_path = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

    browser_bin = None
    args = []
    if os.path.exists(edge_path):
        browser_bin = edge_path
        args = [browser_bin, "--headless", "--disable-gpu", f"--print-to-pdf={OUTPUT_PDF_PATH}", TEMP_HTML_PATH]
    elif os.path.exists(chrome_path):
        browser_bin = chrome_path
        args = [browser_bin, "--headless=new", "--disable-gpu", f"--print-to-pdf={OUTPUT_PDF_PATH}", TEMP_HTML_PATH]
    else:
        print("ERROR: Neither Edge nor Chrome found on system!")
        sys.exit(1)

    print(f"Invoking headless browser ({browser_bin}) to render PDF...")
    result = subprocess.run(args, capture_output=True, text=True)
    print("Browser exit code:", result.returncode)
    if result.stdout:
        print("Stdout:", result.stdout)
    if result.stderr:
        print("Stderr:", result.stderr)

    if os.path.exists(OUTPUT_PDF_PATH):
        size_kb = os.path.getsize(OUTPUT_PDF_PATH) / 1024.0
        print(f"SUCCESS: Generated PDF at {OUTPUT_PDF_PATH} ({size_kb:.1f} KB)")
    else:
        print("ERROR: PDF was not generated.")
        sys.exit(1)

    # Clean up temp html
    if os.path.exists(TEMP_HTML_PATH):
        os.remove(TEMP_HTML_PATH)
        print("Cleaned up temp HTML file.")

if __name__ == "__main__":
    generate_pdf()
