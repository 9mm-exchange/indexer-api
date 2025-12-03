"""Configuration management for the token indexer."""

from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # RPC Configuration
    rpc_url: str = "https://rpc.pulsechain.com"
    
    # Token Configuration
    token_address: str = "0x7b39712Ef45F7dcED2bBDF11F3D5046bA61dA719"
    
    # Start Block
    start_block: int = 20326117
    
    # Database
    database_path: str = "./data/indexer.db"
    
    # Indexer Settings
    batch_size: int = 10000
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
