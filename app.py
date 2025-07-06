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

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Vyapari Bot - FastAPI")

# Configuration
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GEMINI_API_KEY1 = os.getenv('GEMINI_API_KEY1')
GEMINI_API_KEY2 = os.getenv('GEMINI_API_KEY2')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Configure Gemini
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
gemini_client1 = AsyncOpenAI(base_url=GEMINI_BASE_URL, api_key=GEMINI_API_KEY1)
model1 = OpenAIChatCompletionsModel(model="gemini-2.5-flash-preview-05-20", openai_client=gemini_client1)

gemini_client2 = AsyncOpenAI(base_url=GEMINI_BASE_URL, api_key=GEMINI_API_KEY2)
model2 = OpenAIChatCompletionsModel(model="gemini-2.5-flash-preview-05-20", openai_client=gemini_client2)

executor: ThreadPoolExecutor | None = None

# ───────────────────── rate-limit dependency ─────────────────────
def rate_limiter(max_calls: int = 5, time_window: int = 60):
    """
    FastAPI dependency version of the per-chat_id rate-limit.
    Stores timestamps in a closure-level dict.
    """
    call_times: Dict[int, List[float]] = {}

    async def _dependency(request: Request):
        body: Dict[str, Any] = await request.json()
        chat_id = (
            body.get("message", {})
            .get("chat", {})
            .get("id")
            if "message" in body
            else None
        )
        if chat_id is None:
            return  # Let it pass (non-telegram test ping etc.)

        now = time.time()
        call_times.setdefault(chat_id, [])
        call_times[chat_id] = [t for t in call_times[chat_id] if now - t < time_window]

        if len(call_times[chat_id]) >= max_calls:
            logger.warning("Rate limit exceeded for %s", chat_id)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded",
            )
        call_times[chat_id].append(now)

    return _dependency

# Vyapari character system prompt
VYAPARI_PROMPT = r"""
You are a seasoned Indian businessman (Vyapari), an AI chatbot with these traits:

PERSONALITY & COMMUNICATION:
- 🔑 Rule: ALWAYS reply in the SAME language as the user. FOLLOW User's Preferred Language.
- Traits: Direct, witty, practical, middle-aged with a sharp sense of humor.
- Business Wisdom: Use local proverbs or phrases naturally

TASK ROUTING (with COMPLETE language/context passed):

1. INVOICE/SALES REQUESTS ("Sold", "Transactions", "Invoice", "Recording transaction/sales",
etc) → Use Invoice_Generator_And_Transaction_Recorder as a tool ONCE.
"
2. Report/Analytics ("Data Download", "Report", "Sales data", "Insights",
"Summaries/Performance Queries") → Hand off to Report_Agent 

3. General Chat → Handle directly
**Examples**: Greetings, business advice, general questions, casual conversations

DECISION FRAMEWORK:
Before responding, ask yourself:
1. "Does this involve recording/generating invoices?" → Invoice_Agent
2. "Does this need transaction data/reports?" → Report_Agent  
3. "Is this general business chat?" → Handle myself

FORMATTING:
1️⃣  Allowed formatting
    • Emojis 😊, 🚀, etc.  
    • Basic HTML tags only:
        <b>, <strong>, <i>
    • Bullet / numbered lists.

2️⃣  Forbidden formatting (DON'T EVEN INCLUDE in ANY TEXT, IT WILL GIVE ERROR)
    ✘ No <em>, <li>, <ol>, <font>, <span style>, colour attributes, CSS, JavaScript or <script>.  
    ✘ No tables (<table>, <tr>, <td>) or advanced HTML/CSS positioning.  
    ✘ No external assets (images, iframes).

Remember: You're the wise business advisor who knows when to delegate!
"""

INVOICE_PROMPT = """
You are VYAPARI's INVOICE SPECIALIST.

🗣️ Rule: Reply in user's language. FOLLOW User's Language.

### REQUIRED FIELDS:
1. **chat_id** (Integer): Provided to you
2. **item_names** (List of String): Product/service name
3. **quantities** (List of integer): Must be numeric (convert "baara" → 12, "paach" → 5, if not mentioned take it as 1)
4. **prices** (List of float): Price per unit in numbers only
5. **raw_message** (String): The user's text as it is

### OPTIONAL FIELDS:
6. **discounts** (List of float): discount per unit given for that item. (Assume 0.0 if not provided)
7. **cgst_rate, sgst_rate and igst_rate**: 0.0 if not provided
8. **payment_method** (String): cash/credit/gpay/paytm/debit card (default: "cash")
9. **company details**: Various company details like name, address, etc.
10. **date** (string): Format as YYYY-MM-DD (if missing, today's date)
11. **payment_method** (string): cash/credit/gpay/paytm/card (default: "cash")
12. **currency** (string): INR/USD/EUR (default: "INR")
13. **customer_name** (string): If mentioned
14. **customer_details** (String): Phone, address if provided

If some fields are not provided, please don't pass it as an argument.

PROCESSING WORKFLOW:

### STEP 1: DATA VALIDATION
- If mentioned 5% Tax or 5% GST, consider it as 2.5 CGST RATE and 2.5 SGST RATE.
- Validate Required Fields Only. If some of the important fields are absent, HELP user
  to write all the required information, ask him to mention everything in one text,
  teach with examples, and DON'T use any tool or handoffs.

### STEP 2: INVOICE GENERATION
- Generates Invoices. Accept parallel lists for item name, quantity, and price.
- Use `handle_invoice_request` tool only ONCE for all items
- Include ALL transaction items in single invoice

Remember: Use tool only ONCE for all items and then notify the user. 
Accuracy is key - one mistake affects the entire business record!
"""

REPORT_PROMPT = """
You are the ANALYTICS SPECIALIST of VYAPARI - expert in business intelligence and reporting.

🗣️ Rule: ALWAYS Reply in user's language. FOLLOW User's Preferred Language.

Tools:
- To fetch data: "read_transactions" (all the necessary parameters are provided to you)
- For CSV export: "download_transactions_csv"

Sometimes user might only need the transactions csv file. In that case use only the tool: download_transactions_csv.

## TASK FLOW:

### 1. IF DATA EXPORT REQUEST:

- If the user says "export", "download", "csv", "sheet", "data", "excel" etc.,
  call `download_transactions_csv`.
- Only after generating csv, confirm, stop and provide the output. Thats it.
...

### 2. INSIGHT REPORT (if not EXPORT):

## PERSONALITY (Maintain Vyapari Character):
- **Tone**: Knowledgeable business consultant with Indian context.
- **Expertise**: Deep understanding of Indian business patterns and metrics.
- Provide deep insights and patterns to help him grow his business.

## PRIMARY MISSION:
Transform transaction data into actionable business insights.

## REPORTING CAPABILITIES:

### FINANCIAL REPORTS:
- Revenue analysis (daily/weekly/monthly/yearly)
- Profit margins and trends
- Payment method breakdowns
- Currency-wise summaries

### PRODUCT ANALYTICS:
- Best/worst selling products
- Inventory movement patterns
- Seasonal demand analysis
- Product performance rankings

### CUSTOMER INSIGHTS:
- Customer purchase behavior
- Repeat customer identification
- Customer value analysis
- Payment preference patterns

### BUSINESS INTELLIGENCE:
- Growth trend analysis
- Comparative period reports
- Performance benchmarking
- Profitability insights

## REPORT GENERATION WORKFLOW:

### STEP 1: DATA RETRIEVAL
- Use `read_transactions` tool to fetch relevant data
- Validate data completeness and accuracy

### STEP 2: UNDERSTAND REQUEST
Identify specific report type:
- Time-based: "last month", "this year", "quarterly"
- Product-based: "rice sales", "top products"
- Customer-based: "repeat customers", "payment modes"
- Comparative: "vs last year", "growth trends"

### STEP 3: ANALYSIS & INSIGHTS
- Calculate relevant metrics and KPIs
- Identify trends, patterns, and anomalies  
- Generate actionable business recommendations
- Compare with previous periods where relevant

### STEP 4: PRESENTATION
- Format as clean text (NO markdown symbols like "#" or **)
- Use Indian business context (festivals, seasons, local patterns)
- Include both numbers and insights
- Provide specific recommendations

## ERROR HANDLING (reply in user's language):
- **No Data**: "There are no transactions in this period"
- **Insufficient Data**: "I need more data for reporting"
- **Data Issues**: Identify and report data quality problems

FORMATTING:
1️⃣  Allowed formatting
    • Emojis 😊, 🚀, etc.  
    • Basic HTML tags listed ONLY. NOTHING ELSE:
        <b>, <strong>, <i>
    • Bullet / numbered lists.
    
    NOT ALLOWED
    •  Advanced formatting like <li>, etc are NOT allowed. Please don't use it.

Remember: Your reports should help the user make better business decisions - focus on actionable insights, not just numbers!
CRITICAL: DO NOT COMPLETE BEFORE PERFORMING ALL THE STEPS.
"""

PENDING_SEARCH: set[int] = set()          # chat_ids waiting for an invoice #

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
        PENDING_SEARCH.add(chat_id)            # mark chat as waiting
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


# ---------------------------------------------------------------------------
# PLAIN-TEXT MESSAGE HANDLER  – catches invoice number typed by user
# ---------------------------------------------------------------------------
async def handle_text_message(msg: dict):
    """
    Called for every non-command text message.
    If the user was prompted to search an invoice, treat the message content
    as the invoice number and jump to the 'select item' step.
    """
    chat_id = msg["chat"]["id"]
    text    = msg["text"].strip()

    # If not waiting for an invoice number → ignore / continue normal flow
    if chat_id not in PENDING_SEARCH:
        return

    # Cancel?
    if text.lower() in {"/cancel", "cancel"}:
        PENDING_SEARCH.discard(chat_id)
        await send_message(chat_id, "❌ Search cancelled.")
        await send_tx_template_button(chat_id)
        return

    # Search
    items = get_item_names(chat_id, text)
    if not items:
        await send_message(chat_id,
                           f"Invoice <b>{text}</b> not found. "
                           "Please try again or /cancel.")
        return

    # Found – show item keyboard
    PENDING_SEARCH.discard(chat_id)
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

def run_blocking(func, *args, **kwargs):
    """Return an awaitable that executes *func* in the thread-pool."""
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(executor, functools.partial(func, *args, **kwargs))

@app.post("/webhook", dependencies=[Depends(rate_limiter(max_calls=20, time_window=60))])
async def telegram_webhook(request: Request):
    start_time = time.time()
    try:
        update = await request.json()

        # 1. CallbackQuery  → delete-wizard branch
        if "callback_query" in update:
            asyncio.create_task(handle_delete_callback(update["callback_query"]))
            return "OK"

        # 2. Plain text     → check if we’re waiting for invoice #
        if "message" in update and "text" in update["message"]:
            asyncio.create_task(handle_text_message(update["message"]))
            return "OK"

        if 'message' not in update:
            return 'OK'

        message = update['message']
        chat_id = message['chat']['id']

        # Telegram send
        async def send(msg: str):
            await send_telegram_message(chat_id, msg)

        if "contact" in message:
            phone_number = message["contact"].get("phone_number")
            if phone_number:
                await run_blocking(update_user_field, chat_id, "phone", phone_number)
                # remove the keyboard right after storing
                await remove_keyboard(chat_id)
            return "OK"


        user_name = (
            message.get('from', {}).get('username')      # preferred: Telegram @handle
            or message.get('from', {}).get('first_name') # fallback to first name
            or ''                                        # default empty string
        )
        message_ts = message.get('date')  # epoch‐seconds from Telegram
        text = "User: "
        text += message.get('text', '')

        if not text:
            return 'OK'

        print(chat_id)
        print(user_name)
        print(message.get('text', ''))

        # -------------- synchronous helpers --------------
        

        # (1) DB helpers
        read_val   = lambda col: run_blocking(
            read_value_by_chat_id,
            table_name="vyapari_user",
            chat_id=chat_id,
            column_name=col,
        )

        
        async def send_chat_action(chat_id: int, action: str = "typing"):
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(
                    f"{TELEGRAM_API_URL}/sendChatAction",
                    json={"chat_id": chat_id, "action": action},
                )

        async def typing_spinner(chat_id: int, stop_evt: asyncio.Event, every: int = 2):
            """
            Sends 'typing' every <every> seconds until stop_evt is set().
            """
            try:
                while not stop_evt.is_set():
                    await send_chat_action(chat_id, "typing")
                    await asyncio.sleep(every)
            except Exception as e:
                logger.warning(f"typing_spinner error: {e}")


        async def run_with_progress(chat_id: int, coro, ack_msg="👍 Got it…", done_msg=None):
            """
            1) Fires an immediate ack to the user.
            2) Shows 'typing' while <coro> runs.
            3) Optionally pushes <done_msg> after success.
            Returns coro's result.
            """
            await send_telegram_message(chat_id, ack_msg)

            stop_evt = asyncio.Event()
            spinner  = asyncio.create_task(typing_spinner(chat_id, stop_evt))

            try:
                result = await coro          # await the real long task
            finally:
                stop_evt.set()               # stop spinner even on error
                await spinner

            if done_msg:
                await send_telegram_message(chat_id, done_msg)

            return result

        
        user_record_future     = run_blocking(read_user, chat_id)
        last_messages_future   = run_blocking(get_last_messages, chat_id)
        user_language_future   = read_val("language")
        company_details_future = read_val("company_details")

        if message.get('text', '').startswith(r"/start"):
            start_text = r"""
            🎉 Welcome to Your Business Assistant Bot!

Hello! I'm here to help you manage your business with simple, everyday language. Whether you're running a small shop, freelancing, or managing any business, I'll make record-keeping easy for you.

📝 What I Can Do For You:

1. Record Sales & Generate Invoices
Just tell me about your sale in plain language, and I'll handle the rest!

Required: Product name, quantity, and price per unit
Optional: Date (defaults to today), payment method (cash/credit/gpay/paytm/card), currency (INR/USD/EUR), customer name, and customer details

Example texts:
- "I sold 5 packets of tea for ₹20 each to Ram. Discount rupees 5"
- "Generate invoice for 2 laptop repairs at rupees 150 each, paid by credit card. Discount of 10%."
- "Record sale: 10 notebooks ₹25 each, customer paid via GPay"

2. Download Data & Business Insights
Get your complete sales data or ask for reports and analysis.

Example texts:
- "Download all my sales data"
- "Show me this month's revenue"
- "Which product sells the most?"
- "Give me weekly sales report"

3. General Business Advice & Support
I'm here for friendly conversations and business guidance too!

Example texts:
- "How can I increase my sales?"
- "What's the best way to handle customer complaints?"
- "Help me plan my inventory"

⚙️ Quick Settings:

Change Language: Type `/language` followed by your preferred language
Example: /language Hindi

Set Company Details: Type `/company` followed by your business information
Example: /company ABC Store, 123 Main Street, Mumbai, 9876543210, abc@email.com, GSTIN:22AAAAA0000A1Z5, PAN:AAAAA0000A

---

🔒 YOUR DATA IS SAFE WITH US

Ready to get started? Just tell me about your first sale or ask me anything!
            """
            await send(start_text)
            await send_tx_template_button(chat_id)
            return "OK"

        # ───────────── /delete entry point ─────────────
        if message.get("text", "") == "/delete":
            await send_telegram_message(
                chat_id,
                "Delete transaction – choose how you want to find it:",
                reply_markup=kb_delete_entry()       # <- new keyboard
            )
            return "OK"

        if message.get('text', '').startswith(r"/language"):
            lang = text.split(r"/language", 1)[-1].strip()
            update_user_field(chat_id, "language", lang)
            await send(f"Language set to {lang} ✅")
            await send_tx_template_button(chat_id)   # new line

            return "OK"

        if message.get('text', '').startswith(r"/company"):
            comp = text.split(r"/company", 1)[-1].strip()
            update_user_field(chat_id, "company_details", comp)
            await send(f"Company Details set to {comp} ✅")
            await send_tx_template_button(chat_id)   # new line

            return "OK"

        # -------------- gather awaited results --------------
        (
            user_record,
            last_msgs,
            user_language,
            company_details
        ) = await asyncio.gather(
            user_record_future,
            last_messages_future,
            user_language_future,
            company_details_future,
        )

        phone_in_db = user_record.get("phone") if user_record else None
        print(f"Phone number is: {phone_in_db}")
        if not phone_in_db:
            await request_phone_number(chat_id)
            return "OK"

        # ------------------------------------------------------------------
        # 1.  Look up user; insert if not found
        # ------------------------------------------------------------------
        if not user_record:
            await run_blocking(write_user, chat_id, user_name)
        else:
            await run_blocking(update_last_used_date, chat_id, user_name)

        # 2. Log the message and trim to last 5
        await run_blocking(log_message, chat_id, text, message_ts)

        # 3. Fetch last 5 and compose a single variable for the bot
        history     = "\n".join(
            f"[{m['message_date']}] {m['message_text']}" for m in last_msgs
        )

        global VYAPARI_PROMPT, INVOICE_PROMPT, REPORT_PROMPT
        Vyapari_PROMPT = VYAPARI_PROMPT
        Invoice_PROMPT = INVOICE_PROMPT
        Report_PROMPT = REPORT_PROMPT

        # Prepare context
        current_date = datetime.now().strftime('%Y-%m-%d')
        master_context = f"\nChat ID: {chat_id}\nHistory: {history}\n Today's Date: {current_date}\n User's Preferred Language: {user_language}"
        child_context = f"\nChat ID: {chat_id}\n Today's Date: {current_date}\n User's Preferred Language: {user_language}"
        text += f" \n[Context: {child_context}. Speak in user's preferred language only]"

        Vyapari_PROMPT += master_context
        Invoice_PROMPT += child_context
        Report_PROMPT  += child_context

        Invoice_PROMPT += f"\nCompany Details are: {company_details}"

        Invoice_Agent = Agent(
                name="Invoice Generator", 
                instructions=Invoice_PROMPT, 
                model=model1,
                tools=[handle_invoice_request])
                
        Report_Agent = Agent(
                name="Report Generator", 
                instructions=Report_PROMPT, 
                model=model2,
                tools=[read_transactions, download_transactions_csv])

        Vyapari_Agent = Agent(
                name="Vyapari", 
                instructions=Vyapari_PROMPT, 
                model=model1,
                tools = [Invoice_Agent.as_tool(
                tool_name="Invoice_Generator_And_Transaction_Recorder",
                tool_description="Generates Invoice and Records the Transaction",
                )],
                handoffs=[Report_Agent])

        print("Created All Agents")
        with trace("Vyapari Agent"):
            response = await run_with_progress(          # <<< NEW
                chat_id,
                asyncio.wait_for(
                    Runner.run(Vyapari_Agent, text),
                    timeout=180,
                ),
                ack_msg="🤔 Let me figure that out…",    # appears instantly
                # done_msg can be omitted; final_output arrives right after
            )

        await send(response.final_output)
        await send_tx_template_button(chat_id)

        bot_text = "Assitant: "
        bot_text += response.final_output
        await run_blocking(
            log_message,
            chat_id,
            bot_text,
            int(datetime.now(timezone.utc).timestamp()),
        )

        return 'OK'

    except Exception as e:
        logger.error(f"Error processing webhook: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal error")

    finally:
        logger.info("request duration %.2fs", time.time() - start_time)


def send_document(chat_id, file_path):
    """Send document to Telegram chat"""
    url = f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN')}/sendDocument"
    with open(file_path, 'rb') as file:
        files = {'document': file}
        data = {'chat_id': chat_id}
        response = requests.post(url, data=data, files=files)
    print(f"Invoice Sent {response.json()}")
    return response.json()

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}

@app.on_event("startup")
async def _startup_checks():
    global executor
    if not TELEGRAM_TOKEN or not GEMINI_API_KEY1:
        logger.error("Missing required environment variables. Exiting…")
        raise RuntimeError("Incomplete ENV")

    executor = ThreadPoolExecutor(max_workers=20)
    logger.info("ThreadPoolExecutor started with %d workers", 20)

@app.on_event("shutdown")
async def _shutdown_pool():
    if executor:
        executor.shutdown(wait=False)
        logger.info("ThreadPoolExecutor shut down")