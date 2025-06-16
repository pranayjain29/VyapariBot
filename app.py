import os
import json
import logging
from flask import Flask, request, jsonify
import requests
from dotenv import load_dotenv
from openai import  AsyncOpenAI
from typing import List, Dict, Union

from tools_util import *
import re
from datetime import datetime
import asyncio
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

app = Flask(__name__)


# Configuration
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Configure Gemini
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
gemini_client = AsyncOpenAI(base_url=GEMINI_BASE_URL, api_key=GEMINI_API_KEY)
model = OpenAIChatCompletionsModel(model="gemini-2.5-flash-preview-05-20", openai_client=gemini_client)

# Vyapari character system prompt
VYAPARI_PROMPT = """You are a seasoned Indian businessman (Vyapari) an AI Chat bot with the following characteristics:
PERSONALITY & COMMUNICATION:
- **CRITICAL LANGUAGE RULE**: You MUST respond in the EXACT same language as the user's input
- **Character Traits**: Direct, honest, practical with occasional humor
- **Business Wisdom**: Include relevant Indian business proverbs/phrases when appropriate

DELEGATION:

1. INVOICE/SALES REQUESTS ("Sold", "Transactions", "Invoice", "Recording transaction/sales",
etc) → Hand off to Invoice_Agent
"
2. Report/Analytics ("Report", "Sales data", "Insights",
"Summaries/Performance Queries") → Hand off to Report_Agent 

3. General Chat → Handle directly
**Examples**: Greetings, business advice, general questions, casual conversation

DECISION FRAMEWORK:
Before responding, ask yourself:
1. "Does this involve recording/generating invoices?" → Invoice_Agent
2. "Does this need transaction data/reports?" → Report_Agent  
3. "Is this general business chat?" → Handle myself

Remember: You're the wise business advisor who knows when to delegate!
"""

INVOICE_PROMPT = """You are the INVOICE SPECIALIST of VYAPARI - expert in transaction processing and invoice generation.

PERSONALITY (Maintain Vyapari Character):
- **CRITICAL LANGUAGE RULE**: You MUST respond in the EXACT same language as the user's input

DATA EXTRACTION PROTOCOL:

### REQUIRED FIELDS:
1. **item_name** (string): Product/service name
2. **quantity** (number): Must be numeric (convert "baara" → 12, "paach" → 5)
3. **price** (number): Price per unit in numbers only

### OPTIONAL FIELDS:
4. **date** (string): Format as YYYY-MM-DD (if missing, None)
5. **payment_method** (string): cash/credit/gpay/paytm/card (default: "cash")
6. **currency** (string): INR/USD/EUR (default: "INR")
7. **customer_name** (string): If mentioned
8. **customer_details** (dict): Phone, address if provided

PROCESSING WORKFLOW:

### STEP 1: DATA VALIDATION
- Validate Required Fields.

### STEP 2: INVOICE GENERATION
- Generates Invoices. Accept parallel lists for item name, quantity, and price.
- Use `handle_invoice_request` tool ONCE for all items
- Include ALL transaction items in single invoice

### STEP 3: TRANSACTION RECORDING  
- Call `write_transaction` for EACH item separately
- Confirm successful recording

**Tool Failures**: Retry once, then inform user clearly.

Remember: Accuracy is key - one mistake affects the entire business record!
"""

REPORT_PROMPT = """You are the ANALYTICS SPECIALIST of VYAPARI - expert in business intelligence and reporting.
You have to fetch user's business transaction using tool: read_transaction and extract insights.

## PERSONALITY (Maintain Vyapari Character):
- **CRITICAL LANGUAGE RULE**: You MUST respond in the EXACT same language as the user's input
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
        response = requests.post(
            f"{TELEGRAM_API_URL}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text
            }
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
    date: str
) -> str:
    """
    Generates Invoices.
    Accept parallel lists for item name, quantity, and price.
    """
    try:
        # Basic length check to avoid mis-aligned rows
        if not (len(item_names) == len(quantities) == len(prices)):
            raise ValueError("item_names, quantities, and prices must have the same length")

        # Build the structure expected by generate_invoice
        items = [
            {"name": n, "qty": q, "rate": p}
            for n, q, p in zip(item_names, quantities, prices)
        ]

        # Call the updated invoice generator
        invoice_file, invoice_number = generate_invoice(
            items=items,
            date=date
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
 

@app.route('/webhook', methods=['POST'])
async def webhook():
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
        print(chat_id)
        print(user_name)

        # ------------------------------------------------------------------
        # 1.  Look up user; insert if not found
        # ------------------------------------------------------------------
        user_record = read_user(chat_id)           # returns None if absent
        if not user_record:
            write_user(chat_id, user_name)         # create with defaults
        else:
            update_last_used_date(chat_id, user_name)  # refresh timestamp / name

        text = message.get('text', '')

        global VYAPARI_PROMPT, INVOICE_PROMPT, REPORT_PROMPT
        Vyapari_PROMPT = VYAPARI_PROMPT
        Invoice_PROMPT = INVOICE_PROMPT
        Report_PROMPT = REPORT_PROMPT

        Vyapari_PROMPT += f"Chat id is: {chat_id}"
        Invoice_PROMPT += f"Chat id is: {chat_id}"
        Report_PROMPT += f"Chat id is: {chat_id}"

        Invoice_Agent = Agent(
                name="Invoice Generator", 
                instructions=Invoice_PROMPT, 
                model=model,
                tools=[handle_invoice_request, write_transaction])

        Report_Agent = Agent(
                name="Report Generator", 
                instructions=Report_PROMPT, 
                model=model,
                tools=[read_transactions])

        Vyapari_Agent = Agent(
                name="Vyapari", 
                instructions=Vyapari_PROMPT, 
                model=model,
                handoffs=[Invoice_Agent, Report_Agent])

        print("Created All Agents")
        with trace("Vyapari Agent"):
            response = await Runner.run(Vyapari_Agent, text)

        send_telegram_message(chat_id, response.final_output)
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