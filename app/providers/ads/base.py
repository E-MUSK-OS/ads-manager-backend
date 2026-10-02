from abc import ABC, abstractmethod

class AdsProvider(ABC):
    @abstractmethod
    async def generate_for_new_connection(self, ads_account_id: int):
        pass
    
    @abstractmethod
    async def sync_account_data(self, ads_account_id: int):
        pass
