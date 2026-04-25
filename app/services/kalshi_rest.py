import httpx


class KalshiRestClient:
    def __init__(self, base_url: str = "https://api.elections.kalshi.com/trade-api/v2") -> None:
        self.base_url = base_url

    def get_markets(self, limit: int = 100) -> dict:
        url = f"{self.base_url}/markets"
        params = {"limit": limit}

        with httpx.Client(timeout=10.0) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            return response.json()