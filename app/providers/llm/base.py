from abc import ABC, abstractmethod

class LLMProvider(ABC):
    @abstractmethod
    async def chat(self, messages: list, context: str) -> str:
        pass
    
    @abstractmethod
    async def generate_suggestions(self, data: str) -> list:
        pass
