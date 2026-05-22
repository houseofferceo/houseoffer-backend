"""
HouseOffer — Reddit Community Monitor
======================================

Runs as a scheduled job on Render (every 4 hours via cron).
Searches target subreddits for housing-related conversations,
drafts helpful comments using Claude, and queues them in
Google Sheets for CEO review.

Once a day, sends a digest email summarising new drafts.

Env vars required (set on Render):
  ANTHROPIC_API_KEY               — Claude API key
  REDDIT_SHEETS_WEBHOOK_URL       — Apps Script webhook on the Reddit sheet
  REDDIT_SHEETS_WEBHOOK_SECRET    — matches the Apps Script SECRET on that sheet
  RESEND_API_KEY                  — for the digest email
  EMAIL_ADDRESS                   — ceo@houseoffer.uk
  REDDIT_USER_AGENT               — Reddit asks for a user-agent string
  SEND_DIGEST                     — "true" once a day (set by separate cron)
"""

import os
import sys
import json
import time
import hashlib
import requests
from datetime import datetime, timedelta, timezone

# ── CONFIG ────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SHEETS_WEBHOOK_URL = os.environ.get("REDDIT_SHEETS_WEBHOOK_URL", "")
SHEETS_WEBHOOK_SECRET = os.environ.get("REDDIT_SHEETS_WEBHOOK_SECRET", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS", "ceo@houseoffer.uk")
USER_AGENT = os.environ.get("REDDIT_USER_AGENT", "HouseOfferMonitor/1.0 by /u/houseoffer")
SEND_DIGEST = os.environ.get("SEND_DIGEST", "false").lower() == "true"

# Subreddits to monitor — keep lean, expand later if signal is strong
SUBREDDITS = [
    "HousingUK",
    "UKPersonalFinance",
    "HomeOwnersUK",
    "UKHousing",
    "MortgageUK",
]

# Keywords/phrases that suggest a post is a fit for HouseOffer.
# Cast wide here — is_relevant() filters first BEFORE the expensive Claude call,
# and Claude itself can reply "SKIP" if a triggered post isn't actually a fit.
TRIGGER_PHRASES = [
    # — Considering an offer —
    "should i offer",
    "what to offer",
    "how much to offer",
    "how much should i offer",
    "what's a fair offer",
    "fair offer",
    "first offer",
    "opening offer",
    "initial offer",
    "make an offer",
    "making an offer",
    "putting in an offer",
    "putting an offer",

    # — Below / under asking —
    "below asking",
    "under asking",
    "under the asking",
    "off asking",
    "off the asking",
    "low offer",
    "cheeky offer",
    "rude offer",
    "lowball",
    "low ball",

    # — Pricing concerns —
    "overpriced",
    "over priced",
    "over-priced",
    "asking too much",
    "priced too high",
    "way too expensive",
    "feels expensive",
    "seems expensive",
    "seems overpriced",
    "asking price too high",
    "asking price seems",
    "is the asking price",
    "fair price",
    "fair value",
    "is this fair",
    "is it worth",
    "is this worth",
    "worth what they",

    # — Valuation help —
    "house valuation",
    "property valuation",
    "what's it worth",
    "how to value",
    "land registry",
    "sold prices",
    "sold price",
    "comparable sales",
    "what did it last sell for",
    "what did it previously sell",
    "previous sale price",
    "house price",
    "property worth",
    "house worth",

    # — Negotiation —
    "negotiate",
    "negotiating",
    "haggle",
    "haggling",
    "bargaining",
    "counter offer",
    "counter-offer",
    "counteroffer",
    "rejected my offer",
    "rejected our offer",
    "estate agent said",
    "estate agent told",
    "the agent said",
    "agent rejected",

    # — First-time buyer panic —
    "first time buyer",
    "first-time buyer",
    " ftb ",
    "first home",
    "buying my first",
    "found my dream",
    "viewing tomorrow",
    "second viewing",
    "third viewing",

    # — Specific situations —
    "overpaying",
    "over paying",
    "paying over",
    "paying too much",
    "above asking",
    "above the asking",
    "below market",
    "above market",

    # — Reddit-natural phrasings —
    "thoughts on price",
    "thoughts on this offer",
    "thoughts on the price",
    "advice on offer",
    "advice on price",
    "talk me down",
    "talk me out",
    "am i mad",
    "am i crazy",
    "is this nuts",
    "is this normal",
    "how much under asking",
]

# Local cache file to avoid duplicate drafts on the same post
SEEN_POSTS_PATH = "/tmp/houseoffer_seen_posts.json"
DAILY_DIGEST_BUFFER_PATH = "/tmp/houseoffer_digest_buffer.json"

# Look back this many hours when fetching new posts
# Cron runs every 4hr; 12hr lookback gives some buffer for missed runs
LOOKBACK_HOURS = 12

# How many drafts to generate per run (hard cap to control cost)
MAX_DRAFTS_PER_RUN = 10


# ── REDDIT FETCH (no API key needed — uses public JSON) ───────────────────────

def fetch_subreddit_new(subreddit, limit=25):
    """Fetch newest posts from a subreddit. Reddit's public JSON endpoint."""
    url = f"https://www.reddit.com/r/{subreddit}/new.json?limit={limit}"
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
        if r.status_code != 200:
            print(f"Reddit {subreddit}: HTTP {r.status_code}")
            return []
        data = r.json().get("data", {}).get("children", [])
        return [item.get("data", {}) for item in data]
    except Exception as e:
        print(f"Reddit fetch error ({subreddit}): {e}")
        return []


def is_relevant(post):
    """Decide if a post is worth drafting a reply for."""
    title = (post.get("title") or "").lower()
    body = (post.get("selftext") or "").lower()
    combined = title + " " + body

    # Skip removed/deleted posts
    if post.get("removed_by_category") or body == "[removed]" or body == "[deleted]":
        return False
    # Skip very old posts (cron runs every 4hr, look back 6hr)
    age_seconds = time.time() - post.get("created_utc", 0)
    if age_seconds > LOOKBACK_HOURS * 3600:
        return False
    # Skip very short posts (title-only is too thin for context) or very long ones
    if len(body) < 40 or len(body) > 5000:
        return False
    # Skip posts asking general non-buyer questions
    if any(skip in combined for skip in ["selling my", "as a seller", "estate agent here", "i am a landlord", "rental"]):
        return False
    # Must hit at least one trigger phrase
    return any(phrase in combined for phrase in TRIGGER_PHRASES)


def post_fingerprint(post):
    """Stable ID we can store in our seen-posts cache."""
    return post.get("id") or hashlib.md5(post.get("permalink", "").encode()).hexdigest()[:12]


# ── SEEN CACHE ────────────────────────────────────────────────────────────────

def load_seen():
    try:
        with open(SEEN_POSTS_PATH) as f:
            data = json.load(f)
        # Drop entries older than 7 days
        cutoff = time.time() - 7 * 24 * 3600
        return {k: v for k, v in data.items() if v > cutoff}
    except Exception:
        return {}


def save_seen(seen):
    try:
        with open(SEEN_POSTS_PATH, "w") as f:
            json.dump(seen, f)
    except Exception as e:
        print(f"save_seen error: {e}")


# ── CLAUDE DRAFT GENERATION ───────────────────────────────────────────────────

DRAFT_SYSTEM_PROMPT = """You are a friendly UK home-buyer who has used HouseOffer (a free tool that compares asking prices against Land Registry sold prices) and now occasionally helps others when you see relevant questions on Reddit.

Your job is to draft a Reddit reply for the post below. The reply must:

1. Be GENUINELY HELPFUL FIRST. Answer the actual question or give real, useful advice based on the data in the post. Acknowledge their specific situation.

2. Mention HouseOffer NATURALLY only if it fits — e.g. "I found this free tool called HouseOffer useful when I was looking at offers" or "If it helps, HouseOffer pulls Land Registry sold prices and gives a verdict in 30 seconds — free". Don't shoehorn it in.

3. Sound like a real person on Reddit, not a marketing department. UK English. Use lowercase casual tone where it fits. No "I hope this helps!" or "Best of luck!". No corporate language.

4. Be SHORT. 3-6 sentences max. Reddit hates long replies.

5. Never claim affiliation with HouseOffer. Don't say "I built" or "we built". Frame as a satisfied user.

6. If the post is clearly not a fit for HouseOffer (e.g. about renting, selling, mortgages, conveyancing), output: SKIP — followed by a one-line reason. Don't draft anything.

Output format: just the comment text, nothing else. No quotation marks, no preamble, no signoff."""


def draft_reply(post):
    """Call Claude API to draft a helpful Reddit reply."""
    if not ANTHROPIC_API_KEY:
        return None, "missing ANTHROPIC_API_KEY"

    user_message = f"""Subreddit: r/{post.get('subreddit', '')}
Title: {post.get('title', '')}
Post body:
{post.get('selftext', '')[:2000]}

Draft a reply now."""

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5",
                "max_tokens": 400,
                "system": DRAFT_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_message}],
            },
            timeout=30,
        )
        if r.status_code != 200:
            return None, f"Claude API HTTP {r.status_code}: {r.text[:200]}"
        data = r.json()
        text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text").strip()
        if not text or text.startswith("SKIP"):
            return None, text or "empty response"
        return text, None
    except Exception as e:
        return None, f"Claude API error: {e}"


# ── GOOGLE SHEETS WRITE ───────────────────────────────────────────────────────

def post_draft_to_sheets(post, draft_text):
    """Append a row to the Reddit Drafts tab via the existing Apps Script webhook."""
    if not SHEETS_WEBHOOK_URL or not SHEETS_WEBHOOK_SECRET:
        print("Sheets webhook not configured")
        return False

    permalink = "https://reddit.com" + post.get("permalink", "")
    payload = {
        "secret": SHEETS_WEBHOOK_SECRET,
        "type": "reddit_draft",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "post_id": post.get("id"),
        "subreddit": post.get("subreddit"),
        "title": post.get("title", "")[:200],
        "post_url": permalink,
        "post_body_excerpt": (post.get("selftext") or "")[:500],
        "draft_reply": draft_text,
        "status": "Pending review",
    }
    try:
        r = requests.post(SHEETS_WEBHOOK_URL, json=payload, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"Sheets webhook error: {e}")
        return False


def add_to_digest_buffer(entry):
    """Buffer drafts for the daily digest email."""
    try:
        buf = []
        if os.path.exists(DAILY_DIGEST_BUFFER_PATH):
            with open(DAILY_DIGEST_BUFFER_PATH) as f:
                buf = json.load(f)
        buf.append(entry)
        with open(DAILY_DIGEST_BUFFER_PATH, "w") as f:
            json.dump(buf, f)
    except Exception as e:
        print(f"digest buffer error: {e}")


# ── DIGEST EMAIL ──────────────────────────────────────────────────────────────

SHEET_URL = "https://docs.google.com/spreadsheets/d/1K6rG4LOY43TEf9YRnQKI9LOr9Zi0tWu-BCye57r2M5o/edit"


def send_digest_email():
    """Once-daily email summarising the previous 24 hours of drafts."""
    if not os.path.exists(DAILY_DIGEST_BUFFER_PATH):
        print("No drafts in buffer — skipping digest")
        return

    try:
        with open(DAILY_DIGEST_BUFFER_PATH) as f:
            entries = json.load(f)
    except Exception as e:
        print(f"digest read error: {e}")
        return

    if not entries:
        print("Empty buffer — skipping digest")
        return

    # Build a simple, email-safe HTML digest
    rows_html = ""
    for e in entries[:20]:  # cap at 20 for email length
        rows_html += f"""
        <tr>
          <td style="padding:14px 12px;border-bottom:1px solid #e0d9ce;vertical-align:top;">
            <p style="margin:0 0 6px;font-family:Helvetica,Arial,sans-serif;font-size:11px;font-weight:600;color:#1a6b5a;letter-spacing:0.06em;text-transform:uppercase;">
              r/{e.get('subreddit', '')}
            </p>
            <p style="margin:0 0 8px;font-family:Georgia,serif;font-size:15px;font-weight:700;color:#1e1c18;line-height:1.3;">
              <a href="{e.get('post_url', '#')}" style="color:#1e1c18;text-decoration:none;">{e.get('title', '')}</a>
            </p>
            <p style="margin:0 0 10px;font-family:Helvetica,Arial,sans-serif;font-size:13px;color:#5c5849;line-height:1.5;background:#f7f3ed;padding:10px 12px;border-radius:6px;border-left:3px solid #b8dfd5;">
              {e.get('draft_reply', '')[:280]}{('…' if len(e.get('draft_reply', '')) > 280 else '')}
            </p>
            <p style="margin:0;font-family:Helvetica,Arial,sans-serif;font-size:12px;">
              <a href="{e.get('post_url', '#')}" style="color:#1a6b5a;text-decoration:underline;">View post →</a>
            </p>
          </td>
        </tr>
        """

    skipped_count = sum(1 for e in entries if e.get("status") == "skipped")
    drafted_count = sum(1 for e in entries if e.get("status") == "drafted")

    html = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f7f3ed;font-family:Helvetica,Arial,sans-serif;">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#f7f3ed;">
  <tr><td align="center" style="padding:24px 12px;">
    <table cellpadding="0" cellspacing="0" border="0" width="600" style="max-width:600px;background:#fff;border-radius:14px;border:1px solid #e0d9ce;">
      <tr><td style="padding:24px 28px 18px;border-bottom:1px solid #e0d9ce;">
        <p style="margin:0 0 6px;font-size:11px;font-weight:600;color:#1a6b5a;letter-spacing:0.1em;text-transform:uppercase;">Maya · Community Agent</p>
        <h1 style="margin:0 0 4px;font-family:Georgia,serif;font-size:22px;font-weight:700;color:#1e1c18;">Reddit digest — {datetime.now().strftime('%a %d %b %Y')}</h1>
        <p style="margin:0;font-size:13px;color:#5c5849;">{drafted_count} drafts ready for review · {skipped_count} posts skipped</p>
      </td></tr>
      <tr><td style="padding:0 14px;">
        <table cellpadding="0" cellspacing="0" border="0" width="100%">{rows_html}</table>
      </td></tr>
      <tr><td align="center" style="padding:24px 28px;border-top:1px solid #e0d9ce;">
        <a href="{SHEET_URL}" style="display:inline-block;background:#1a6b5a;color:#fff;font-size:14px;font-weight:700;padding:13px 28px;border-radius:24px;text-decoration:none;">Review all drafts in Sheet →</a>
        <p style="margin:14px 0 0;font-size:11px;color:#9b9488;">Open the Sheet, change Status to ✅ Approved / ❌ Rejected, then copy approved replies to Reddit manually.</p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""

    text = f"Reddit digest — {datetime.now().strftime('%a %d %b %Y')}\n\n"
    text += f"{drafted_count} drafts ready for review, {skipped_count} skipped.\n\n"
    for e in entries[:20]:
        text += f"r/{e.get('subreddit', '')}: {e.get('title', '')}\n  → {e.get('post_url', '')}\n\n"
    text += f"\nReview all drafts: {SHEET_URL}"

    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={
                "from": f"Maya (HouseOffer) <{EMAIL_ADDRESS}>",
                "to": [EMAIL_ADDRESS],
                "subject": f"Reddit digest — {drafted_count} new drafts to review",
                "html": html,
                "text": text,
            },
            timeout=15,
        )
        print(f"Digest email Resend: {r.status_code}")
    except Exception as e:
        print(f"Digest email error: {e}")

    # Clear buffer after sending
    try:
        os.remove(DAILY_DIGEST_BUFFER_PATH)
    except Exception:
        pass


# ── MAIN LOOP ─────────────────────────────────────────────────────────────────

def run_monitor():
    seen = load_seen()
    drafts_this_run = 0
    skipped_this_run = 0

    for sub in SUBREDDITS:
        if drafts_this_run >= MAX_DRAFTS_PER_RUN:
            break
        posts = fetch_subreddit_new(sub, limit=25)
        print(f"r/{sub}: fetched {len(posts)} posts")
        time.sleep(2)  # polite delay between subreddit fetches

        for post in posts:
            if drafts_this_run >= MAX_DRAFTS_PER_RUN:
                break

            fp = post_fingerprint(post)
            if fp in seen:
                continue

            if not is_relevant(post):
                continue

            print(f"  → Drafting reply for: {post.get('title', '')[:80]}")
            draft, err = draft_reply(post)

            if not draft:
                print(f"    skipped: {err}")
                seen[fp] = time.time()
                add_to_digest_buffer({
                    "status": "skipped",
                    "subreddit": post.get("subreddit"),
                    "title": post.get("title", "")[:200],
                    "post_url": "https://reddit.com" + post.get("permalink", ""),
                    "reason": err,
                })
                skipped_this_run += 1
                continue

            post_draft_to_sheets(post, draft)
            add_to_digest_buffer({
                "status": "drafted",
                "subreddit": post.get("subreddit"),
                "title": post.get("title", "")[:200],
                "post_url": "https://reddit.com" + post.get("permalink", ""),
                "draft_reply": draft,
            })
            seen[fp] = time.time()
            drafts_this_run += 1
            time.sleep(1)

    save_seen(seen)
    print(f"\nRun complete: {drafts_this_run} drafted, {skipped_this_run} skipped")

    if SEND_DIGEST:
        print("Sending daily digest…")
        send_digest_email()


if __name__ == "__main__":
    run_monitor()
