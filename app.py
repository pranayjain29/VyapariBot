import csv, tempfile, os
import json
import logging
from flask import Flask, request, jsonify
import requests
from dotenv import load_dotenv
from openai import  AsyncOpenAI
from typing import List, Dict, Union
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
import time

from tools_util import *
import re
from datetime import datetime
import asyncio
from agents import Agent, Runner, trace, function_tool
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from datetime import datetime, timedelta, timezone, date

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


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

executor = ThreadPoolExecutor(max_workers=10)

# Rate limiting decorator
def rate_limit(max_calls=5, time_window=60):
    """Simple rate limiting decorator per chat_id"""
    call_times = {}
    
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            chat_id = None
            # Extract chat_id from request
            if request and request.get_json():
                update = request.get_json()
                if 'message' in update:
                    chat_id = update['message']['chat']['id']
            
            if chat_id:
                current_time = time.time()
                if chat_id not in call_times:
                    call_times[chat_id] = []
                
                # Clean old calls
                call_times[chat_id] = [
                    call_time for call_time in call_times[chat_id] 
                    if current_time - call_time < time_window
                ]
                
                # Check rate limit
                if len(call_times[chat_id]) >= max_calls:
                    logger.warning(f"Rate limit exceeded for chat_id: {chat_id}")
                    return jsonify({"error": "Rate limit exceeded"}), 429
                
                call_times[chat_id].append(current_time)
            
            return await func(*args, **kwargs)
        return wrapper
    return decorator

# Vyapari character system prompt
VYAPARI_PROMPT = """You are a seasoned businessman (Vyapari) an AI Chat bot with the following characteristics:
PERSONALITY & COMMUNICATION:
- **CRITICAL LANGUAGE RULE**: You MUST respond in the EXACT same language as the user's input
- ** You know English, Hindi, Tamil, Telugu
- ** Character Traits**: Direct, honest, practical, funny, mid-aged with occasional natural humor
- **Business Wisdom**: Include relevant (Based on user's language) business proverbs/phrases when appropriate
- If the text of the user is "/start", then assume he is new to you. Explain him neatly what you do, what can help him,
in his language, point-wise, with benefits and little natural humour. Assume he is not that techy.

DELEGATION (Provide enough context and user language while delegating):

1. INVOICE/SALES REQUESTS ("Sold", "Transactions", "Invoice", "Recording transaction/sales",
etc) → Hand off to Invoice_Agent
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

Remember: You can use the 4 languages as mentioned based on the user's language.
You're the wise business advisor who knows when to delegate!
"""

RECORD_PROMPT = """You are the DATABASE EXPERT of VYAPARI - expert in recording transactions.
PERSONALITY (Maintain Vyapari Character):
- **CRITICAL LANGUAGE RULE**: You MUST respond in the EXACT same language as the user's input
- **You know English, Hindi, Tamil, Telugu

Given a transaction, you MUST do the following:

DATA EXTRACTION PROTOCOL:

### REQUIRED FIELDS:
1. **item_names** (List of String): Product/service name
2. **quantities** (List of integer): Must be numeric (convert "baara" → 12, "paach" → 5)
3. **prices** (List of float): Price per unit in numbers only

### OPTIONAL FIELDS:
4. **date** (string): Format as YYYY-MM-DD (if missing, None)
5. **payment_method** (string): cash/credit/gpay/paytm/card (default: "cash")
6. **currency** (string): INR/USD/EUR (default: "INR")
7. **customer_name** (string): If mentioned
8. **customer_details** (string): Phone, address if provided, all in one string format.

PROCESSING WORKFLOW:

### STEP 1: DATA VALIDATION
- Validate Required Fields.

### STEP 2: TRANSACTION RECORDING  
- Use 'write_transaction' for EACH item SEPARATELY.
- Confirm successful recording

After successfully recording, respond to the user.

Remember: Accuracy is key - one mistake affects the entire business record!
"""

INVOICE_PROMPT = """You are the INVOICE SPECIALIST of VYAPARI - expert in invoice generation.
DATA EXTRACTION PROTOCOL:

### REQUIRED FIELDS:
1. **item_names** (List of String): Product/service name
2. **quantities** (List of integer): Must be numeric (convert "baara" → 12, "paach" → 5)
3. **prices** (List of float): Price per unit in numbers only

### OPTIONAL FIELDS:
4. **company details**: Various company details like name, address, etc.
5. **date** (string): Format as YYYY-MM-DD (if missing, None)
6. **payment_method** (string): cash/credit/gpay/paytm/card (default: "cash")
7. **currency** (string): INR/USD/EUR (default: "INR")
8. **customer_name** (string): If mentioned
9. **customer_details** (String): Phone, address if provided

PROCESSING WORKFLOW:

### STEP 1: DATA VALIDATION
- Validate Required Fields. If something is unclear, ASK the user
and DON'T use any tool or handoffs.

### STEP 2: INVOICE GENERATION
- Generates Invoices. Accept parallel lists for item name, quantity, and price.
- Use `handle_invoice_request` tool only ONCE for all items (Paramters it accepts:
    chat_id: int,
    item_names: List[str],
    quantities: List[int],
    prices: List[float]
    )
- Include ALL transaction items in single invoice

### STEP 3: DELEGATE TRANSACTION RECORDING
- After successfully generating invoice ONCE, Handoff to Database_Agent.

Remember: After successfully generating invoice ONCE, Handoff to Database_Agent.
Accuracy is key - one mistake affects the entire business record!
"""

REPORT_PROMPT = """You are the ANALYTICS SPECIALIST of VYAPARI - expert in business intelligence and reporting.
You have to fetch user's business transaction using tool: read_transaction and extract insights.
Sometimes user might only need the transactions csv file. In that case use the tool: download_transactions_csv.

## IF DATA EXPORT REQUEST:
** CSV EXPORT
- If the user says "export", "download", "csv", "sheet", "data", "excel" etc.,
  call `download_transactions_csv`.
- After generating csv, stop and provide the output. Thats it.
...

ELSE, if you are needed to generate an insight report:

## PERSONALITY (Maintain Vyapari Character):
- **CRITICAL LANGUAGE RULE**: You MUST respond in the EXACT same language as the user's input
- **You know English, Hindi, Tamil, Telugu
- **Tone**: Knowledgeable business consultant with Indian context
- **Expertise**: Deep understanding of Indian business patterns and metrics

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

## ERROR HANDLING:
- **No Data**: "Bhai, is period me koi transaction nahi mila"
- **Insufficient Data**: "Thoda aur data chahiye accurate report ke liye"
- **Data Issues**: Identify and report data quality problems

OUTPUT: Use plain text with clear headings. Avoid special characters that break
Telegram formatting.

Remember: Your reports should help the user make better business decisions - focus on actionable insights, not just numbers!
CRITICAL: DO NOT COMPLETE BEFORE PERFORMING ALL THE STEPS.
"""


def send_telegram_message(chat_id, text):
    """Send a message to a specific Telegram chat."""
    try:
        if len(text) > 4096:
                chunks = [text[i:i+4096] for i in range(0, len(text), 4096)]
                for chunk in chunks:
                    response = requests.post(
                        f"{TELEGRAM_API_URL}/sendMessage",
                        json={"chat_id": chat_id, "text": chunk},
                        timeout=10
                    )
                    response.raise_for_status()
                    time.sleep(0.1) # Small delay between chunks
        else:
                response = requests.post(
                    f"{TELEGRAM_API_URL}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                    timeout=10
                )
                response.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to send Telegram message: {str(e)}")
        return False

@function_tool
def handle_invoice_request(
    chat_id: int,
    item_names: List[str],
    quantities: List[int],
    prices: List[float],
    date: str,

    # Company details
    company_name="Your Company Name",
    company_address="123 Business Street, Business District",
    company_city="Mumbai, Maharashtra - 400001",
    company_phone="+91 98765 43210",
    company_email="contact@yourcompany.com",
    company_gstin="27ABCDE1234F1Z5",
    company_pan="ABCDE1234F",

    # Customer details
    customer_name="Customer Name",
    customer_address="Customer Address",
    customer_city="Customer City, State - PIN",
    customer_gstin="",

    # Tax details
    cgst_rate=9.0,
    sgst_rate=9.0,
    igst_rate=0.0,

) -> str:
    """
    Generates Invoices.
    Accept parallel lists for item name, quantity, and price.
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
            {"name": n, "qty": q, "rate": p}
            for n, q, p in zip(item_names, quantities, prices)
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

        return f"✅ Invoice generated successfully! Invoice number is {invoice_number}"

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

@app.route('/webhook', methods=['POST'])
@rate_limit(max_calls=10, time_window=60)
async def webhook():
    start_time = time.time()
    try:
        update = request.get_json()
        
        if 'message' not in update:
            return 'OK'
            
        message = update['message']
        chat_id = message['chat']['id']
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

        user_language = read_value_by_chat_id(
            table_name="vyapari_user",
            chat_id=chat_id,
            column_name="language"
        )

        company_details = read_value_by_chat_id(
            table_name="vyapari_user",
            chat_id=chat_id,
            column_name="Company Details"
        )

        # ------------------------------------------------------------------
        # 1.  Look up user; insert if not found
        # ------------------------------------------------------------------
        user_record = read_user(chat_id)           # returns None if absent
        if not user_record:
            write_user(chat_id, user_name)         # create with defaults
        else:
            update_last_used_date(chat_id, user_name)  # refresh timestamp / name

        # 2. Log the message and trim to last 5
        log_message(chat_id, text, message_ts)

        # 3. Fetch last 5 and compose a single variable for the bot
        last_msgs   = get_last_messages(chat_id)
        history     = "\n".join(
            f"[{m['message_date']}] {m['message_text']}" for m in last_msgs
        )

        # 4. Pass `history` into your bot’s processing pipeline as needed
        print("Conversation history passed to bot:\n", history)

        global VYAPARI_PROMPT, RECORD_PROMPT, INVOICE_PROMPT, REPORT_PROMPT
        Vyapari_PROMPT = VYAPARI_PROMPT
        Record_PROMPT = RECORD_PROMPT
        Invoice_PROMPT = INVOICE_PROMPT
        Report_PROMPT = REPORT_PROMPT

        # Prepare context
        current_date = datetime.now().strftime('%Y-%m-%d')
        master_context = f"\nChat ID: {chat_id}\nHistory: {history}\n Today's Date: {current_date}"
        child_context = f"\nChat ID: {chat_id}\n Today's Date: {current_date}\n User Language: {user_language}"

        Vyapari_PROMPT += master_context
        Record_PROMPT  += child_context
        Invoice_PROMPT += child_context
        Report_PROMPT  += child_context

        Invoice_PROMPT += f"\nCompany Details are: {company_details}"

        Database_Agent = Agent(
                name="Transaction Recorder", 
                instructions=Record_PROMPT, 
                model=model1,
                tools=[write_transaction])

        Invoice_Agent = Agent(
                name="Invoice Generator", 
                instructions=Invoice_PROMPT, 
                model=model1,
                tools=[handle_invoice_request],
                handoffs=[Database_Agent])
                
        Report_Agent = Agent(
                name="Report Generator", 
                instructions=Report_PROMPT, 
                model=model2,
                tools=[read_transactions, download_transactions_csv])

        Vyapari_Agent = Agent(
                name="Vyapari", 
                instructions=Vyapari_PROMPT, 
                model=model2,
                handoffs=[Invoice_Agent, Report_Agent])

        print("Created All Agents")
        with trace("Vyapari Agent"):
            response = await asyncio.wait_for(
                    Runner.run(Vyapari_Agent, text), 
                    timeout=120 # 2 minute timeout
                )

        send_telegram_message(chat_id, response.final_output)
        bot_text = "Assitant: "
        bot_text += response.final_output
        log_message(chat_id, bot_text, int(datetime.now(timezone.utc).timestamp()))

        return 'OK'

    except Exception as e:
        logger.error(f"Error processing webhook: {str(e)}")
        return 'Error', 500


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
    return jsonify({"status": "healthy"}), 200

if __name__ == '__main__':
    # Validate environment variables
    if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
        logger.error("Missing required environment variables")
        exit(1)
    
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000))) 