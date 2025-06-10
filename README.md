# Telegram Bot with Gemini Integration

This is a Flask-based Telegram bot that uses Google's Gemini AI model to respond to messages. The bot forwards incoming messages to Gemini and sends the responses back to the user.

## Prerequisites

- Python 3.8 or higher
- A Telegram bot token (get from [@BotFather](https://t.me/botfather))
- A Gemini API key (get from [Google AI Studio](https://makersuite.google.com/app/apikey))

## Setup

1. Clone this repository
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Create a `.env` file based on `.env.example`:
   ```bash
   cp .env.example .env
   ```

4. Edit the `.env` file and add your:
   - Telegram Bot Token
   - Gemini API Key

## Running the Bot

1. Start the Flask application:
   ```bash
   python app.py
   ```

2. Set up the webhook with Telegram:
   ```bash
   curl -F "url=https://your-domain.com/webhook" https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook
   ```
   Replace `your-domain.com` with your actual domain and `<YOUR_BOT_TOKEN>` with your Telegram bot token.

## Production Deployment

For production deployment, it's recommended to:

1. Use a proper WSGI server like Gunicorn:
   ```bash
   gunicorn -w 4 -b 0.0.0.0:5000 app:app
   ```

2. Set up a reverse proxy (like Nginx) with SSL
3. Use a process manager (like Supervisor or systemd)
4. Set up proper logging and monitoring

## Security Considerations

- Keep your API keys secure and never commit them to version control
- Use HTTPS for your webhook URL
- Implement rate limiting for production use
- Monitor your API usage and costs

## Error Handling

The bot includes error handling for:
- Network failures
- Invalid payloads
- Missing environment variables
- API errors

## Health Check

The application includes a health check endpoint at `/health` that returns a 200 status code when the service is running properly.

## License

MIT License 