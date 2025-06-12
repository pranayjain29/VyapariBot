import os
import json
import logging
from flask import Flask, request, jsonify
import requests
from dotenv import load_dotenv
from openai import  AsyncOpenAI

from tools_util import generate_invoice
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
VYAPARI_PROMPT = """You are a seasoned Indian businessman (Vyapari) with the following characteristics:
- You speak Hindi, English or a mix of Hindi and English (Hinglish) with a business-oriented mindset
- You use typical Indian business phrases and proverbs
- You're direct, honest, and sometimes use a bit of humor
- You use terms like "behenji", "bhai", "dost" when appropriate and occassionally.
Remember to maintain this character in all your responses while being helpful and informative.

Given the message/text of the user you have to identify if the text is a sales/purchase details or not.
If the message contains sales/purchase details, extract the following information:
- item_name: The name of the item/product
- quantity: The number of items (must be a number)
- price: The price per item (must be a number)
- date: The date of the transaction (if not specified, use today's date)

If it is a sales/purchase details, generate the invoice using handle_invoice_request tool, otherwise simply respond to the text.
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
        update = request.get_json()
        
        if 'message' not in update:
            return 'OK'
            
        message = update['message']
        chat_id = message['chat']['id']
        text = message.get('text', '')

        Vyapari_Agent = Agent(
                name="Vyapari", 
                instructions=VYAPARI_PROMPT, 
                model=model,
                tools=[handle_invoice_request])

        print("Created Vyapari Agent")
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