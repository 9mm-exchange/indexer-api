"""Configuration management for the token indexer."""

from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import List, Dict, Optional
import json
import os


class ChainConfig(BaseSettings):
    """Configuration for a single chain."""
    chain_id: int
    chain_name: str
    rpc_url: str
    token_address: str
    start_block: int


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # Database
    database_path: str = "./data/indexer.db"
    
    # Indexer Settings
    batch_size: int = 10000
    
    # Chains configuration - can be provided as JSON string or via individual env vars
    # Format: JSON string with array of chain configs
    # Example: [{"chain_id": 1, "chain_name": "Ethereum", "rpc_url": "...", "token_address": "...", "start_block": 0}]
    chains_config: Optional[str] = None
    
    # Legacy single-chain support (for backward compatibility)
    rpc_url: Optional[str] = None
    token_address: Optional[str] = None
    start_block: Optional[int] = None
    chain_id: Optional[int] = None
    chain_name: Optional[str] = None
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
    
    def get_chains(self) -> List[ChainConfig]:
        """Parse and return chain configurations."""
        chains = []
        
        # Try to load from chains_config JSON string
        if self.chains_config:
            try:
                configs = json.loads(self.chains_config)
                for config in configs:
                    chains.append(ChainConfig(**config))
                return chains
            except (json.JSONDecodeError, TypeError) as e:
                raise ValueError(f"Invalid chains_config JSON: {e}")
        
        # Fallback: Try to load from individual environment variables
        # Check for CHAIN_IDS environment variable (comma-separated)
        chain_ids_str = os.getenv("CHAIN_IDS", "")
        if chain_ids_str:
            chain_ids = [int(x.strip()) for x in chain_ids_str.split(",") if x.strip()]
            for chain_id in chain_ids:
                chain_name = os.getenv(f"CHAIN_{chain_id}_NAME", f"Chain-{chain_id}")
                rpc_url = os.getenv(f"CHAIN_{chain_id}_RPC_URL")
                token_address = os.getenv(f"CHAIN_{chain_id}_TOKEN_ADDRESS")
                start_block = int(os.getenv(f"CHAIN_{chain_id}_START_BLOCK", "0"))
                
                if rpc_url and token_address:
                    chains.append(ChainConfig(
                        chain_id=chain_id,
                        chain_name=chain_name,
                        rpc_url=rpc_url,
                        token_address=token_address,
                        start_block=start_block
                    ))
        
        # Legacy support: single chain config (for backward compatibility)
        if not chains:
            rpc_url = self.rpc_url or os.getenv("RPC_URL", "https://rpc.pulsechain.com")
            token_address = self.token_address or os.getenv("TOKEN_ADDRESS", "0x7b39712Ef45F7dcED2bBDF11F3D5046bA61dA719")
            start_block = self.start_block if self.start_block is not None else int(os.getenv("START_BLOCK", "20326117"))
            chain_id = self.chain_id if self.chain_id is not None else int(os.getenv("CHAIN_ID", "369"))  # PulseChain chain ID
            
            chains.append(ChainConfig(
                chain_id=chain_id,
                chain_name=self.chain_name or os.getenv("CHAIN_NAME", "PulseChain"),
                rpc_url=rpc_url,
                token_address=token_address,
                start_block=start_block
            ))
        
        return chains


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
