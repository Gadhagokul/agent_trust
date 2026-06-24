# agent_trust\app\ml\trust_model.py
import logging
import os

import joblib
import numpy as np

from app.domain.errors import ModelUnavailableError

logger = logging.getLogger(__name__)

class TrustModelPredictor:
    """
    ML Inference Engine for Agent Trust Scoring.
    Loads a pre-trained model (e.g., RandomForestRegressor) to predict the overall score.
    """
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls, *args, **kwargs)
        return cls._instance

    def __init__(self, model_path: str = None):
        if not hasattr(self, 'initialized'):
            if model_path is None:
                base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                model_path = os.path.join(base_dir, "ml", "models", "trust_model_v1.pkl")

            self.model_path = model_path
            self.model = None
            self.initialized = True
            self._load_model()

    def _load_model(self):
        try:
            if os.path.exists(self.model_path):
                self.model = joblib.load(self.model_path)
                logger.info(f"Successfully loaded ML model from {self.model_path}")
            else:
                logger.warning(f"ML Model not found at {self.model_path}")
        except Exception as e:
            logger.error(f"Failed to load ML model from {self.model_path}: {e}")

    def predict(self, features: dict) -> float:
        if self.model is None:
            raise ModelUnavailableError(
                f"ML model not loaded from {self.model_path}. Cannot perform inference."
            )

        try:
            feature_vector = np.array([[
                features.get("eff_searches_7d", 0),
                features.get("bookings_7d", 0),
                features.get("eff_searches_30d", 0),
                features.get("bookings_30d", 0),
                features.get("unpaid_count", 0),
                features.get("unpaid_ratio", 0.0),
                features.get("delay_days", 0)
            ]])

            predicted_score = self.model.predict(feature_vector)[0]

            return max(5.0, min(float(predicted_score), 100.0))

        except Exception as e:
            logger.error(f"Error during ML inference: {e}")
            raise ModelUnavailableError(f"ML inference failed: {e}") from e
