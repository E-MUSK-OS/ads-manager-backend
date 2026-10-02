from app.providers.ads.base import AdsProvider

class AmazonAdsProvider(AdsProvider):
    async def generate_for_new_connection(self, ads_account_id: int):
        # TODO: implement once API access is approved (LWA OAuth token exchange, /v2/profiles)
        pass

    async def sync_account_data(self, ads_account_id: int):
        # TODO: implement /sp/campaigns, /reporting/reports
        pass
