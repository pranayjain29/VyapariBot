import os
import requests
from dotenv import load_dotenv

load_dotenv()

def update_webhook():
    bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
    railway_url = os.getenv('RAILWAY_URL')  # You'll set this in Railway's environment variables
    
    if not bot_token or not railway_url:
        print("Error: Missing environment variables")
        return
    
    webhook_url = f"{railway_url}/webhook"
    api_url = f"https://api.telegram.org/bot{bot_token}/setWebhook"
    
    try:
        response = requests.get(api_url, params={'url': webhook_url})
        if response.status_code == 200:
            print(f"Webhook updated successfully to: {webhook_url}")
        else:
            print(f"Failed to update webhook. Status code: {response.status_code}")
            print(f"Response: {response.text}")
    except Exception as e:
        print(f"Error updating webhook: {str(e)}")

if __name__ == "__main__":
    update_webhook() 