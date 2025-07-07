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


@function_tool
def handle_invoice_request(
    chat_id: int,
    item_names: List[str],
    quantities: List[int],
    prices: List[float],
    discounts: List[float],
    date: str,
    raw_message: str,

    # OPTIONAL Company details
    payment_method="cash",
    company_name="Company Name",
    company_address="Company Address",
    company_city="Company City",
    company_phone="Company Number",
    company_email="Company Mail",
    company_gstin="Company GSTIN",
    company_pan="Company PAN",

    # OPTIONAL Customer details
    customer_name="Customer Name",
    customer_address="Customer Address",
    customer_city="Customer City, State - PIN",
    customer_details="",
    customer_gstin="",

    # OPTIONAL Tax details
    cgst_rate=0.0,
    sgst_rate=0.0,
    igst_rate=0.0,

) -> str:
    """
    Generates Invoices.
    Accept parallel lists for item name, quantity, and price.
    Many OPTIONAL fields which you must not include if the value is not provided.
    """
    try:
        # Validation
        if not all([item_names, quantities, prices]):
            return "❌ Missing required fields for invoice generation."
        
        if not (len(item_names) == len(quantities) == len(prices)):
            return "❌ Item lists must have equal length."
        
        # Validate data types
        for i, (name, qty, price) in enumerate(zip(item_names, quantities, prices)):
            if not isinstance(name, str) or not name.strip():
                return f"❌ Invalid item name at position {i+1}"
            if not isinstance(qty, int) or qty <= 0:
                return f"❌ Invalid quantity at position {i+1}"
            if not isinstance(price, (int, float)) or price <= 0:
                return f"❌ Invalid price at position {i+1}"

        # Build the structure expected by generate_invoice
        items = [
            {"name": n, "qty": q, "rate": p, "discount": d}
            for n, q, p, d in zip(item_names, quantities, prices, discounts)
        ]

        # Call the updated invoice generator
        invoice_file, invoice_number = generate_invoice(
            items=items,
            date=date,
            chat_id=chat_id,

            # Company details
            company_name=company_name,
            company_address=company_address,
            company_city=company_city,
            company_phone=company_phone,
            company_email=company_email,
            company_gstin=company_gstin,
            company_pan=company_pan,
            
            customer_name=customer_name,
            customer_address=customer_address,
            customer_city=customer_city,
            customer_gstin=customer_gstin,

            # Tax details
            cgst_rate=cgst_rate,
            sgst_rate=sgst_rate,
            igst_rate=igst_rate
        )

        # Send invoice as document
        send_document(chat_id, invoice_file)

        # Cleanup
        try:
            os.remove(invoice_file)
        except:
            pass

        blended_tax_rate = cgst_rate + sgst_rate + igst_rate        # e.g. 9 + 9 + 0 = 18

        for itm in items:
            # -> write one DB row per item
            write_transaction(
                chat_id            = chat_id,
                item_name          = itm["name"],
                quantity           = itm["qty"],
                price_per_unit     = itm["rate"],
                tax_rate           = blended_tax_rate,
                invoice_date       = date,
                invoice_number     = invoice_number,
                discount_per_unit  = itm.get("discount", 0.0) or 0.0,
                raw_message        = raw_message,                 # or the original user text if you keep it
                payment_method     = payment_method,
                currency           = "INR",
                customer_name      = customer_name,
                customer_details   = customer_details,
            )
        return f"✅ Invoice generated and recorded successfully! Invoice number is {invoice_number}"

    except Exception as e:
        logger.error(f"Error generating invoice: {str(e)}")
        return "❌ Sorry, there was an error generating the invoice. Please try again."
 
@function_tool
def download_transactions_csv(chat_id: int) -> str:
    """
    Fetches transactions via read_transactions(), writes them to a temporary
    CSV file, sends it to the user, then deletes the temp file.

    """
    try:
        csv_name = download_Transactions_CSV(chat_id=chat_id)

        # ──  Send file via Telegram ─────
        send_document(chat_id, csv_name)

        # ── Housekeeping ─────
        os.remove(csv_name)
        return "✅ CSV Sent Successfully."

    except Exception as e:
        print(f"[download_transactions_csv] {e}")
        return "❌ Error in making CSV. Sorry brother."

