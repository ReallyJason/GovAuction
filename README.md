# GovAuctions Master Monitor: Continuous 10-Tab Priority Rotation & Live Dashboard

This system continuously monitors GovAuctions by discovering, indexing, and tracking **hundreds or thousands of battles** in a **Master Auction List**, while maintaining a strict hardware-friendly limit of **only 10 active browser tabs** in a persistent reusable pool.

---

### Key Architectural Concepts

| Concept | Implementation Details |
| :--- | :--- |
| **Master Battles Tracked** | **Hundreds or thousands** discovered from continuous StaticData #1 and StaticData #2 interception. |
| **Active Browser Tabs** | **Strict maximum of 10 tabs** running at once to prevent computer lag and CPU slowdown. |
| **Tab Reuse Mechanics** | Fixed pool of 10 tabs continuously recycled across batches via `tab.goto()` (no opening/closing hundreds of tabs). |
| **Permanent Data Retention** | When a tab switches from Auction 101 to 111, **Auction 101's data is never lost**—it remains permanently in the master record and continues to be displayed on the dashboard. |
| **Intelligent Priority Queue** | Battles with rapid bid activity (hot battles) are checked 3–4x more frequently, while quiet battles rotate fairly with built-in anti-starvation. |
| **Localhost Dashboard** | Displays the **entire Master List** of discovered battles with live 4-tier status badges, instant search, and filter pills. |

---

### Four-Tier Status System

Each auction card on the dashboard displays its real-time monitoring state:
* 🟢 **ACTIVE**: Currently open and being scraped in one of the 10 browser tabs right now (e.g. `🟢 ACTIVE (Tab 3)`).
* 🟡 **QUEUED**: In the top priority batch scheduled to be loaded in the next rotation.
* 🔵 **RECENTLY CHECKED**: Checked in a recent batch with collected bid history, highest bid, and unique bidders.
* ⚪ **WAITING**: Discovered in the master list, awaiting its turn in the priority cycle.

---

### Localhost Dashboard Features (`http://localhost:5000`)

The web dashboard is served locally and refreshes dynamically every 1.2 seconds without page reloads or layout shift:

#### 1. Real-Time Telemetry Banner
* **Active Tabs**: `10 / 10`
* **Total Discovered**: Total count of all battles in master list (e.g. `387`)
* **Queued Next**: Upcoming batch count (e.g. `10`)
* **Recently Checked**: Battles checked with collected bid histories
* **Last Cycle**: Timestamp of the latest batch completion
* **Last Static Refresh**: Timestamp of the latest staticData API interception

#### 2. Search & Interactive Filter Toolbar
* **Search Box**: Instant filter by auction ID, product title, or keyword.
* **Filter Pills**:
  * `All (387)`
  * `🟢 Active (10)`
  * `🟡 Queued (10)`
  * `🔵 Checked (N)`
  * `⚪ Waiting (N)`
  * `🔥 Hot Battles (N)`
* **Sort Dropdown**:
  * Status (Active first, then Queued, Checked, Waiting)
  * Highest Bid ($ High to Low)
  * Most Bidders
  * Last Checked (Most recent first)
  * Auction ID

#### 3. Responsive Auction Cards
* **Product Image**: Primary image with thumbnail gallery switcher for multi-angle previews.
* **Monospace ID Badge**: `ID: #17137916`
* **Status Badge**: 🟢 ACTIVE, 🟡 QUEUED, 🔵 CHECKED, ⚪ WAITING
* **Product Title**: Official item name.
* **Highest Bid**: Glowing emerald callout (e.g. `$197.98`), pulsing automatically with animation on new bids.
* **Unique Bidders Count**: Real-time counter pill of distinct users who placed bids.
* **Bulleted Bidders List**: Clean bulleted list of unique usernames (no duplicates) with golden highlight for target keywords.
* **Last Checked & Last Updated**: Real-time timestamps.
* **Gonzales URL**: Direct link to the raw JSON details endpoint.

---

### How to Run

Double-click [**`run_login.bat`**](file:///d:/MakingMoney/GovAuction/run_login.bat) or run from PowerShell:

```powershell
.\.venv\Scripts\python.exe govauction_login.py
```

#### Optional CLI Arguments:
* `--port 5000`: Port for localhost dashboard (default: `5000`).
* `--cycle-interval 3.0`: Seconds between rotation batch passes (default: `3.0`).
* `--headless`: Run browser headlessly without opening visible Chrome window.
* `-u <username> -p <password>`: Pass credentials directly to skip manual entry.

---

### Interactive Console Commands

While the system is running:
* **`queue`** / **`status`**: Prints a live formatted telemetry table showing the 10 active tabs, next queued batch, and total discovered battles.
* **`next`** / **`rotate`**: Forces an immediate rotation to the next batch of 10 auctions.
* **`poll`** / **`cycle`**: Triggers an immediate scrape pass on the current batch.
* **`dash`**: Displays the localhost dashboard URL and opens it in a browser tab.
* **`home`**: Reloads the GovAuctions home page to trigger fresh `staticData` requests.
* **`check`** (or press **Enter**): Reports keyword `"oneontopofyou"` match status.
* **`save`**: Forces an immediate save of [**`govauction_items.csv`**](file:///d:/MakingMoney/GovAuction/govauction_items.csv) and [**`govauction_items.json`**](file:///d:/MakingMoney/GovAuction/govauction_items.json).
* **`q`** / **`exit`**: Closes all tabs and browser cleanly.


