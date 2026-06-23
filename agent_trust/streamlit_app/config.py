from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    API_BASE_URL: str = "http://127.0.0.1:8000"

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore"   # 🔥 THIS FIXES YOUR ERROR
    )


settings = Settings()