import os
import json
import logging
from flask import Flask, request, jsonify
import requests
from dotenv import load_dotenv
import google.generativeai as genai
from invoice_generator import InvoiceGenerator
from agents import InvoiceAgent
import re
from datetime import datetime
import asyncio

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
invoice_gen = InvoiceGenerator()
invoice_agent = InvoiceAgent()

# Configuration
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-flash-preview-05-20')

# Vyapari character system prompt
VYAPARI_PROMPT = """You are a seasoned Indian businessman (Vyapari) with the following characteristics:
- You speak Hindi, English or a mix of Hindi and English (Hinglish) with a business-oriented mindset
- You have 30+ years of experience in traditional Indian business
- You're known for your practical wisdom and street-smart business advice
- You use typical Indian business phrases and proverbs
- You're direct, honest, and sometimes use a bit of humor
- You often share real-world business examples from Indian context
- You're respectful but straightforward in your communication
- You use terms like "behenji", "bhai", "dost" when appropriate and occassionally.
- You're knowledgeable about both traditional and modern business practices
- You often end your advice with encouraging phrases like "Aap kar sakte hain", "Koi baat nahi, try karte raho"
- Answer in concise manner
Remember to maintain this character in all your responses while being helpful and informative."""

def extract_invoice_details(text):
    """Extract invoice details from text using regex"""
    # Pattern: "X item_name sold for rupees Y on Z"
    pattern = r"(\d+)\s+([^f]+?)\s+sold\s+for\s+rupees\s+(\d+)\s+on\s+(.+)"
    match = re.search(pattern, text.lower())
    
    if match:
        quantity = int(match.group(1))
        item_name = match.group(2).strip()
        price = int(match.group(3))
        date = match.group(4).strip()
        return quantity, item_name, price, date
    return None

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

def get_gemini_response(prompt):
    """Get response from Gemini API with Vyapari character."""
    try:
        # Combine the system prompt with the user's message
        full_prompt = f"{VYAPARI_PROMPT}\n\nUser message: {prompt}\n\nRespond as a Vyapari:"
        response = model.generate_content(full_prompt)
        return response.text
    except Exception as e:
        logger.error(f"Failed to get Gemini response: {str(e)}")
        return "Arre yaar, thoda technical problem ho gaya hai. Thodi der baad try karna."

@app.route('/webhook', methods=['POST'])
async def webhook():
    try:
        update = request.get_json()
        
        if 'message' not in update:
            return 'OK'
            
        message = update['message']
        chat_id = message['chat']['id']
        text = message.get('text', '')
        
        # Use agent to analyze message
        is_invoice, details = await invoice_agent.analyze_message(text)
        
        if is_invoice and details:
            # Generate invoice
            invoice_file = invoice_gen.generate_invoice(
                item_name=details['item_name'],
                quantity=details['quantity'],
                price=details['price'],
                date=details['date']
            )
            
            # Send invoice as document
            send_document(chat_id, invoice_file)
            
            # Cleanup
            invoice_gen.cleanup(invoice_file)
            
            return 'OK'
            
        # If not an invoice request, use Gemini
        response = model.generate_content(text)
        send_message(chat_id, response.text)
        
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