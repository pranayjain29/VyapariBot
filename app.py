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
from helper_funcs import *

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
1. "Does this involve recording/generating invoices?" → use this tool: Invoice_Generator_And_Transaction_Recorder
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
            await handle_delete_callback(update["callback_query"])
            return "OK"
        
        # 2. Searching for Invoice #
        msg = update.get("message")
        if msg and "text" in msg:
            text = msg["text"]
            if text.lstrip().upper().startswith("INV"):   # ← new detector
                await handle_invoice_number(msg)
                return "OK"

            # 3. No Messages
            if 'message' not in update:
                return 'OK'

        chat_id = update["message"]["chat"]["id"] 
        message = update['message']

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

<b>1. Record Sales & Generate Invoices</b>
Just tell me about your sale in plain language, and I'll handle the rest!

Required: Product name, quantity, and price per unit
Optional: Date (defaults to today), payment method (cash/credit/gpay/paytm/card), currency (INR/USD/EUR), customer name, and customer details

Example texts:
- "I sold 5 packets of tea for ₹20 each to Ram. Discount rupees 5"
- "Generate invoice for 2 laptop repairs at rupees 150 each, paid by credit card. Discount of 10%."
- "Record sale: 10 notebooks ₹25 each, customer paid via GPay"

<i>Note: To delete any transaction, use /delete.</i>

<b>2. Download Data & Business Insights</b>
Get your complete sales data or ask for reports and analysis.

Example texts:
- "Download all my sales data"
- "Show me this month's revenue"
- "Which product sells the most?"
- "Give me weekly sales report"

<b>3. General Business Advice & Support</b>
I'm here for friendly conversations and business guidance too!

Example texts:
- "How can I increase my sales?"
- "What's the best way to handle customer complaints?"
- "Help me plan my inventory"

<b>⚙️ Quick Settings:</b>

Change Language: Type `/language` followed by your preferred language
Example: /language Hindi

Set Company Details: Type `/company` followed by your business information
Example: /company ABC Store, 123 Main Street, Mumbai, 9876543210, abc@email.com, GSTIN:22AAAAA0000A1Z5, PAN:AAAAA0000A

Delete Transactions: Type '/delete' and follow the steps to delete any transaction you want.
---

<b>🔒 YOUR DATA IS SAFE WITH US</b>

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