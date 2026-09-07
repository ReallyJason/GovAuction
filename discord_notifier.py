import os
import json
import time
import threading
import queue
import urllib.request
import urllib.error

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "discord_config.json")
EXAMPLE_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "discord_config.example.json")

class DiscordNotifier:
    """
    Discord Bot Notifier for GovAuctions:
    - Monitors bookmarked auctions.
    - Sends an alert when a bookmarked auction has exactly 2 people bidding on it.
    - Repeatedly pings 5 times with:
        @me @me @me @me @me
        ONLY 2 PEOPLE BIDDING ON: [ITEM NAME]
    - Prevents duplicate spam while an auction remains at 2 bidders.
    - Resets tracking if bidder count changes, allowing a new alert if it returns to 2 bidders.
    - Runs delivery asynchronously in a background thread so auction polling is never delayed.
    - Keeps bot tokens secure and separate from source code (never logged).
    """

    def __init__(self, config_path=CONFIG_PATH, auto_start_worker=True):
        self.config_path = config_path
        self.alerted_auction_ids = set()
        self.lock = threading.Lock()
        self.queue = queue.Queue()
        self.worker_thread = None
        self.total_alerts_sent = 0
        self.last_alert_time = None
        self.last_alert_auction = None

        self.load_config()

        if auto_start_worker:
            self.start_worker()

    def load_config(self):
        """Loads configuration from discord_config.json or environment variables."""
        self.enabled = False
        self.bot_token = ""
        self.channel_id = ""
        self.webhook_url = ""
        self.ping_target = "@me"
        self.repeat_pings = 5

        # 1. Read from json file if present
        if os.path.isfile(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.enabled = bool(data.get("enabled", True))
                    self.bot_token = str(data.get("bot_token", "")).strip()
                    self.channel_id = str(data.get("channel_id", "")).strip()
                    self.webhook_url = str(data.get("webhook_url", "")).strip()
                    self.ping_target = str(data.get("ping_target", "@me")).strip()
                    self.repeat_pings = int(data.get("repeat_pings", 5))
            except Exception as e:
                print(f"[DiscordNotifier] Warning: Failed to parse {self.config_path}: {e}")

        # 2. Check environment variable overrides
        env_token = os.environ.get("DISCORD_BOT_TOKEN")
        if env_token:
            self.bot_token = env_token.strip()
            self.enabled = True

        env_channel = os.environ.get("DISCORD_CHANNEL_ID")
        if env_channel:
            self.channel_id = env_channel.strip()

        env_webhook = os.environ.get("DISCORD_WEBHOOK_URL")
        if env_webhook:
            self.webhook_url = env_webhook.strip()
            self.enabled = True

        env_ping = os.environ.get("DISCORD_PING_TARGET")
        if env_ping:
            self.ping_target = env_ping.strip()

    def is_configured(self):
        """Returns True if bot token + channel_id or webhook_url is configured."""
        has_bot = bool(self.bot_token and self.channel_id and not self.bot_token.startswith("YOUR_DISCORD"))
        has_webhook = bool(self.webhook_url and self.webhook_url.startswith("http"))
        return self.enabled and (has_bot or has_webhook)

    def get_status_info(self):
        """Returns non-sensitive status info for console/dashboard."""
        configured = self.is_configured()
        method = "Unconfigured"
        if configured:
            if self.bot_token and self.channel_id:
                method = f"Discord Bot (Channel: {self.channel_id})"
            elif self.webhook_url:
                method = "Discord Webhook"

        return {
            "configured": configured,
            "enabled": self.enabled,
            "method": method,
            "ping_target": self.ping_target,
            "repeat_pings": self.repeat_pings,
            "tracked_alert_ids": list(self.alerted_auction_ids),
            "total_alerts_sent": self.total_alerts_sent,
            "last_alert_time": self.last_alert_time,
            "last_alert_auction": self.last_alert_auction
        }

    @property
    def alerts_sent(self):
        return self.total_alerts_sent

    @property
    def delivery_method(self):
        return self.get_status_info()["method"]

    def start_worker(self):
        """Starts background worker thread for asynchronous message dispatch."""
        if self.worker_thread and self.worker_thread.is_alive():
            return
        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="DiscordWorker")
        self.worker_thread.start()

    def _worker_loop(self):
        while True:
            try:
                task = self.queue.get()
                if task is None:
                    break
                message_text = task
                self._dispatch_http_send(message_text)
                self.queue.task_done()
            except Exception:
                pass

    def _dispatch_http_send(self, message_text):
        """Sends HTTP request to Discord API or Webhook without exposing tokens in logs."""
        try:
            payload = json.dumps({"content": message_text}).encode("utf-8")

            # 1. Discord Bot Token via REST API
            if self.bot_token and self.channel_id and not self.bot_token.startswith("YOUR_DISCORD"):
                url = f"https://discord.com/api/v10/channels/{self.channel_id}/messages"
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={
                        "Authorization": f"Bot {self.bot_token}",
                        "Content-Type": "application/json",
                        "User-Agent": "GovAuctionsDiscordAlert/1.0"
                    },
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status in (200, 201):
                        self.total_alerts_sent += 1
                        self.last_alert_time = time.strftime("%I:%M:%S %p").lstrip("0")
                        return True

            # 2. Discord Webhook URL fallback
            elif self.webhook_url and self.webhook_url.startswith("http"):
                req = urllib.request.Request(
                    self.webhook_url,
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "GovAuctionsDiscordAlert/1.0"
                    },
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status in (200, 204):
                        self.total_alerts_sent += 1
                        self.last_alert_time = time.strftime("%I:%M:%S %p").lstrip("0")
                        return True

        except urllib.error.HTTPError as e:
            # Mask credentials in error output
            print(f" [!] [Discord Alert Error] HTTP {e.code}: {e.reason}")
        except Exception as e:
            print(f" [!] [Discord Alert Error] Network error: {e}")
        return False

    def build_pings_prefix(self):
        """Constructs repeated pings prefix (e.g. '@me @me @me @me @me')."""
        target = self.ping_target or "@me"
        # If user provided a pure numeric ID (e.g. 123456789), format as Discord mention <@123456789>
        if target.isdigit():
            mention = f"<@{target}>"
        else:
            mention = target
        
        count = max(1, self.repeat_pings)
        return " ".join([mention] * count)

    def build_alert_message(self, auction_data):
        """
        Builds the Discord alert message.
        Matches required structure:
            @me @me @me @me @me
            ONLY 2 PEOPLE BIDDING ON: [ITEM NAME]
        """
        aid = str(auction_data.get("auctionId") or auction_data.get("id") or auction_data.get("i") or "").strip()
        name = str(auction_data.get("name") or f"Auction #{aid}").strip()
        highest_bid = auction_data.get("highestBidFormatted") or (f"${auction_data['highestBid']:,.2f}" if auction_data.get("highestBid") else "N/A")
        battle_url = auction_data.get("battleUrl") or f"https://www.govauctions.com/battle/{aid}"

        bidders = auction_data.get("bidders", [])
        bidders_str = ", ".join(bidders) if bidders else "2 Bidders"

        pings = self.build_pings_prefix()
        lines = [
            pings,
            f"ONLY 2 PEOPLE BIDDING ON: {name}",
            f"• **Auction ID**: `{aid}`",
            f"• **Current Bid**: `{highest_bid}` ({bidders_str})",
            f"• **Battle Link**: <{battle_url}>"
        ]
        return "\n".join(lines)

    def get_bidder_count(self, auction_data):
        """
        Calculates the number of bidders on an auction.
        Uses dynamic.auctions[].x / bidderCount from static network data,
        or unique bidders list from gonzales.php.
        """
        if not isinstance(auction_data, dict):
            return 0

        b_list = auction_data.get("bidders", [])
        num_named_bidders = len(b_list) if isinstance(b_list, list) else 0

        raw_count = auction_data.get("bidderCount")
        if raw_count is None:
            raw_count = auction_data.get("x")

        if raw_count is not None:
            try:
                c = int(raw_count)
                return max(c, num_named_bidders)
            except Exception:
                pass

        return num_named_bidders

    def is_qualifying_auction(self, auction_data):
        """
        Determines if an auction qualifies for the Discord alert:
        1. Auction is bookmarked ("bookmarked": True).
        2. Exactly 2 people bidding on that auction.
        """
        if not isinstance(auction_data, dict):
            return False

        # 1. Bookmark requirement: MUST be bookmarked: True
        is_bookmarked = (auction_data.get("bookmarked") is True)
        if not is_bookmarked:
            return False

        # 2. Exactly 2 people bidding
        count = self.get_bidder_count(auction_data)
        return (count == 2)

    def check_auction(self, auction_data):
        """
        Evaluates an auction and sends a Discord alert if conditions are met:
        - Only monitors bookmarked auctions.
        - Triggers when exactly 2 people are bidding.
        - Prevents duplicate spam while it remains at 2 bidders.
        - Resets tracking if bidder count changes to != 2, allowing a new alert if it returns to 2 bidders later.
        """
        if not isinstance(auction_data, dict):
            return False

        aid = str(auction_data.get("auctionId") or auction_data.get("id") or auction_data.get("i") or "").strip()
        if not aid:
            return False

        is_bookmarked = (auction_data.get("bookmarked") is True)
        # Never send alerts for non-bookmarked auctions
        if not is_bookmarked:
            return False

        bidder_count = self.get_bidder_count(auction_data)

        with self.lock:
            if bidder_count == 2:
                if aid not in self.alerted_auction_ids:
                    # Condition met and not yet alerted: trigger alert!
                    self.alerted_auction_ids.add(aid)
                    self.last_alert_auction = aid

                    name = str(auction_data.get("name") or f"Auction #{aid}").strip()
                    print(f"\n" + "!" * 75)
                    print(f" [DISCORD ALERT TRIGGERED] ONLY 2 PEOPLE BIDDING ON: {name} (Auction #{aid})")
                    print(f"                           Bookmarked: True | Bidder Count: 2")
                    print("!" * 75 + "\n")

                    msg = self.build_alert_message(auction_data)
                    self.queue.put(msg)
                    return True
            else:
                # Bidder count changed away from 2 (e.g. became 1, 3, 4, etc.)
                if aid in self.alerted_auction_ids:
                    # Reset alert state so it can alert again if it returns to 2 bidders later
                    self.alerted_auction_ids.remove(aid)

        return False

    def send_test_message(self):
        """Sends a test message to Discord to verify configuration."""
        if not self.is_configured():
            return False, "Discord is not configured. Please fill in discord_config.json with your bot_token & channel_id, or webhook_url."
        
        pings = self.build_pings_prefix()
        test_msg = (
            f"{pings}\n"
            f"[GovAuctions Alert Test]\n"
            f"Discord Bot Alert is successfully connected!\n"
            f"You will receive 5-ping alerts whenever a bookmarked auction has exactly 2 bidders."
        )
        self.queue.put(test_msg)
        return True, "Test alert queued for Discord."
