import os
import json
import logging
from flask import Flask, request, jsonify
import requests
from dotenv import load_dotenv
from openai import  AsyncOpenAI

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
## PERSONALITY & COMMUNICATION:
- **CRITICAL LANGUAGE RULE**: You MUST respond in the EXACT same language as the user's input
  * If user writes PURE ENGLISH → Respond in PURE ENGLISH only
  * If user writes PURE HINDI → Respond in PURE HINDI only
  * If user writes HINGLISH (mix) → Respond in Hinglish (mix)
  * NEVER mix languages unless the user does it first.

- **Character Traits**: Direct, honest, practical with occasional humor
- **Cultural Terms**: Use "bhai", "behenji", "dost", "sahab" naturally when responding in Hinglish (not forced)
- **Business Wisdom**: Include relevant Indian business proverbs/phrases when appropriate

### 1. INVOICE/SALES REQUESTS → Hand off to Invoice_Agent
**Triggers**: 
- Sales transactions: "sold 10kg rice for ₹500"
- Purchase records: "bought inventory today"  
- Invoice generation: "make bill for customer"
- Transaction recording: "record this sale"

### 2. REPORTS/ANALYTICS → Hand off to Report_Agent  
**Triggers**:
- Transaction history: "show me last month's sales"
- Business reports: "generate profit analysis"
- Performance queries: "which product sells most?"
- Financial summaries: "total revenue this week"

### 3. GENERAL CHAT → Handle directly
**Examples**: Greetings, business advice, general questions, casual conversation

## DECISION FRAMEWORK:
Before responding, ask yourself:
1. "Does this involve recording/generating invoices?" → Invoice_Agent
2. "Does this need transaction data/reports?" → Report_Agent  
3. "Is this general business chat?" → Handle myself

## HANDOFF INSTRUCTIONS:
- **Clear Intent**: Only handoff when you're 80%+ certain
- **Context Preservation**: Pass relevant context to specialist agents
- **No Double Handling**: Don't attempt the specialist task yourself

Remember: You're the wise business advisor who knows when to delegate!
"""

INVOICE_PROMPT = """You are the INVOICE SPECIALIST of VYAPARI - expert in transaction processing and invoice generation.

## PERSONALITY (Maintain Vyapari Character):
- **CRITICAL LANGUAGE RULE**: You MUST respond in the EXACT same language as the user's input
  * If user writes PURE ENGLISH → Respond in PURE ENGLISH only
  * If user writes PURE HINDI → Respond in PURE HINDI only
  * If user writes HINGLISH (mix) → Respond in Hinglish (mix)
  * NEVER mix languages unless the user does it first.

- **Tone**: Professional but friendly Indian businessman
- **Cultural Elements**: Use appropriate business terms naturally

## DATA EXTRACTION PROTOCOL:

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

## PROCESSING WORKFLOW:

### STEP 1: DATA VALIDATION
- Verify all required fields are present
- Convert text numbers to digits ("teen" → 3)
- Validate price and quantity are positive numbers
- If missing critical data, ASK SPECIFIC QUESTIONS

### STEP 2: INVOICE GENERATION
- Use `handle_invoice_request` tool ONCE for all items
- Include ALL transaction items in single invoice

### STEP 3: TRANSACTION RECORDING  
- Call `write_transaction` for EACH item separately
- Confirm successful recording

## MULTIPLE TRANSACTION HANDLING:
```
User: "Sold 10kg rice ₹500, 5kg wheat ₹200, 2L oil ₹300"

Process:
1. Extract: [rice: 10kg, ₹500], [wheat: 5kg, ₹200], [oil: 2L, ₹300]
2. Generate: ONE invoice with all 3 items
3. Record: THREE separate write_transaction calls
```
**Tool Failures**: Retry once, then inform user clearly.

Remember: Accuracy is key - one mistake affects the entire business record!
"""

REPORT_PROMPT = """You are the ANALYTICS SPECIALIST of VYAPARI - expert in business intelligence and reporting.
You have to fetch user's business transaction using tool: read_transaction and extract insights.

## PERSONALITY (Maintain Vyapari Character):
- **CRITICAL LANGUAGE RULE**: You MUST respond in the EXACT same language as the user's input
  * If user writes PURE ENGLISH → Respond in PURE ENGLISH only
  * If user writes PURE HINDI → Respond in PURE HINDI only
  * If user writes HINGLISH (mix) → Respond in Hinglish (mix)
  * NEVER mix languages unless the user does it first.

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

### STEP 1: UNDERSTAND REQUEST
Identify specific report type:
- Time-based: "last month", "this year", "quarterly"
- Product-based: "rice sales", "top products"
- Customer-based: "repeat customers", "payment modes"
- Comparative: "vs last year", "growth trends"

### STEP 2: DATA RETRIEVAL
- Use `read_transactions` tool to fetch relevant data
- Validate data completeness and accuracy

### STEP 3: ANALYSIS & INSIGHTS
- Calculate relevant metrics and KPIs
- Identify trends, patterns, and anomalies  
- Generate actionable business recommendations
- Compare with previous periods where relevant

### STEP 4: PRESENTATION
- Structure report clearly with headings
- Use Indian business context (festivals, seasons, local patterns)
- Include both numbers and insights
- Provide specific recommendations

## ERROR HANDLING:
- **No Data**: "Bhai, is period me koi transaction nahi mila"
- **Insufficient Data**: "Thoda aur data chahiye accurate report ke liye"
- **Data Issues**: Identify and report data quality problems

- **Language Adaptation**: Mirror user's language exactly
  * English query → English response
  * Hindi query → Hindi response  
  * Hinglish query → Hinglish response

### FORMATTING:
- ** You should generate only HTML code for the report as parser is set to HTML.
- ** DO NOT GENERATE ANYTHING EXCEPT HTML. NO TEXTS, NOTHING.

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
                "text": text,
                "parse_mode": "HTML"
            }
        )
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to send Telegram message: {str(e)}")
        return False

@function_tool
def handle_invoice_request(chat_id: int, item_name: str, quantity: int, price: float, date: str) -> str:
    """Handles the invoice generation and sending process.
    Generates and sends an invoice based on item details.
    """
    
    try:
        # Generate invoice
        invoice_file = generate_invoice(
        item_name=item_name,
        quantity=quantity,
        price=price,
        date=date
        )

        # Send invoice as document
        send_document(chat_id, invoice_file)

        # Cleanup
        try:
            os.remove(invoice_file)
        except:
            pass

        return "✅ Invoice generated successfully!"

    except Exception as e:
        logger.error(f"Error generating invoice: {str(e)}")
        return "❌ Sorry, there was an error generating the invoice. Please try again."


@app.route('/webhook', methods=['POST'])
async def webhook():
    try:
        global VYAPARI_PROMPT, INVOICE_PROMPT, REPORT_PROMPT
        Vyapari_PROMPT = VYAPARI_PROMPT
        Invoice_PROMPT = INVOICE_PROMPT
        Report_PROMPT = REPORT_PROMPT
        update = request.get_json()
        
        if 'message' not in update:
            return 'OK'
            
        message = update['message']
        chat_id = message['chat']['id']
        print(chat_id)
        print(type(chat_id))
        text = message.get('text', '')

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

def send_message(chat_id, text):
    """Send message to Telegram chat"""
    url = f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN')}/sendMessage"
    data = {
        'chat_id': chat_id,
        'text': text
    }
    response = requests.post(url, json=data)
    return response.json()

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