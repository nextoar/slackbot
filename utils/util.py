import re
from sqlalchemy.orm import Session
from db.models import Conversation
from sqlalchemy import desc
import logging
from typing import Dict, Tuple, List, Optional

logger = logging.getLogger(__name__)

# Constants for regex patterns
SLACK_USER_MENTION_PATTERN = r'<@[A-Z0-9]+>'
THINK_TAG_PATTERN = r'<think>(.*?)</think>'
BOLD_TEXT_PATTERN = r'\*\*(.*?)\*\*'

def clean_message(text: str) -> str:
    """Remove user mentions and clean the message text.
    
    Args:
        text (str): The input message text containing Slack mentions
        
    Returns:
        str: Cleaned message text with mentions removed
    """
    if not isinstance(text, str):
        logger.warning(f"Invalid input type for clean_message: {type(text)}")
        return str(text)
        
    # Remove user mentions in the format <@U...>
    cleaned_text = re.sub(SLACK_USER_MENTION_PATTERN, '', text)
    # Remove any leading/trailing whitespace
    cleaned_text = cleaned_text.strip()
    return cleaned_text

def extract_think_and_answer(response_text: str) -> Tuple[Optional[List[str]], str]:
    """Extract the thinking part and answer from the response text.
    
    Args:
        response_text (str): The response text containing <think> tags and answer
        
    Returns:
        Tuple[Optional[List[str]], str]: A tuple containing:
            - List of thinking parts (or None if no thinking parts found)
            - The answer text
    """
    if not isinstance(response_text, str):
        logger.warning(f"Invalid input type for extract_think_and_answer: {type(response_text)}")
        return None, str(response_text)
        
    try:
        # Find all content between <think> tags
        think_matches = re.finditer(THINK_TAG_PATTERN, response_text, re.DOTALL)
        thinking_parts = [match.group(1).strip() for match in think_matches]
        
        # Get everything after the last </think> tag
        think_end = response_text.rfind('</think>')
        if think_end == -1:
            return None, response_text.strip()
        
        answer = response_text[think_end + 8:].strip()
        return thinking_parts, answer
    except Exception as e:
        logger.error(f"Error extracting think and answer: {str(e)}")
        return None, response_text.strip()
    
def chunk_text(text: str, max_length: int = 2900) -> List[str]:
    """Split text into chunks that fit within Slack's character limit.
    Ensures splits occur at word boundaries for better readability.
    
    Args:
        text (str): Text to split
        max_length (int): Maximum length for each chunk
        
    Returns:
        List[str]: List of text chunks
    """
    chunks = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break
        
        # Find the last complete sentence within max_length
        chunk_end = max_length
        last_period = text[:max_length].rfind('.')
        last_newline = text[:max_length].rfind('\n')
        last_space = text[:max_length].rfind(' ')
        
        # Try to split at the most appropriate boundary
        if last_period != -1 and last_period > max_length * 0.7:  # Only use period if it's not too far back
            chunk_end = last_period + 1
        elif last_newline != -1 and last_newline > max_length * 0.7:  # Only use newline if it's not too far back
            chunk_end = last_newline + 1
        elif last_space != -1:  # Always better to split at a space than mid-word
            chunk_end = last_space + 1
            
        # If no good splitting point found, find the next space after max_length
        if chunk_end == max_length and len(text) > max_length:
            next_space = text.find(' ', max_length)
            if next_space != -1 and next_space - max_length < 100:  # Don't extend too far
                chunk_end = next_space + 1
        
        current_chunk = text[:chunk_end].strip()
        if current_chunk:  # Only add non-empty chunks
            chunks.append(current_chunk)
        text = text[chunk_end:].strip()
    
    return chunks

def format_slack_response(response_text: str) -> Dict:
    """Format the response with collapsible thinking section and proper text formatting.
    
    Args:
        response_text (str): The response text to format
        
    Returns:
        Dict: Formatted response with Slack blocks
    """
    if not isinstance(response_text, str):
        logger.warning(f"Invalid input type for format_slack_response: {type(response_text)}")
        return {"text": str(response_text)}
        
    try:
        thinking_parts, answer = extract_think_and_answer(response_text)
        
        blocks = []
        
        # Only add thinking process section if there are thinking parts
        if thinking_parts:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Thinking Process* :arrow_down:"
                }
            })
            
            # Split thinking parts into chunks if needed
            for part in thinking_parts:
                chunks = chunk_text(part)
                for chunk in chunks:
                    blocks.append({
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"```{chunk}```"
                        }
                    })
            
            blocks.append({
                "type": "divider"
            })
        
        # Format the answer text to handle bold formatting
        formatted_answer = re.sub(BOLD_TEXT_PATTERN, r'*\1*', answer)
        
        # Split answer into chunks if needed
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Answer:*"
            }
        })
        
        answer_chunks = chunk_text(formatted_answer)
        for chunk in answer_chunks:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": chunk
                }
            })
        
        return {"blocks": blocks}
    except Exception as e:
        logger.error(f"Error formatting Slack response: {str(e)}")
        return {"text": str(response_text)}

def get_recent_conversations(db: Session, channel_id: str, model_id: str, limit: int = 5) -> str:
    """Get recent conversations and format them as context.
    
    Args:
        db (Session): SQLAlchemy database session
        channel_id (str): The Slack channel ID to filter conversations
        model_id (str): The model ID to filter conversations
        limit (int, optional): Number of recent conversations to fetch. Defaults to 5.
        
    Returns:
        str: Formatted context string from recent conversations
    """
    try:
        # Get recent conversations ordered by timestamp, filtered by channel_id and model_id
        conversations = db.query(Conversation).filter(
            Conversation.channel_id == channel_id,
            Conversation.model_name == model_id
        ).order_by(desc(Conversation.created_at)).limit(limit).all()
        
        # Format conversations as context
        context_parts = []
        for conv in reversed(conversations):  # Reverse to get chronological order
            # Extract only the answer part from the outgoing message
            _, answer = extract_think_and_answer(conv.outgoing_message)
            context_parts.append(f"user: \"{conv.incoming_message}\"")
            context_parts.append(f"assistant: \"{answer}\"")
        
        # Join with newlines
        return "\n".join(context_parts)
    except Exception as e:
        logger.error(f"Error fetching recent conversations: {str(e)}")
        return ""