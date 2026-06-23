import requests
from typing import Dict
from streamlit_app.config import settings


class TrustAPIClient:
    def __init__(self):
        self.base_url = settings.API_BASE_URL

    def get_trust_score(self, agent_id: int) -> Dict:
        url = f"{self.base_url}/v1/trust-score"
        params = {"agent_id": agent_id}

        try:
            response = requests.get(url, params=params, timeout=5)

            if response.status_code != 200:
                raise Exception(f"API Error: {response.status_code}")

            return response.json()

        except requests.exceptions.RequestException as e:
            raise Exception(f"Connection error: {str(e)}")