from openai import AsyncOpenAI
from typing import Optional, Tuple
import os
from dotenv import load_dotenv
import logging

load_dotenv()
logger = logging.getLogger(__name__)

class InvoiceAgent:
    def __init__(self):
        self.gemini_client = AsyncOpenAI(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key=os.getenv('GEMINI_API_KEY')
        )
        
    async def analyze_message(self, text: str) -> Tuple[bool, Optional[dict]]:
        """
        Analyze the message to determine if it's an invoice request and extract details
        Returns: (is_invoice, details)
        """
        print(f"\nAnalyzing message: {text}")
        
        prompt = f"""You are an invoice detection system. Analyze if the following message contains sales or purchase details that should be converted into an invoice.

Message: {text}

If the message contains sales/purchase details, extract the following information in JSON format:
- item_name: The name of the item/product
- quantity: The number of items (must be a number)
- price: The price per item (must be a number)
- date: The date of the transaction (if not specified, use today's date)

Examples of valid invoice messages:
1. "Sold 2 sarees for 1500 each"
2. "Bought 5 kg rice at 60 per kg"
3. "Sale: 3 shirts, 800 each"
4. "Purchase of 10 notebooks for 50 rupees each"

Respond with JSON only if it's an invoice request, otherwise respond with 'not_invoice'.
Example JSON format:
{{
    "item_name": "blue saree",
    "quantity": 1,
    "price": 1999,
    "date": "10th june 2025"
}}
"""
        
        try:
            print("\nSending request to Gemini model...")
            response = await self.gemini_client.chat.completions.create(
                model="gemini-2.5-flash-preview-05-20",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1
            )
            
            result = response.choices[0].message.content.strip()
            print(f"\nModel response: {result}")
            logger.info(f"Agent response: {result}")
            
            if result == "not_invoice":
                print("Not an invoice request")
                return False, None
                
            import json
            details = json.loads(result)
            print(f"\nParsed details: {details}")
            
            # Validate the extracted details
            if not all(key in details for key in ['item_name', 'quantity', 'price']):
                print("Missing required fields in invoice details")
                logger.error("Missing required fields in invoice details")
                return False, None
                
            # Ensure quantity and price are numbers
            try:
                details['quantity'] = int(details['quantity'])
                details['price'] = int(details['price'])
                print(f"Validated numbers - quantity: {details['quantity']}, price: {details['price']}")
            except (ValueError, TypeError):
                print("Invalid quantity or price format")
                logger.error("Invalid quantity or price format")
                return False, None
                
            # Set default date if not provided
            if 'date' not in details:
                from datetime import datetime
                details['date'] = datetime.now().strftime("%dth %B %Y")
                print(f"Added default date: {details['date']}")
                
            print("\nSuccessfully processed invoice request!")
            return True, details
            
        except Exception as e:
            print(f"\nError analyzing message: {str(e)}")
            logger.error(f"Error analyzing message: {str(e)}")
            return False, None 