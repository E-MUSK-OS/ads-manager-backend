from app.providers.llm.base import LLMProvider
from app.config import settings
import anthropic

class ClaudeProvider(LLMProvider):
    def __init__(self):
        self.client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY) if settings.ANTHROPIC_API_KEY else None

    async def chat(self, messages: list, context: str) -> str:
        if not self.client:
            return "Mock AI response (Claude API key not set)"
        
        system = f"You are an AI assistant for Amazon Ads. Context: {context}"
        try:
            resp = await self.client.messages.create(
                model="claude-3-opus-20240229",
                max_tokens=1024,
                system=system,
                messages=messages
            )
            return resp.content[0].text
        except Exception as e:
            return f"Error calling Claude: {str(e)}"
    
    async def generate_suggestions(self, data: str) -> list:
        return []
