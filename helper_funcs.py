import csv, tempfile, os, time, asyncio, logging, json, httpx

from fastapi import FastAPI, Request, status, HTTPException, Depends
from fastapi.responses import JSONResponse
from flask import Flask, request, jsonify

import requests
from dotenv import load_dotenv
from openai import  AsyncOpenAI
from typing import List, Dict, Union, Any
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
import functools

from tools_util import *
import re
from datetime import datetime
from agents import Agent, Runner, trace, function_tool
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from datetime import datetime, timedelta, timezone, date

from collections import defaultdict
from supabase import create_client, Client
import csv, tempfile, os
from app import *

# Configuration
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GEMINI_API_KEY1 = os.getenv('GEMINI_API_KEY1')
GEMINI_API_KEY2 = os.getenv('GEMINI_API_KEY2')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Initialize Supabase client
url: str = os.environ.get("SUPABASE_URL_KEY")
key: str = os.environ.get("SUPABASE_API_KEY")
supabase: Client = create_client(url, key)

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
    Adds a uniform “❌ Cancel” button. `level` helps us know where to jump back to.
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
    q = (
        supabase
        .table("vyapari_transactions")
        .select("invoice_date")
        .eq("chat_id", str(chat_id))
        .order("invoice_date", desc=True)
        .execute()
    )
    return sorted({r["invoice_date"] for r in q.data}, reverse=True)

def day_range(date_iso: str) -> tuple[str, str]:
    """
    2025-07-05T00:00:00+00:00  →  ('2025-07-05 00:00:00+00', '2025-07-05 23:59:59+00')
    """
    d = date_iso[:10]                       # 'YYYY-MM-DD'
    return (f"{d} 00:00:00+00", f"{d} 23:59:59+00")

def get_invoice_numbers(chat_id: int, date_iso: str) -> list[str]:
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

def get_item_names(chat_id: int, inv: str) -> list[str]:
    q = (
        supabase
        .table("vyapari_transactions")
        .select("item_name")
        .eq("chat_id", str(chat_id))
        .eq("invoice_number", inv)
        .execute()
    )
    return sorted({r["item_name"] for r in q.data})

async def handle_delete_callback(cq: dict):
    chat_id = cq["message"]["chat"]["id"]
    msg_id  = cq["message"]["message_id"]
    action, *parts = cq["data"].split("|")

    async def edit(text: str, kb: dict | None = None):
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
            # DEBUG
            # print("TG edit:", r1.status_code, r1.text)

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

    if action == "del_inv":
        date_short, inv = parts
        items = get_item_names(chat_id, inv)
        if not items:
            await edit("No items under that invoice.")
            return
        await edit(f"Invoice {inv}\nSelect item to delete:",
                   kb_for_items(items, inv))
        return

    if action == "del_item":
        inv, item = parts
        ok = delete_transaction(chat_id, inv, item)
        await edit("✅ Deleted." if ok else "❌ Nothing deleted.")
        await send_tx_template_button(chat_id)
        return

    if action == "del_cancel":
        await edit("❌ Delete operation cancelled.")
        await send_tx_template_button(chat_id)
        return


# --------------------------------------------------------------
# INVOICE-NUMBER TEXT HANDLER  (starts with “INV”)
# --------------------------------------------------------------
async def handle_invoice_number(msg: dict):
    """Runs whenever a user sends a text that starts with INV."""
    chat_id = msg["chat"]["id"]
    text    = msg["text"].strip()

    # Cancellation shortcut -----------------------------------------
    if text.lower() in {"/cancel", "cancel"}:
        await send_message(chat_id, "❌ Search cancelled.")
        await send_tx_template_button(chat_id)
        return

    # Retrieve items -------------------------------------------------
    items = get_item_names(chat_id, text)
    if not items:
        await send_message(chat_id,
                           f"Invoice <b>{text}</b> not found. "
                           "Please try again or /cancel.")
        return

    # Success: show items keyboard ----------------------------------
    await send_message(chat_id,
                       f"Invoice {text}\nSelect item to delete:",
                       kb_for_items(items, text))
# ---------------------------------------------------------------------------
# SEND MESSAGE helper (simplified)
# ---------------------------------------------------------------------------
async def send_message(chat_id: int, text: str, kb: dict | None = None):
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post(f"{TELEGRAM_API_URL}/sendMessage",
                     json={
                         "chat_id": chat_id,
                         "text": text,
                         "parse_mode": "HTML",
                         **({"reply_markup": kb} if kb else {})
                     })    
async def send_tx_template_button(chat_id: int):
    """
    Sends a one-tap inline button that injects a transaction template
    into the user's input box (they can edit before sending).
    """
    today = datetime.now().strftime("%Y-%m-%d")

    # The text that will appear in the input field
    template = (
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

    keyboard = {
        "inline_keyboard": [[
            {
                "text": "➕ Record Transaction",
                "switch_inline_query_current_chat": template
            }
        ]]
    }

    await send_telegram_message(
        chat_id,
        "Tap ➕ Record Transaction to insert a template you can edit:",
        reply_markup=keyboard
    )

async def send_telegram_message(chat_id, text, reply_markup=None):
    """Send a message to a specific Telegram chat (optionally with reply-markup)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup     # <<< NEW

            # Split >4 k messages into chunks (unchanged)
            if len(text) > 4096:
                for chunk in (text[i:i+4096] for i in range(0, len(text), 4096)):
                    payload["text"] = chunk
                    await client.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload)
                    await asyncio.sleep(0.1)
            else:
                await client.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload)

        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to send Telegram message: {str(e)}")
        return False

# Code Generated by Sidekick is for learning and experimentation purposes only.
async def request_phone_number(chat_id):
    keyboard = {
        "keyboard": [[{"text": "📱 Share phone number", "request_contact": True}]],
        "one_time_keyboard": True,
        "resize_keyboard": True,
    }
    await send_telegram_message(
        chat_id,
        "📞 <b>Please share your phone number to continue.</b>",
        reply_markup=keyboard,
    )

async def remove_keyboard(chat_id: int, text: str = "✅ Thanks! You're all set."):
    """Sends a message that removes the custom reply keyboard."""
    await send_telegram_message(
        chat_id,
        text,
        reply_markup={"remove_keyboard": True},
    )


