import argparse
import csv
import getpass
import json
import os
import queue
import sys
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

GOVAUCTIONS_HOME_URL = "https://www.govauctions.com/"
GOVAUCTIONS_LOGIN_URL = "https://www.govauctions.com/login"
GONZALES_BASE_URL = "https://www.govauctions.com/gonzales.php"

CHROME_PROFILE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "chrome_user_data"))
DASHBOARD_HTML_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "dashboard.html"))

TARGET_KEYWORD = "oneontopofyou"
MAX_ACTIVE_AUCTIONS = 10
DEFAULT_DASHBOARD_PORT = 5000


def console_input_worker(command_queue, stop_event):
    """
    Runs in a background thread to read console input without blocking
    the main Playwright event loop or cyclic processor.
    """
    while not stop_event.is_set():
        try:
            line = sys.stdin.readline()
            if not line:
                break
            cmd = line.strip()
            command_queue.put(cmd)
            if cmd.lower() in ["q", "quit", "exit"]:
                break
        except (EOFError, KeyboardInterrupt):
            command_queue.put("exit")
            break
        except Exception:
            break


class AuctionQueueEngine:
    """
    Master Auction Scaling & Continuous Priority Rotation Engine:
    - Tracks ALL discovered auctions from StaticData #1, StaticData #2, and repeated refreshes.
    - Limits concurrent browser tabs to a maximum of 10 persistent tabs in a reusable tab pool.
    - Reuses the 10 tabs across rotation batches (Batch 1 -> Batch 2 -> Batch 3 ...) via tab.goto().
    - Retains ALL collected data (highest bid, unique bidders, timestamps) permanently in the master record.
    - Intelligently prioritizes active battles (frequent bid changes) while guaranteeing anti-starvation.
    - Distinguishes 4 auction statuses: ACTIVE, QUEUED, RECENTLY CHECKED, WAITING.
    - Feeds full master records and telemetry to localhost dashboard and CSV/JSON exports.
    """
    def __init__(self, context=None, max_active=MAX_ACTIVE_AUCTIONS):
        self.context = context
        self.max_active = max_active

        self.master_auctions = {}       # aid (str) -> dict of full auction data (current session only)
        self.discovery_order = []       # list of aid in first-seen order
        self.tab_pool = []              # list of up to max_active Playwright Page objects
        self.active_batch = []          # list of up to 10 auction IDs currently open in tab_pool
        self.preview_next_batch = []    # list of top priority auction IDs queued for next batch

        self.last_static_refresh = "Waiting..."
        self.last_cycle_time = "Starting..."
        self.cycle_count = 0
        self.lock = threading.Lock()

        # Pure in-memory session: Never load old data from past runs
    def set_context(self, context):
        self.context = context

    @property
    def auction_queue(self):
        return self.discovery_order

    @property
    def active_slots(self):
        return self.active_batch

    @property
    def auction_records(self):
        return self.master_auctions

    def process_static_data(self, data, raw_url=""):
        """
        Reads both StaticData #1 and StaticData #2 (and repeated requests).
        Extracts id, name, and images.productImages for ALL battles.
        Adds brand-new auctions to master list without discarding existing ones.
        Refreshes product metadata (name, images) for existing auctions.
        """
        discovered_ids = []
        now = time.time()
        now_str = time.strftime("%I:%M:%S %p").lstrip("0")

        with self.lock:
            if isinstance(data, dict):
                items_to_check = []
                if "static" in data and isinstance(data["static"], dict):
                    items_to_check.extend(data["static"].items())
                for k, v in data.items():
                    if k != "static" and isinstance(v, dict):
                        items_to_check.append((k, v))

                for k, info in items_to_check:
                    aid = str(info.get("id") or k).strip()
                    if not aid:
                        continue

                    name = info.get("name") or info.get("title") or f"Auction #{aid}"

                    images_field = info.get("images", {})
                    product_images = []
                    if isinstance(images_field, dict):
                        product_images = images_field.get("productImages", [])
                    elif isinstance(images_field, list):
                        product_images = images_field
                    if not isinstance(product_images, list):
                        product_images = []

                    if not product_images:
                        single = info.get("image") or info.get("imageUrl")
                        if single:
                            product_images = [single]

                    primary_img = product_images[0] if product_images else ""

                    if aid not in self.discovery_order:
                        self.discovery_order.append(aid)
                    first_seen_order = self.discovery_order.index(aid) + 1

                    if aid in self.master_auctions:
                        rec = self.master_auctions[aid]
                        rec["name"] = name
                        if product_images:
                            rec["productImages"] = product_images
                            rec["primaryImage"] = primary_img
                    else:
                        self.master_auctions[aid] = {
                            "auctionId": aid,
                            "id": aid,
                            "name": name,
                            "firstSeenOrder": first_seen_order,
                            "firstSeenTime": now,
                            "firstSeenFormatted": now_str,
                            "productImages": product_images,
                            "primaryImage": primary_img,
                            "highestBid": 0.0,
                            "highestBidFormatted": "N/A",
                            "bidders": [],
                            "status": "waiting",
                            "activeSlot": None,
                            "lastChecked": None,
                            "lastCheckedTime": 0.0,
                            "lastUpdated": None,
                            "lastBidChangeTime": 0.0,
                            "activityScore": 0,
                            "checkCount": 0,
                            "gonzalesUrl": f"{GONZALES_BASE_URL}?idlist={aid}&auctionDetailsIds={aid}",
                            "battleUrl": f"https://www.govauctions.com/battle/{aid}"
                        }

                    discovered_ids.append(aid)

            # Check query parameters in URL
            if raw_url:
                parsed = urlparse(raw_url)
                qs = parse_qs(parsed.query)
                for q_id in (qs.get("auctionIds[]", []) or qs.get("auctionIds", [])):
                    q_id = str(q_id).strip()
                    if q_id and q_id not in discovered_ids:
                        discovered_ids.append(q_id)
                        if q_id not in self.discovery_order:
                            self.discovery_order.append(q_id)
                        q_first_seen_order = self.discovery_order.index(q_id) + 1

                        if q_id not in self.master_auctions:
                            self.master_auctions[q_id] = {
                                "auctionId": q_id,
                                "id": q_id,
                                "name": f"Auction #{q_id}",
                                "firstSeenOrder": q_first_seen_order,
                                "firstSeenTime": now,
                                "firstSeenFormatted": now_str,
                                "productImages": [],
                                "primaryImage": "",
                                "highestBid": 0.0,
                                "highestBidFormatted": "N/A",
                                "bidders": [],
                                "status": "waiting",
                                "activeSlot": None,
                                "lastChecked": None,
                                "lastCheckedTime": 0.0,
                                "lastUpdated": None,
                                "lastBidChangeTime": 0.0,
                                "activityScore": 0,
                                "checkCount": 0,
                                "gonzalesUrl": f"{GONZALES_BASE_URL}?idlist={q_id}&auctionDetailsIds={q_id}",
                                "battleUrl": f"https://www.govauctions.com/battle/{q_id}"
                            }

            new_count = 0
            for aid in discovered_ids:
                if aid not in self.discovery_order:
                    self.discovery_order.append(aid)
                    new_count += 1

            if len(self.active_batch) < min(self.max_active, len(self.master_auctions)):
                self.active_batch = self.select_next_batch(count=self.max_active)
                self.preview_next_batch = [aid for aid in self.select_next_batch(count=self.max_active * 2) if aid not in self.active_batch][:self.max_active]
            self.update_statuses_locked()

            if new_count > 0:
                print(f"\n[+] [StaticData Refresh] Added {new_count} new battle(s). Total discovered: {len(self.master_auctions)}")

    def sync_tab_pool(self):
        """
        Maintains exactly up to max_active (10) persistent Playwright browser tabs.
        Reuses closed tabs if necessary.
        """
        if not self.context:
            return

        with self.lock:
            valid_tabs = []
            for tab in self.tab_pool:
                try:
                    if not tab.is_closed():
                        valid_tabs.append(tab)
                except Exception:
                    pass
            self.tab_pool = valid_tabs

            needed = min(self.max_active, len(self.master_auctions)) - len(self.tab_pool)

        for _ in range(needed):
            try:
                new_tab = self.context.new_page()
                with self.lock:
                    self.tab_pool.append(new_tab)
            except Exception as e:
                print(f"[!] Notice creating browser tab: {e}")
                break

    def compute_priority(self, aid, now):
        """
        Intelligent priority calculation with anti-starvation:
        - Never-checked auctions get maximum priority (immediate initial sweep).
        - Frequently changing auctions (new bids/bidders in last 60s/5m or higher activityScore) get a heavy activity multiplier.
        - Quiet auctions steadily increase priority as time_since_last_checked grows, guaranteeing fair round-robin rotation across all battles.
        """
        rec = self.master_auctions.get(aid)
        if not rec:
            return -1.0

        if rec["checkCount"] == 0:
            idx = self.discovery_order.index(aid) if aid in self.discovery_order else 0
            return 1_000_000.0 - idx

        dt = max(1.0, now - rec["lastCheckedTime"])
        mult = 0.0

        if rec.get("lastBidChangeTime", 0) > 0:
            bid_age = now - rec["lastBidChangeTime"]
            if bid_age < 60:
                mult += 4.0  # High priority: bid change in last 60 seconds
            elif bid_age < 300:
                mult += 1.8  # Medium priority: bid change in last 5 minutes
            elif bid_age < 900:
                mult += 0.8  # Low priority: bid change in last 15 minutes

        # Activity score multiplier for battles with multiple bids
        mult += min(6.0, rec.get("activityScore", 0) * 1.5)

        return dt * (1.0 + mult)

    def select_next_batch(self, count=10):
        """
        Selects the top 'count' auctions with highest priority.
        If we have more auctions than count, avoids immediately re-selecting
        the currently active batch unless they have extreme priority.
        """
        now = time.time()
        scored = []
        for aid in self.master_auctions:
            score = self.compute_priority(aid, now)
            rec = self.master_auctions[aid]
            if rec.get("checkCount", 0) > 0 and len(self.master_auctions) > count and aid in self.active_batch:
                if (now - rec.get("lastBidChangeTime", 0)) > 60:
                    score = score * 0.1
            scored.append((score, aid))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [aid for _, aid in scored[:count]]

    def update_statuses_locked(self):
        """
        Updates each auction's status field:
        - 'active': Currently in active_batch (loaded in Tab 1..10)
        - 'queued': In preview_next_batch (scheduled for next cycle)
        - 'recently_checked': Checked within last 5 minutes (or has bids)
        - 'waiting': Awaiting turn
        """
        now = time.time()
        for aid, rec in self.master_auctions.items():
            if aid in self.active_batch:
                rec["status"] = "active"
                rec["activeSlot"] = self.active_batch.index(aid) + 1
            elif aid in self.preview_next_batch:
                rec["status"] = "queued"
                rec["activeSlot"] = None
            elif rec["lastCheckedTime"] > 0 and (now - rec["lastCheckedTime"]) < 300:
                rec["status"] = "recently_checked"
                rec["activeSlot"] = None
            else:
                rec["status"] = "waiting"
                rec["activeSlot"] = None
            rec["active"] = (rec["status"] == "active")

    def process_gonzales_history(self, aid, history_entries):
        """
        Processes bid history for an auction:
        - Updates highest bid if a higher bid is found.
        - Merges new unique bidder usernames (no duplicates).
        - Tracks lastChecked and lastUpdated timestamps.
        - Detects activity changes and updates lastBidChangeTime.
        """
        aid = str(aid).strip()
        if not isinstance(history_entries, list):
            return

        now = time.time()
        now_str = time.strftime("%I:%M:%S %p").lstrip("0")
        has_changed = False

        with self.lock:
            if aid not in self.master_auctions:
                if aid not in self.discovery_order:
                    self.discovery_order.append(aid)
                first_seen_order = self.discovery_order.index(aid) + 1

                self.master_auctions[aid] = {
                    "auctionId": aid,
                    "id": aid,
                    "name": f"Auction #{aid}",
                    "firstSeenOrder": first_seen_order,
                    "firstSeenTime": now,
                    "firstSeenFormatted": now_str,
                    "productImages": [],
                    "primaryImage": "",
                    "highestBid": 0.0,
                    "highestBidFormatted": "N/A",
                    "bidders": [],
                    "status": "waiting",
                    "activeSlot": None,
                    "lastChecked": now_str,
                    "lastCheckedTime": now,
                    "lastUpdated": None,
                    "lastBidChangeTime": 0.0,
                    "activityScore": 0,
                    "checkCount": 0,
                    "gonzalesUrl": f"{GONZALES_BASE_URL}?idlist={aid}&auctionDetailsIds={aid}",
                    "battleUrl": f"https://www.govauctions.com/battle/{aid}"
                }

            record = self.master_auctions[aid]
            record["lastChecked"] = now_str
            record["lastCheckedTime"] = now
            record["checkCount"] = record.get("checkCount", 0) + 1

            for bid_entry in history_entries:
                if not isinstance(bid_entry, list) or len(bid_entry) < 3:
                    continue

                # First value: Bid amount string e.g. "197.98"
                try:
                    amt = float(bid_entry[0])
                    if amt > record["highestBid"]:
                        record["highestBid"] = amt
                        record["highestBidFormatted"] = f"${amt:.2f}"
                        has_changed = True
                except Exception:
                    pass

                # Third value: Bidder username e.g. "Djpook"
                name = str(bid_entry[2]).strip()
                if name and name not in record["bidders"]:
                    record["bidders"].append(name)
                    has_changed = True

            if has_changed:
                record["lastUpdated"] = now_str
                record["lastBidChangeTime"] = now
                record["activityScore"] = record.get("activityScore", 0) + 1
                bidders_display = ", ".join(record["bidders"][:4]) + (f" (+{len(record['bidders'])-4} more)" if len(record["bidders"]) > 4 else "")
                print(f" [Bid Update] Auction #{aid} | Highest: {record['highestBidFormatted']} | Bidders ({len(record['bidders'])}): [{bidders_display}]")

    def extract_tab_bid_history(self, aid, page):
        """Reads raw JSON from tab and updates history."""
        try:
            raw_text = page.evaluate("""() => {
                const pre = document.querySelector('pre');
                if (pre) return pre.innerText;
                return document.body ? document.body.innerText : '';
            }""")
            if raw_text and raw_text.strip().startswith("{"):
                data = json.loads(raw_text)
                auctions_details = data.get("auctionsDetails", [])
                for item in auctions_details:
                    item_id = str(item.get("auctionId") or item.get("auction_id") or "").strip()
                    history = item.get("history", [])
                    if item_id:
                        self.process_gonzales_history(item_id, history)
        except Exception:
            pass

    def run_cycle_step(self):
        """
        Executes one continuous rotation cycle:
        1. Selects the next batch of up to 10 auctions according to priority.
        2. Reuses tabs in self.tab_pool (navigating to each auction's gonzales.php URL).
        3. Scrapes bid history, updates highest bid, appends unique bidders, updates timestamps.
        4. Retains all existing data in master records (never deletes).
        5. Computes preview of next batch so dashboard shows QUEUED status.
        6. Persists full master records to CSV & JSON.
        """
        self.sync_tab_pool()

        with self.lock:
            if not self.master_auctions:
                return

            target_batch = self.select_next_batch(count=self.max_active)
            self.active_batch = list(target_batch)
            self.preview_next_batch = [aid for aid in self.select_next_batch(count=self.max_active * 2) if aid not in self.active_batch][:self.max_active]
            self.update_statuses_locked()

            tabs_to_use = list(self.tab_pool[:len(self.active_batch)])

        for slot_idx, aid in enumerate(target_batch):
            if slot_idx >= len(tabs_to_use):
                break
            tab = tabs_to_use[slot_idx]
            gonzales_url = f"{GONZALES_BASE_URL}?idlist={aid}&auctionDetailsIds={aid}"
            try:
                if tab.is_closed():
                    continue
                tab.goto(gonzales_url, timeout=20000)
                self.extract_tab_bid_history(aid, tab)
            except Exception:
                pass

        with self.lock:
            self.cycle_count += 1
            self.last_cycle_time = time.strftime("%I:%M:%S %p").lstrip("0")
            self.update_statuses_locked()

        # Pure in-memory: no disk file writes

    def rotate_active_slot(self, aid=None):
        """Forces an immediate rotation to the next batch of auctions."""
        self.run_cycle_step()

    def get_dashboard_cards(self, status_filter=None, search=None, sort_by=None):
        """
        Returns all records from the master auction list.
        Supports filtering by status or search keyword, and custom sorting.
        """
        with self.lock:
            self.update_statuses_locked()
            cards = []
            for aid, rec in self.master_auctions.items():
                c = dict(rec)
                disc_idx = self.discovery_order.index(aid) if aid in self.discovery_order else 0
                c["discoveryIndex"] = disc_idx
                c["firstSeenOrder"] = rec.get("firstSeenOrder") or (disc_idx + 1)
                c["firstSeenTime"] = rec.get("firstSeenTime", 0.0)
                cards.append(c)

        if search:
            s = search.lower().strip()
            cards = [c for c in cards if s in c.get("name", "").lower() or s in c.get("auctionId", "").lower()]

        if status_filter and status_filter.lower() != "all":
            sf = status_filter.lower()
            if sf == "hot":
                cards = [c for c in cards if c.get("activityScore", 0) > 0 or (c.get("lastBidChangeTime", 0) > 0 and (time.time() - c["lastBidChangeTime"]) < 300)]
            else:
                cards = [c for c in cards if c.get("status") == sf]

        # Sorting
        if sort_by in ["first_seen_asc", "first_seen", "oldest"]:
            cards.sort(key=lambda c: (c.get("firstSeenOrder", 0), c.get("firstSeenTime", 0.0)))
        elif sort_by in ["first_seen_desc", "newest_first_seen"]:
            cards.sort(key=lambda c: (c.get("firstSeenOrder", 0), c.get("firstSeenTime", 0.0)), reverse=True)
        elif sort_by == "bid_high":
            cards.sort(key=lambda c: c.get("highestBid", 0.0), reverse=True)
        elif sort_by == "bidders":
            cards.sort(key=lambda c: len(c.get("bidders", [])), reverse=True)
        elif sort_by == "active":
            status_order = {"active": 0, "queued": 1, "recently_checked": 2, "waiting": 3}
            cards.sort(key=lambda c: (status_order.get(c.get("status"), 4), c.get("activeSlot") or 99))
        elif sort_by == "auction_id":
            cards.sort(key=lambda c: c.get("auctionId", ""))
        elif sort_by == "recent":
            def sort_key(c):
                checked = 1 if (c.get("lastCheckedTime", 0) > 0 or len(c.get("bidders", [])) > 0 or c.get("highestBid", 0) > 0) else 0
                event_time = max(c.get("lastCheckedTime", 0.0), c.get("lastBidChangeTime", 0.0))
                disc_idx = self.discovery_order.index(c.get("auctionId")) if c.get("auctionId") in self.discovery_order else 0
                return (
                    checked,
                    event_time,
                    c.get("highestBid", 0.0),
                    -disc_idx
                )
            cards.sort(key=sort_key, reverse=True)
        else:
            # Default sorting: First Seen: Oldest -> Newest (preserves original discovered position!)
            cards.sort(key=lambda c: (c.get("firstSeenOrder", 0), c.get("firstSeenTime", 0.0)))

        return cards

    def get_system_stats(self):
        """Returns telemetry stats for the dashboard header and console."""
        with self.lock:
            self.update_statuses_locked()
            total = len(self.master_auctions)
            active = len(self.active_batch)
            queued = len(self.preview_next_batch)
            recently_checked = sum(1 for c in self.master_auctions.values() if c.get("status") == "recently_checked")
            waiting = max(0, total - active - queued - recently_checked)

            return {
                "active_count": active,
                "total_discovered": total,
                "queued_count": queued,
                "recently_checked_count": recently_checked,
                "waiting_count": waiting,
                "last_static_refresh": self.last_static_refresh,
                "last_cycle_time": self.last_cycle_time,
                "cycle_count": self.cycle_count,
                "active_slots": self.active_batch,
                "next_slots": self.preview_next_batch
            }

    def save_csv_and_json(self):
        """No-op: In-memory mode active. Data is not saved to disk."""
        pass

    def get_summary_table(self):
        """Returns a formatted console table of active batch, queue, and stats."""
        with self.lock:
            total = len(self.master_auctions)
            active_ids = list(self.active_batch)
            next_ids = list(self.preview_next_batch)
            cards = [self.master_auctions[aid] for aid in active_ids if aid in self.master_auctions]

        lines = []
        lines.append("\n" + "=" * 120)
        lines.append(f" MASTER AUCTION MONITOR TELEMETRY (Cycle #{self.cycle_count})")
        lines.append(f" Total Discovered: {total} | Active Tabs: {len(active_ids)}/{self.max_active} | Queued Next: {len(next_ids)}")
        lines.append(f" Last StaticData Refresh: {self.last_static_refresh} | Last Cycle: {self.last_cycle_time}")
        lines.append("-" * 120)
        lines.append(f"{'Tab':<5} | {'Auction ID':<12} | {'Highest Bid':<12} | {'Bidders':<8} | {'Status':<16} | {'Product Name'}")
        lines.append("-" * 120)
        for idx, r in enumerate(cards, 1):
            aid = r.get("auctionId", "")
            h_bid = r.get("highestBidFormatted", "N/A")
            b_cnt = len(r.get("bidders", []))
            status = (r.get("status") or "ACTIVE").upper()
            name = (r.get("name") or f"Auction #{aid}")[:45]
            lines.append(f"Tab {idx:<2} | {aid:<12} | {h_bid:<12} | {b_cnt:<8} | {status:<16} | {name}")
        lines.append("=" * 120)
        if next_ids:
            lines.append(f" Next Rotation Batch: {next_ids}")
            lines.append("=" * 120)
        lines.append("\n")
        return "\n".join(lines)


class DashboardHandler(BaseHTTPRequestHandler):
    """
    HTTP handler serving dashboard frontend and real-time APIs.
    """
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ["/", "/index.html"]:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if os.path.exists(DASHBOARD_HTML_PATH):
                with open(DASHBOARD_HTML_PATH, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.wfile.write(b"<h1>Dashboard HTML not found.</h1>")

        elif path == "/api/auctions":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            qs = parse_qs(parsed.query)
            status_f = qs.get("status", [None])[0]
            search_f = qs.get("search", [None])[0]
            sort_f = qs.get("sort", [None])[0]
            cards = self.server.engine.get_dashboard_cards(status_filter=status_f, search=search_f, sort_by=sort_f)
            self.wfile.write(json.dumps(cards, ensure_ascii=False).encode("utf-8"))

        elif path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            stats = self.server.engine.get_system_stats()
            self.wfile.write(json.dumps(stats, ensure_ascii=False).encode("utf-8"))

        else:
            self.send_response(404)
            self.end_headers()


def start_dashboard_server(engine, host="127.0.0.1", port=DEFAULT_DASHBOARD_PORT):
    """
    Spawns background HTTP server for localhost dashboard.
    """
    for test_port in [port, port + 1, port + 50, 8080]:
        try:
            server = ThreadingHTTPServer((host, test_port), DashboardHandler)
            server.engine = engine
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            dashboard_url = f"http://localhost:{test_port}"
            print(f"\n[+] Localhost Web Dashboard active: {dashboard_url}")
            return server, dashboard_url
        except Exception:
            continue
    print("[!] Could not bind localhost dashboard server.")
    return None, None


class NetworkMonitor:
    """
    Listens to browser network traffic:
    - Intercepts all staticData responses (StaticData #1 and #2) and pushes to engine.
    - Intercepts gonzales.php responses and updates bid histories.
    - Checks all responses and WebSockets for keyword 'oneontopofyou'.
    """
    def __init__(self, engine, target_keyword=TARGET_KEYWORD):
        self.engine = engine
        self.target = target_keyword.lower()
        self.matches = []
        self.total_responses = 0
        self.lock = threading.Lock()

    def record_match(self, url, match_type, snippet=""):
        with self.lock:
            match_entry = {
                "keyword": self.target,
                "type": match_type,
                "url": url,
                "snippet": snippet,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            self.matches.append(match_entry)
            print(f"\n" + "!" * 75)
            print(f" [!] [MATCH DETECTED] Found '{self.target}' in network {match_type}!")
            print(f"     URL: {url}")
            if snippet:
                print(f"     Snippet: {snippet[:150]}")
            print("!" * 75 + "\n")

            # In-memory only: matches stored in self.matches

    def on_response(self, response):
        """Inspects HTTP responses."""
        with self.lock:
            self.total_responses += 1

        try:
            url = response.url

            # 1. Intercept staticData responses (both StaticData #1 & #2)
            if "staticdata" in url.lower():
                try:
                    body = response.text()
                    if body.strip().startswith(("{", "[")):
                        data = json.loads(body)
                        self.engine.process_static_data(data, raw_url=url)
                except Exception:
                    pass

            # 2. Intercept gonzales.php responses
            if "gonzales.php" in url.lower():
                try:
                    body = response.text()
                    if "auctionsDetails" in body:
                        data = json.loads(body)
                        auctions_details = data.get("auctionsDetails", [])
                        for item in auctions_details:
                            aid = str(item.get("auctionId") or item.get("auction_id") or "").strip()
                            history = item.get("history", [])
                            if aid:
                                self.engine.process_gonzales_history(aid, history)
                except Exception:
                    pass

            # 3. Check for target keyword in URL
            if self.target in url.lower():
                self.record_match(url, "URL", snippet=url)
                return

            # Skip binary media
            content_type = (response.headers.get("content-type") or "").lower()
            if any(m in content_type for m in ["image/", "font/", "video/", "audio/", "octet-stream"]):
                return

            # 4. Check response body for target keyword
            try:
                body = response.text()
                if self.target in body.lower():
                    idx = body.lower().find(self.target)
                    start = max(0, idx - 40)
                    end = min(len(body), idx + len(self.target) + 40)
                    snippet = body[start:end].replace("\n", " ").replace("\r", " ")
                    self.record_match(url, "Response Body", snippet=snippet)
            except Exception:
                pass

        except Exception:
            pass

    def on_websocket(self, ws):
        """Inspects live WebSocket frames."""
        def on_frame(payload):
            try:
                text = str(payload)
                if self.target in text.lower():
                    idx = text.lower().find(self.target)
                    start = max(0, idx - 30)
                    end = min(len(text), idx + len(self.target) + 30)
                    snippet = text[start:end].replace("\n", " ")
                    self.record_match(ws.url, "WebSocket Frame", snippet=snippet)
            except Exception:
                pass

        try:
            ws.on("framereceived", on_frame)
            ws.on("framesent", on_frame)
        except Exception:
            pass

    def print_status(self):
        """Prints whether 'oneontopofyou' was found (True) or not (False)."""
        print("\n" + "=" * 75)
        print(f"       KEYWORD SEARCH RESULT FOR: '{self.target}'")
        print("=" * 75)
        with self.lock:
            if len(self.matches) > 0:
                print(f" >>> RESULT: True <<<")
                print(f" Total matches found: {len(self.matches)}")
                print("-" * 75)
                for idx, m in enumerate(self.matches, 1):
                    print(f" Match #{idx} [{m['type']}]: {m['url']}")
                    if m.get("snippet"):
                        print(f"   Context: ... {m['snippet']} ...")
                print("-" * 75)
                print(f" Matches held in-memory (disk saving disabled)")
            else:
                print(f" >>> RESULT: False <<<")
                print(f" (No network responses or WebSocket messages contained '{self.target}')")
                print(f" Total network responses inspected: {self.total_responses}")
        print("=" * 75 + "\n")


def check_is_logged_in(page):
    """Verifies active session in DOM."""
    try:
        result = page.evaluate("""() => {
            if (window.dd && window.dd.user && window.dd.user.custom) {
                const segment = window.dd.user.custom.segment;
                if (segment && segment !== "Guest") {
                    return { logged_in: true, reason: "Active user segment: " + segment };
                }
            }
            const logout = document.querySelector('a[href*="logout"], button[class*="logout"], [data-testid="logout"]');
            if (logout) return { logged_in: true, reason: "Found logout link" };

            const account = document.querySelector('a[href*="/account"], a[href*="/profile"], [class*="avatar"], [class*="user-menu"]');
            if (account) return { logged_in: true, reason: "Found account/profile element" };

            const bids = document.querySelector('[class*="bids"], [class*="balance"], [data-testid="bids-count"]');
            if (bids) return { logged_in: true, reason: "Found bid/balance counter" };

            const loginLink = document.querySelector('a[href*="/login"]');
            const path = window.location.pathname.toLowerCase();
            if (!loginLink && (path === "/" || path.includes("home") || path.includes("auction"))) {
                const getStarted = document.querySelector('a[href*="/learn"], a[href*="/register"]');
                if (!getStarted) return { logged_in: true, reason: "No login buttons found on home" };
            }
            return { logged_in: false };
        }""")
        return result.get("logged_in", False), result.get("reason", "Not logged in")
    except Exception:
        return False, "Error checking DOM"


def prompt_for_credentials(cli_username=None, cli_password=None):
    """Prompts for credentials if needed."""
    print("\n" + "-" * 75)
    print(" [i] Authentication required. Please enter your credentials:")
    print("-" * 75)
    username = cli_username or input("Enter your Username or Email: ").strip()
    password = cli_password or getpass.getpass("Enter your Password: ").strip()
    return username, password


def run_login(cli_username=None, cli_password=None, headless=False, cycle_interval=3.0, dashboard_port=DEFAULT_DASHBOARD_PORT):
    """
    Main execution loop:
    1. Launches persistent Chrome browser.
    2. Starts localhost web dashboard on port 5000.
    3. Navigates to GovAuctions home page.
    4. Intercepts both staticData responses into the queue.
    5. Runs continuous processing cycle: Active 1..10 -> refresh bid history -> update dashboard -> repeat.
    """
    os.makedirs(CHROME_PROFILE_DIR, exist_ok=True)
    engine = AuctionQueueEngine(max_active=MAX_ACTIVE_AUCTIONS)
    monitor = NetworkMonitor(engine=engine, target_keyword=TARGET_KEYWORD)

    # Start localhost dashboard
    server, dashboard_url = start_dashboard_server(engine, port=dashboard_port)

    print("=" * 75)
    print("      GOVAUCTIONS CONTINUOUS QUEUE MONITOR & DASHBOARD")
    if dashboard_url:
        print(f"      Localhost Dashboard: {dashboard_url}")
    print("=" * 75)

    with sync_playwright() as p:
        print("\n[1/4] Launching Chrome with persistent profile...")
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=CHROME_PROFILE_DIR,
                channel="chrome",
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check"
                ],
                viewport={"width": 1366, "height": 850}
            )
        except Exception as e:
            print(f"Notice: Falling back to Chromium ({e})...")
            context = p.chromium.launch_persistent_context(
                user_data_dir=CHROME_PROFILE_DIR,
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check"
                ],
                viewport={"width": 1366, "height": 850}
            )

        engine.set_context(context)

        # Attach network listener
        context.on("response", monitor.on_response)

        def setup_page_listeners(page_obj):
            page_obj.on("websocket", monitor.on_websocket)

        context.on("page", setup_page_listeners)

        home_page = context.pages[0] if context.pages else context.new_page()
        setup_page_listeners(home_page)

        # Open dashboard tab in Chrome
        if dashboard_url:
            try:
                dash_page = context.new_page()
                dash_page.goto(dashboard_url)
                print(f"[+] Opened dashboard in browser tab: {dashboard_url}")
            except Exception:
                pass

        print(f"[2/4] Navigating to {GOVAUCTIONS_HOME_URL}...")
        try:
            home_page.goto(GOVAUCTIONS_HOME_URL, timeout=45000)
            home_page.wait_for_load_state("domcontentloaded")
            home_page.wait_for_timeout(2000)
            print(f"      Loaded: '{home_page.title()}' ({home_page.url})")
        except Exception as err:
            print(f"      Navigation notice: {err}")

        # Check login status
        is_logged_in, reason = check_is_logged_in(home_page)

        if is_logged_in:
            print("\n" + "=" * 75)
            print(f" >>> [+] ALREADY LOGGED IN! ({reason}) <<<")
            print("     Saved session detected. Reading staticData responses...")
            print("=" * 75)
        else:
            print(f"\n[3/4] No active session found ({reason}). Proceeding to login...")
            username, password = prompt_for_credentials(cli_username, cli_password)

            login_link = home_page.query_selector('a[href*="/login"]')
            login_page = None

            if login_link:
                try:
                    with context.expect_page(timeout=10000) as new_page_info:
                        login_link.click()
                    login_page = new_page_info.value
                    setup_page_listeners(login_page)
                    login_page.wait_for_load_state("domcontentloaded")
                except Exception:
                    home_page.goto(GOVAUCTIONS_LOGIN_URL, timeout=45000)
                    login_page = home_page
            else:
                home_page.goto(GOVAUCTIONS_LOGIN_URL, timeout=45000)
                login_page = home_page

            login_page.wait_for_timeout(2000)
            is_redirected_logged_in, redir_reason = check_is_logged_in(login_page)

            if not is_redirected_logged_in:
                print("      Entering credentials...")
                try:
                    username_input = login_page.wait_for_selector(
                        'input[type="email"], input[placeholder*="Username"], input[placeholder*="Email"]',
                        timeout=10000
                    )
                    password_input = login_page.wait_for_selector('input[type="password"]', timeout=10000)
                    username_input.fill(username)
                    password_input.fill(password)
                    submit_btn = login_page.query_selector('button:has-text("Log In"), button[type="submit"], input[type="submit"]')
                    if submit_btn:
                        submit_btn.click()
                        print("      Submitted login form.")
                except PlaywrightTimeoutError:
                    print("      Notice: Login inputs not found.")

        # Start console thread
        cmd_queue = queue.Queue()
        stop_event = threading.Event()
        input_thread = threading.Thread(target=console_input_worker, args=(cmd_queue, stop_event), daemon=True)
        input_thread.start()

        # Allow home page staticData to arrive
        print("\n[4/4] Capturing initial staticData #1 & #2 from home page...")
        for _ in range(8):
            if context.pages:
                context.pages[0].wait_for_timeout(500)

        # Print command menu
        print("\n" + "=" * 75)
        print(" CONTINUOUS AUCTION MONITOR CONSOLE:")
        print(f" - Localhost Dashboard   : {dashboard_url}")
        print(" - 'queue'               : Show active 1-10 slots and waiting queue")
        print(" - 'next' / 'rotate'     : Retire an active auction so next queued one steps in")
        print(" - 'poll' / 'cycle'      : Force an immediate cycle through all active auctions")
        print(" - 'home'                : Reload GovAuctions home to refresh staticData")
        print(" - 'dash'                : Open/print dashboard link")
        print(" - 'check'               : Check for 'oneontopofyou' keyword matches")
        print(" - 'save'                : Force save CSV and JSON records")
        print(" - 'q' / 'exit'          : Close all tabs and exit cleanly")
        print("=" * 75)
        print("Command: ", end="", flush=True)

        last_cycle_time = time.time()

        while not stop_event.is_set():
            if not context.pages:
                break

            # Process console commands
            try:
                cmd = cmd_queue.get_nowait()
                cmd_clean = cmd.strip()

                if cmd_clean.lower() in ["q", "quit", "exit"]:
                    print("\nExiting session...")
                    break

                elif cmd_clean.lower() in ["queue", "q-list", "status"]:
                    print(engine.get_summary_table())
                    print("Command: ", end="", flush=True)

                elif cmd_clean.lower() in ["next", "rotate"]:
                    engine.rotate_active_slot()
                    print(engine.get_summary_table())
                    print("Command: ", end="", flush=True)

                elif cmd_clean.lower().startswith("retire ") or cmd_clean.lower().startswith("close "):
                    parts = cmd_clean.split(maxsplit=1)
                    if len(parts) > 1:
                        engine.rotate_active_slot(parts[1].strip())
                    print("Command: ", end="", flush=True)

                elif cmd_clean.lower() in ["poll", "cycle"]:
                    print("\nRunning immediate cycle...")
                    engine.run_cycle_step()
                    print(engine.get_summary_table())
                    print("Command: ", end="", flush=True)

                elif cmd_clean.lower() in ["dash", "dashboard", "d"]:
                    print(f"\nLocalhost Dashboard: {dashboard_url}")
                    if dashboard_url and context.pages:
                        nt = context.new_page()
                        nt.goto(dashboard_url)
                    print("Command: ", end="", flush=True)

                elif cmd_clean.lower() in ["check", "c"] or cmd_clean == "":
                    monitor.print_status()
                    print("Command: ", end="", flush=True)

                elif cmd_clean.lower() == "home":
                    print(f"\nRefreshing {GOVAUCTIONS_HOME_URL}...")
                    home_page.goto(GOVAUCTIONS_HOME_URL)
                    print("Command: ", end="", flush=True)

                elif cmd_clean.lower() == "save":
                    print(f"\n[!] Disk saving is disabled. All current data is held purely in-memory.")
                    print("Command: ", end="", flush=True)

                elif cmd_clean.lower() in ["help", "h", "?"]:
                    print("\nAvailable Commands:")
                    print("  queue            - View 10 active slots & waiting queue")
                    print("  next / rotate    - Retire an active auction so next queued one steps in")
                    print("  poll / cycle     - Trigger an immediate cycle")
                    print("  dash             - Open localhost dashboard")
                    print("  home             - Reload GovAuctions home to refresh staticData")
                    print("  check            - Check keyword 'oneontopofyou' matches")
                    print("  q / exit         - Exit browser")
                    print("Command: ", end="", flush=True)

                else:
                    print(f"Unknown command: '{cmd_clean}'. Type 'help' for command list.")
                    print("Command: ", end="", flush=True)

            except queue.Empty:
                pass

            # Continuous cycle execution: repeatedly refreshes active 1..10 auctions
            now = time.time()
            if now - last_cycle_time >= cycle_interval:
                last_cycle_time = now
                if engine.master_auctions:
                    engine.run_cycle_step()

            try:
                if context.pages:
                    context.pages[0].wait_for_timeout(250)
            except Exception:
                break

        stop_event.set()
        try:
            context.close()
        except Exception:
            pass
        print("Browser closed cleanly.")
        return True


def main():
    parser = argparse.ArgumentParser(description="GovAuctions Continuous Queue Monitor & Dashboard")
    parser.add_argument("-u", "--username", help="Username or Email address")
    parser.add_argument("-p", "--password", help="Password")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--cycle-interval", type=float, default=3.0, help="Seconds between cycle executions (default: 3.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_DASHBOARD_PORT, help="Port for localhost dashboard (default: 5000)")
    args = parser.parse_args()

    run_login(
        cli_username=args.username,
        cli_password=args.password,
        headless=args.headless,
        cycle_interval=args.cycle_interval,
        dashboard_port=args.port
    )


if __name__ == "__main__":
    main()
