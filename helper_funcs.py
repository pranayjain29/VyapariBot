import csv, tempfile, os, time, asyncio, logging, json, httpx
from typing import List, Dict, Union, Any, Optional
from datetime import datetime, timedelta, timezone, date
from collections import defaultdict
from supabase import create_client, Client
import re

# Import required functions from tools_util
from tools_util import delete_transaction

# Configuration
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GEMINI_API_KEY1 = os.getenv('GEMINI_API_KEY1')
GEMINI_API_KEY2 = os.getenv('GEMINI_API_KEY2')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Initialize Supabase client with connection pooling
url: str = os.environ.get("SUPABASE_URL_KEY", "")
key: str = os.environ.get("SUPABASE_API_KEY", "")
if not url or not key:
    raise ValueError("SUPABASE_URL_KEY and SUPABASE_API_KEY must be set")
supabase: Client = create_client(url, key)

# Configure logging
logger = logging.getLogger(__name__)

def kb_delete_entry() -> dict:
    """Root menu: Pick Recent or Search."""
    return {
        "inline_keyboard": [
            [{"text": "🕑 Recent (last 10 days)",
              "callback_data": "del_recent"}],
            [{"text": "🔍 Search invoice number",
              "callback_data": "del_search"}],
            [make_cancel_btn("root")[0]],           # reuse your cancel builder
        ]
    }

def make_cancel_btn(level: str) -> list:
    """
    Adds a uniform "❌ Cancel" button. `level` helps us know where to jump back to.
    """
    return [{"text": "❌ Cancel", "callback_data": f"del_cancel|{level}"}]

def kb_for_dates(dates: list[str]) -> dict:
    rows = [
        [{"text": d[:10], "callback_data": f"del_date|{d}"}]   # show only YYYY-MM-DD
        for d in dates
    ]
    rows.append(make_cancel_btn("root"))
    return {"inline_keyboard": rows}

def get_recent_dates(chat_id: int, limit_: int = 10) -> list[str]:
    """Latest <limit_> distinct invoice dates (ISO)."""
    try:
        q = (
            supabase
            .table("vyapari_transactions")
            .select("invoice_date")
            .eq("chat_id", str(chat_id))
            .order("invoice_date", desc=True)
            .limit(limit_)
            .execute()
        )
        return sorted({r["invoice_date"] for r in q.data}, reverse=True)[:limit_]
    except Exception as e:
        logger.error(f"Error getting recent dates: {e}")
        return []

def kb_for_invoices(inv_numbers: list[str], date_iso: str) -> dict:
    date_short = date_iso[:10]                       # '2025-07-05'
    rows = [
        [{
            "text": str(inv),
            "callback_data": f"del_inv|{date_short}|{inv}"   # now ≤ 64 bytes
        }]
        for inv in inv_numbers
    ]
    rows.append(make_cancel_btn("date"))
    return {"inline_keyboard": rows}

def kb_for_items(items: list[str], inv: str) -> dict:
    """
    We no longer embed the (long) invoice number *and* the item name
    in the callback_data.  We only pass the item name.
    """
    rows = [
        [{
            "text": itm,
            "callback_data": f"del_item|{inv}|{itm}"[:64]  # just in case
        }]
        for itm in items
    ]
    rows.append(make_cancel_btn("inv"))
    return {"inline_keyboard": rows}

def get_distinct_dates(chat_id: int) -> list[str]:
    try:
        q = (
            supabase
            .table("vyapari_transactions")
            .select("invoice_date")
            .eq("chat_id", str(chat_id))
            .order("invoice_date", desc=True)
            .execute()
        )
        return sorted({r["invoice_date"] for r in q.data}, reverse=True)
    except Exception as e:
        logger.error(f"Error getting distinct dates: {e}")
        return []

def day_range(date_iso: str) -> tuple[str, str]:
    """
    2025-07-05T00:00:00+00:00  →  ('2025-07-05 00:00:00+00', '2025-07-05 23:59:59+00')
    """
    d = date_iso[:10]                       # 'YYYY-MM-DD'
    return (f"{d} 00:00:00+00", f"{d} 23:59:59+00")

def get_invoice_numbers(chat_id: int, date_iso: str) -> list[str]:
    try:
        start, end = day_range(date_iso)
        q = (
            supabase
            .table("vyapari_transactions")
            .select("invoice_number")
            .eq("chat_id", str(chat_id))
            .gte("invoice_date", start)
            .lt("invoice_date",  end)
            .execute()
        )
        return sorted({r["invoice_number"] for r in q.data})
    except Exception as e:
        logger.error(f"Error getting invoice numbers: {e}")
        return []

def get_item_names(chat_id: int, inv: str) -> list[str]:
    try:
        q = (
            supabase
            .table("vyapari_transactions")
            .select("item_name")
            .eq("chat_id", str(chat_id))
            .eq("invoice_number", inv)
            .execute()
        )
        return sorted({r["item_name"] for r in q.data})
    except Exception as e:
        logger.error(f"Error getting item names: {e}")
        return []

async def handle_delete_callback(cq: dict):
    chat_id = cq["message"]["chat"]["id"]
    msg_id  = cq["message"]["message_id"]
    action, *parts = cq["data"].split("|")

    async def edit(text: str, kb: dict | None = None):
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                # change-message
                r1 = await c.post(f"{TELEGRAM_API_URL}/editMessageText",
                                  json={
                                      "chat_id":    chat_id,
                                      "message_id": msg_id,
                                      "text":       text,
                                      "parse_mode": "HTML",
                                      **({"reply_markup": kb} if kb else {})
                                  })
                # stop spinner
                await c.post(f"{TELEGRAM_API_URL}/answerCallbackQuery",
                             json={"callback_query_id": cq["id"]})
        except Exception as e:
            logger.error(f"Error editing message: {e}")

    # ─── Entry menu ──────────────────────────────────────────────────
    if action == "del_menu":
        await edit("Select an option:", kb_delete_entry())
        return

    # ─── OPTION 1: RECENT DATES ─────────────────────────────────────
    if action == "del_recent":
        dates = get_recent_dates(chat_id, 10)
        if not dates:
            await edit("No recent invoices found.")
            return
        await edit("Select a date:", kb_for_dates(dates))
        return

    # ─── OPTION 2: SEARCH BY INVOICE NUMBER ─────────────────────────
    if action == "del_search":
        await edit("Please send the *exact* invoice number "
                   "(or /cancel to abort).")
        return

    # ─── Existing flow (date → invoice → item) ──────────────────────
    if action == "del_date":
        date_iso   = parts[0]
        date_short = date_iso[:10]
        invs = get_invoice_numbers(chat_id, date_iso)
        if not invs:
            await edit("No invoices found for that date.")
            return
        await edit(f"Date: {date_short}\nSelect invoice number:",
                   kb_for_invoices(invs, date_short))
        return

    # ─── Invoice selection ───────────────────────────────────────────
    if action == "del_inv":
        date_short, inv = parts
        items = get_item_names(chat_id, inv)
        if not items:
            await edit("No items found for that invoice.")
            return
        await edit(f"Invoice: {inv}\nSelect item to delete:",
                   kb_for_items(items, inv))
        return

    # ─── Item deletion ───────────────────────────────────────────────
    if action == "del_item":
        inv, item = parts
        try:
            success = delete_transaction(chat_id, inv, item)
            if success:
                await edit(f"✅ Deleted: {item} from invoice {inv}")
            else:
                await edit("❌ Failed to delete item. Please try again.")
        except Exception as e:
            logger.error(f"Error deleting item: {e}")
            await edit("❌ Error occurred while deleting. Please try again.")
        return

    # ─── Cancel handling ─────────────────────────────────────────────
    if action == "del_cancel":
        level = parts[0]
        if level == "root":
            await edit("❌ Cancelled.")
        elif level == "date":
            await edit("Select an option:", kb_delete_entry())
        elif level == "inv":
            # Go back to date selection
            date_iso = parts[1] if len(parts) > 1 else ""
            if date_iso:
                invs = get_invoice_numbers(chat_id, date_iso)
                if invs:
                    await edit(f"Date: {date_iso[:10]}\nSelect invoice number:",
                               kb_for_invoices(invs, date_iso[:10]))
                else:
                    await edit("Select an option:", kb_delete_entry())
            else:
                await edit("Select an option:", kb_delete_entry())
        return

async def handle_invoice_number(msg: dict):
    """Handle invoice number search and deletion."""
    chat_id = msg["chat"]["id"]
    text = msg["text"].strip()
    
    if text.upper() == "/CANCEL":
        await send_telegram_message(chat_id, "❌ Cancelled.")
        return
    
    # Extract invoice number (remove "INV" prefix if present)
    inv_number = text.upper().replace("INV", "").strip()
    
    try:
        # Find items for this invoice
        items = get_item_names(chat_id, inv_number)
        if not items:
            await send_telegram_message(chat_id, f"No items found for invoice {inv_number}")
            return
        
        # Send item selection keyboard
        await send_telegram_message(
            chat_id,
            f"Invoice: {inv_number}\nSelect item to delete:",
            reply_markup=kb_for_items(items, inv_number)
        )
    except Exception as e:
        logger.error(f"Error handling invoice number: {e}")
        await send_telegram_message(chat_id, "❌ Error occurred. Please try again.")

async def send_message(chat_id: int, text: str, kb: dict | None = None):
    """Send message with optional keyboard."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{TELEGRAM_API_URL}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    **({"reply_markup": kb} if kb else {})
                }
            )
            return response.json()
    except Exception as e:
        logger.error(f"Error sending message: {e}")
        return None

async def send_tx_template_button(chat_id: int):
    """
    Sends inline buttons that inject templates into the user's input box
    (they can edit before sending).
    """
    today = datetime.now().strftime("%Y-%m-%d")

    # Template for recording a sale
    sale_template = (
        "Record Transaction:\n"
        "Item(s): <item name>\n"
        "Quantity(s): 1\n"
        "Price(s) per unit: 0\n"
        "Discount(s) per unit: 0\n"
        "GST: 0\n"
        f"Date: {today}\n"
        "Customer Name and Details:\n"
        "Payment method: cash\n"
        "(You can edit any value before sending.)"
    )

    # Template for downloading data
    download_template = "Download all my sales data"

    # Template for getting reports
    report_template = "Show me this month's revenue"

    keyboard = {
        "inline_keyboard": [
            [{
                "text": "📝 Record a Sale",
                "switch_inline_query_current_chat": sale_template
            }],
            [{
                "text": "📊 Download Data",
                "switch_inline_query_current_chat": download_template
            }],
            [{
                "text": "📈 Get Reports",
                "switch_inline_query_current_chat": report_template
            }]
        ]
    }

    await send_telegram_message(
        chat_id,
        "Tap any button to insert a template you can edit:",
        reply_markup=keyboard
    )

async def send_telegram_message(chat_id, text, reply_markup=None):
    """Send message to Telegram with error handling."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML"
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            
            response = await client.post(
                f"{TELEGRAM_API_URL}/sendMessage",
                json=payload
            )
            
            if response.status_code != 200:
                logger.error(f"Telegram API error: {response.status_code} - {response.text}")
                return False
                
            return True
    except Exception as e:
        logger.error(f"Error sending Telegram message: {e}")
        return False

async def request_phone_number(chat_id):
    """Request phone number from user."""
    try:
        keyboard = {
            "keyboard": [[{"text": "📱 Share Phone Number", "request_contact": True}]],
            "resize_keyboard": True,
            "one_time_keyboard": True
        }
        await send_telegram_message(
            chat_id,
            "Please share your phone number to continue:",
            keyboard
        )
    except Exception as e:
        logger.error(f"Error requesting phone number: {e}")

async def remove_keyboard(chat_id: int, text: str = "✅ Thanks! You're all set."):
    """Remove keyboard and send confirmation message."""
    try:
        keyboard = {"remove_keyboard": True}
        await send_telegram_message(chat_id, text, keyboard)
    except Exception as e:
        logger.error(f"Error removing keyboard: {e}")


