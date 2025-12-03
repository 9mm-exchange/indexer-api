"""Pydantic models for API responses."""

from pydantic import BaseModel
from typing import List


class Holder(BaseModel):
    """Token holder with balance."""
    address: str
    balance: str  # Wei as string to preserve precision


class HoldersResponse(BaseModel):
    """Response model for /holders endpoint."""
    token_address: str
    holder_count: int
    last_indexed_block: int
    sync_in_progress: bool
    holders: List[Holder]


class HealthResponse(BaseModel):
    """Response model for /health endpoint."""
    status: str
    last_indexed_block: int
    sync_in_progress: bool


class SyncStatus(BaseModel):
    """Indexer sync status."""
    last_indexed_block: int
    chain_head_block: int
    blocks_behind: int
    is_syncing: bool
    addresses_checked: int
