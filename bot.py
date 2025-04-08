from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
import os
from dotenv import load_dotenv
import requests
from fastapi import FastAPI, Request, Depends
from fastapi.responses import JSONResponse
import json
import logging
import sys
from datetime import datetime
import re
from sqlalchemy.orm import Session
from sqlalchemy import desc
from db.database import engine, SessionLocal
from db.models import Base, Conversation
from utils.util import *

# Create database tables
Base.metadata.create_all(bind=engine)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

load_dotenv()
SLACK_TOKEN = os.getenv("SLACK_TOKEN", "")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
CHAT_ENDPOINT = os.getenv("CHAT_ENDPOINT", "")
MODEL_NAME = os.getenv("MODEL_NAME", "")
REQUEST_TIMEOUT = 120  # Timeout in seconds

# Debug logging for environment variables
logger.info(f"Loaded environment variables:")
logger.info(f"SLACK_TOKEN: {'*' * len(SLACK_TOKEN) if SLACK_TOKEN else 'Not set'}")
logger.info(f"SLACK_SIGNING_SECRET: {'*' * len(SLACK_SIGNING_SECRET) if SLACK_SIGNING_SECRET else 'Not set'}")
logger.info(f"CHAT_ENDPOINT: {CHAT_ENDPOINT}")
logger.info(f"MODEL_NAME: {MODEL_NAME}")

# Initialize the async app with your bot token and signing secret
app = AsyncApp(token=SLACK_TOKEN, signing_secret=SLACK_SIGNING_SECRET)
app_handler = AsyncSlackRequestHandler(app)

# Create FastAPI app
fastapi_app = FastAPI()

# Create a database session
db = SessionLocal()

# Listen to regular messages in channels and DMs
@app.event("message")
async def handle_message(event, say):
    # Extract the text from the message
    text = event.get("text", "")
    
    if text:
        # Clean the message
        cleaned_text = clean_message(text)
        logger.info(f"Echoing message: {cleaned_text}")
        try:
            # Store the conversation in database
            conversation = Conversation(
                user_id=event.get("user"),
                channel_id=event.get("channel"),
                message_id=event.get("ts"),
                incoming_message=cleaned_text,
                outgoing_message=cleaned_text,
                model_name="echo"
            )
            db.add(conversation)
            db.commit()
        except Exception as e:
            logger.error(f"Error storing conversation: {str(e)}")
            db.rollback()
        # Simply echo back the message
        await say(cleaned_text)

# Handle app mentions - send these to the chat endpoint
@app.event("app_mention")
async def handle_mention(event, say):
    text = event.get("text", "")
    if text:
        try:
            # Clean the message
            cleaned_text = clean_message(text)
            logger.info(f"Processing mention: {cleaned_text}")
            
            # Store the incoming message first
            try:
                conversation = Conversation(
                    user_id=event.get("user"),
                    channel_id=event.get("channel"),
                    message_id=event.get("ts"),
                    incoming_message=cleaned_text,
                    outgoing_message="",  # Empty for now, will be updated later
                    model_name=MODEL_NAME
                )
                db.add(conversation)
                db.commit()
                logger.info("Stored incoming message in database")
            except Exception as e:
                logger.error(f"Error storing incoming message: {str(e)}")
                db.rollback()
            
            # Get recent conversations for context
            context = get_recent_conversations(db, event.get("channel"), MODEL_NAME)
            logger.info(f"Context from recent conversations: {context}")
            
            # Prepare the prompt with context
            prompt = f"""You are an AI Coach, world-class semiconductor value chain expert with extensive and in-depth knowledge of semiconductor manufacturing. You need to help your team of users to become expert problem-solvers in this field by providing answers and reasoning to the questions which the user asks in the most accurate form by demonstrating your technical skills and depth. Be conversational, friendly and professional in your response.

                    Here is some context to the conversation, based on the previous conversations:
                    {context}

                    Please answer the below question now

                    ### Question: {cleaned_text}"""
            
            # Send message to external endpoint with correct format
            payload = {
                "model": MODEL_NAME,
                "prompt": prompt,
                "stream": False
            }
            headers = {
                "Content-Type": "application/json"
            }
            
            response = requests.post(
                CHAT_ENDPOINT, 
                json=payload, 
                headers=headers,
                timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            
            # Get the response from the endpoint
            response_data = response.json()
            response_text = response_data.get("response", "")
            logger.info(f"Response received from endpoint: {response_text}")
            
            try:
                # Update the existing conversation with the response
                conversation = db.query(Conversation).filter_by(message_id=event.get("ts")).first()
                if conversation:
                    conversation.outgoing_message = response_text
                    db.commit()
                    logger.info("Updated conversation with response in database")
                else:
                    logger.error("Could not find conversation to update")
            except Exception as e:
                logger.error(f"Error updating conversation with response: {str(e)}")
                db.rollback()
            
            # Format and send the response
            formatted_response = format_slack_response(response_text)
            await say(**formatted_response)
            
        except requests.exceptions.Timeout:
            logger.error(f"Request timed out after {REQUEST_TIMEOUT} seconds")
            await say("Sorry, the request took too long to process. Please try again.")
        except requests.exceptions.RequestException as e:
            logger.error(f"Error connecting to endpoint: {str(e)}")
            await say("Sorry, I'm having trouble connecting to the chat service right now.")

# Handle all Slack events
@fastapi_app.post("/slack/events")
async def slack_events(request: Request):
    # Get the raw request body and decode it
    body = await request.body()
    try:
        # Decode bytes to string and parse JSON
        body_str = body.decode('utf-8')
        data = json.loads(body_str)
        
        # Handle URL verification challenge
        if data.get("type") == "url_verification":
            logger.info("Handling URL verification challenge")
            return JSONResponse(content={"challenge": data["challenge"]})
        
        # Debug logging for request headers
        headers = dict(request.headers)
        logger.info(f"Request headers: {headers}")
        logger.info(f"Signing secret length: {len(SLACK_SIGNING_SECRET)}")
        
        # Handle all other events
        return await app_handler.handle(request)
        
    except json.JSONDecodeError as e:
        logger.error(f"Error decoding JSON: {str(e)}")
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})
    except Exception as e:
        logger.error(f"Error processing request: {str(e)}")
        return JSONResponse(status_code=500, content={"error": "Internal server error"})

# Log startup
logger.info("Slack bot application initialized") 