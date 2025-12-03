"""Pydantic models for API responses."""

from pydantic import BaseModel
from typing import List, Optional


class Holder(BaseModel):
    """Token holder with balance."""
    address: str
    balance: str  # Wei as string to preserve precision


class HoldersResponse(BaseModel):
    """Response model for /holders endpoint."""
    chain_id: int
    chain_name: str
    token_address: str
    holder_count: int
    last_indexed_block: int
    sync_in_progress: bool
    holders: List[Holder]


class ChainInfo(BaseModel):
    """Chain information."""
    chain_id: int
    chain_name: str
    token_address: str
    start_block: int
    is_active: bool


class ChainsResponse(BaseModel):
    """Response model for /chains endpoint."""
    chains: List[ChainInfo]


class HealthResponse(BaseModel):
    """Response model for /health endpoint."""
    status: str
    chains: List[ChainInfo]
    any_syncing: bool


class SyncStatus(BaseModel):
    """Indexer sync status for a chain."""
    chain_id: int
    chain_name: str
    last_indexed_block: int
    chain_head_block: int
    blocks_behind: int
    is_syncing: bool
    addresses_checked: int


class MultiChainSyncStatus(BaseModel):
    """Multi-chain sync status."""
    chains: List[SyncStatus]
