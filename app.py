import csv, tempfile, os, time, asyncio, logging, json, httpx
from contextlib import asynccontextmanager
from typing import List, Dict, Union, Any, Optional
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
import functools

from fastapi import FastAPI, Request, status, HTTPException, Depends
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

import requests
from dotenv import load_dotenv
from openai import AsyncOpenAI
from datetime import datetime, timedelta, timezone, date
from collections import defaultdict
import redis.asyncio as redis

from tools_util import *
from helper_funcs import *

import re
from agents import Agent, Runner, trace, function_tool
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global state management
class AppState:
    def __init__(self):
        self.executor: Optional[ThreadPoolExecutor] = None
        self.redis_client: Optional[redis.Redis] = None
        self.gemini_client1: Optional[AsyncOpenAI] = None
        self.gemini_client2: Optional[AsyncOpenAI] = None
        self.model1: Optional[OpenAIChatCompletionsModel] = None
        self.model2: Optional[OpenAIChatCompletionsModel] = None
        self.rate_limit_cache: Dict[int, List[float]] = defaultdict(list)

app_state = AppState()

# Configuration
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GEMINI_API_KEY1 = os.getenv('GEMINI_API_KEY1')
GEMINI_API_KEY2 = os.getenv('GEMINI_API_KEY2')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379')
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Configure Gemini
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting up Vyapari Bot...")
    
    # Initialize ThreadPoolExecutor
    app_state.executor = ThreadPoolExecutor(max_workers=20)
    
    # Initialize Redis for rate limiting and caching
    try:
        app_state.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        await app_state.redis_client.ping()
        logger.info("Redis connection established")
    except Exception as e:
        logger.warning(f"Redis connection failed: {e}. Using in-memory rate limiting.")
        app_state.redis_client = None
    
    # Initialize Gemini clients
    app_state.gemini_client1 = AsyncOpenAI(base_url=GEMINI_BASE_URL, api_key=GEMINI_API_KEY1)
    app_state.gemini_client2 = AsyncOpenAI(base_url=GEMINI_BASE_URL, api_key=GEMINI_API_KEY2)
    app_state.model1 = OpenAIChatCompletionsModel(model="gemini-2.5-flash-preview-05-20", openai_client=app_state.gemini_client1)
    app_state.model2 = OpenAIChatCompletionsModel(model="gemini-2.5-flash-preview-05-20", openai_client=app_state.gemini_client2)
    
    logger.info("All services initialized successfully")
    yield
    
    # Shutdown
    logger.info("Shutting down Vyapari Bot...")
    if app_state.executor:
        app_state.executor.shutdown(wait=False)
    if app_state.redis_client:
        await app_state.redis_client.close()
    logger.info("Shutdown complete")

app = FastAPI(
    title="Vyapari Bot - FastAPI",
    lifespan=lifespan
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ───────────────────── Optimized Rate Limiter ─────────────────────
async def rate_limiter(max_calls: int = 20, time_window: int = 60):
    """
    Optimized rate limiter using Redis when available, fallback to in-memory.
    """
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
        
        if app_state.redis_client:
            # Use Redis for distributed rate limiting
            key = f"rate_limit:{chat_id}"
            try:
                # Add current timestamp to sorted set
                await app_state.redis_client.zadd(key, {str(now): now})
                # Remove old entries
                await app_state.redis_client.zremrangebyscore(key, 0, now - time_window)
                # Count current entries
                count = await app_state.redis_client.zcard(key)
                # Set expiry
                await app_state.redis_client.expire(key, time_window)
                
                if count > max_calls:
                    logger.warning("Rate limit exceeded for %s", chat_id)
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail="Rate limit exceeded",
                    )
            except Exception as e:
                logger.error(f"Redis rate limiting failed: {e}")
                # Fallback to in-memory
                pass
        else:
            # In-memory fallback
            app_state.rate_limit_cache[chat_id] = [
                t for t in app_state.rate_limit_cache[chat_id] 
                if now - t < time_window
            ]
            
            if len(app_state.rate_limit_cache[chat_id]) >= max_calls:
                logger.warning("Rate limit exceeded for %s", chat_id)
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Rate limit exceeded",
                )
            app_state.rate_limit_cache[chat_id].append(now)

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
etc) → Use Invoice_Generator_And_Transaction_Recorder as a tool ONLY ONCE and stop when you recieve success message.
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
    if not app_state.executor:
        raise RuntimeError("ThreadPoolExecutor not initialized")
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(app_state.executor, functools.partial(func, *args, **kwargs))

# Agent factory for better resource management
class AgentFactory:
    @staticmethod
    def create_invoice_agent(context: str) -> Agent:
        return Agent(
            name="Invoice Generator", 
            instructions=INVOICE_PROMPT + context, 
            model=app_state.model1,
            tools=[handle_invoice_request]
        )
    
    @staticmethod
    def create_report_agent(context: str) -> Agent:
        return Agent(
            name="Report Generator", 
            instructions=REPORT_PROMPT + context, 
            model=app_state.model2,
            tools=[read_transactions, download_transactions_csv]
        )
    
    @staticmethod
    def create_vyapari_agent(context: str, invoice_agent: Agent) -> Agent:
        return Agent(
            name="Vyapari", 
            instructions=VYAPARI_PROMPT + context, 
            model=app_state.model1,
            tools=[invoice_agent.as_tool(
                tool_name="Invoice_Generator_And_Transaction_Recorder",
                tool_description="Generates Invoice and Records the Transaction",
            )],
            handoffs=[AgentFactory.create_report_agent(context)]
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

        # Prepare context
        current_date = datetime.now().strftime('%Y-%m-%d')
        master_context = f"\nChat ID: {chat_id}\nHistory: {history}\n Today's Date: {current_date}\n User's Preferred Language: {user_language}"
        child_context = f"\nChat ID: {chat_id}\n Today's Date: {current_date}\n User's Preferred Language: {user_language}"

        text += f" \n[Context: {child_context}. Speak in user's preferred language only]"

        # Create agents using factory pattern
        invoice_agent = AgentFactory.create_invoice_agent(child_context + f"\nCompany Details are: {company_details}")
        vyapari_agent = AgentFactory.create_vyapari_agent(master_context, invoice_agent)

        print("Created All Agents")
        with trace("Vyapari Agent"):
            response = await run_with_progress(          # <<< NEW
                chat_id,
                asyncio.wait_for(
                    Runner.run(vyapari_agent, text),
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

@app.get('/health')
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}