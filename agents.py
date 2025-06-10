from openai import AsyncOpenAI
from typing import Optional, Tuple
import os
from dotenv import load_dotenv

load_dotenv()

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
        prompt = f"""Analyze the following message and determine if it's a request to generate an invoice.
        If it is, extract the following details in JSON format:
        - item_name: The name of the item
        - quantity: The number of items
        - price: The price per item
        - date: The date of the transaction
        
        Message: {text}
        
        Respond with JSON only if it's an invoice request, otherwise respond with 'not_invoice'.
        Example JSON format:
        {{
            "item_name": "blue saree",
            "quantity": 1,
            "price": 1999,
            "date": "10th june 2025"
        }}
        """
        
        response = await self.gemini_client.chat.completions.create(
            model="gemini-2.5-flash-preview-05-20",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1
        )
        
        result = response.choices[0].message.content.strip()
        
        if result == "not_invoice":
            return False, None
            
        try:
            import json
            details = json.loads(result)
            return True, details
        except:
            return False, None 