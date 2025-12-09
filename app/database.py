"""SQLite database management for the token indexer."""

import aiosqlite
import os
from typing import List, Tuple, Optional, Set, Dict
from contextlib import asynccontextmanager

from app.config import get_settings

# Zero address to exclude
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


class Database:
    """Async SQLite database manager."""
    
    def __init__(self, db_path: Optional[str] = None):
        settings = get_settings()
        self.db_path = db_path or settings.database_path
        self._connection: Optional[aiosqlite.Connection] = None
    
    async def connect(self):
        """Initialize database connection and create tables."""
        # Ensure directory exists
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        
        self._connection = await aiosqlite.connect(self.db_path)
        self._connection.row_factory = aiosqlite.Row
        
        # Enable WAL mode for better concurrent read/write performance
        await self._connection.execute("PRAGMA journal_mode=WAL")
        await self._connection.execute("PRAGMA synchronous=NORMAL")
        await self._connection.execute("PRAGMA cache_size=-64000")  # 64MB cache
        await self._connection.execute("PRAGMA temp_store=MEMORY")
        
        await self._create_tables()
    
    async def close(self):
        """Close database connection."""
        if self._connection:
            await self._connection.close()
            self._connection = None
    
    @asynccontextmanager
    async def get_connection(self):
        """Get database connection context manager."""
        if not self._connection:
            await self.connect()
        yield self._connection
    
    async def _create_tables(self):
        """Create database tables if they don't exist."""
        async with self.get_connection() as conn:
            # Chains table - stores chain configurations
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS chains (
                    chain_id INTEGER PRIMARY KEY,
                    chain_name TEXT NOT NULL,
                    rpc_url TEXT NOT NULL,
                    token_address TEXT NOT NULL,
                    start_block INTEGER NOT NULL,
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Transfers table - now includes chain_id
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS transfers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chain_id INTEGER NOT NULL,
                    block_number INTEGER NOT NULL,
                    tx_hash TEXT NOT NULL,
                    log_index INTEGER NOT NULL,
                    from_address TEXT NOT NULL,
                    to_address TEXT NOT NULL,
                    value TEXT NOT NULL,
                    UNIQUE(chain_id, tx_hash, log_index),
                    FOREIGN KEY (chain_id) REFERENCES chains(chain_id)
                )
            """)
            
            # Create indexes for fast queries
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_transfers_chain 
                ON transfers(chain_id)
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_transfers_from 
                ON transfers(chain_id, from_address)
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_transfers_to 
                ON transfers(chain_id, to_address)
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_transfers_block 
                ON transfers(chain_id, block_number)
            """)
            
            # Address types table - caches whether address is EOA or contract (per chain)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS address_types (
                    chain_id INTEGER NOT NULL,
                    address TEXT NOT NULL,
                    is_eoa INTEGER NOT NULL,
                    PRIMARY KEY (chain_id, address),
                    FOREIGN KEY (chain_id) REFERENCES chains(chain_id)
                )
            """)
            
            # Pre-computed balances table for fast queries (per chain)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS balances (
                    chain_id INTEGER NOT NULL,
                    address TEXT NOT NULL,
                    balance TEXT NOT NULL,
                    PRIMARY KEY (chain_id, address),
                    FOREIGN KEY (chain_id) REFERENCES chains(chain_id)
                )
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_balances_chain_balance 
                ON balances(chain_id, CAST(balance AS INTEGER) DESC)
            """)
            
            # Sync state table - per chain
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_state (
                    chain_id INTEGER PRIMARY KEY,
                    last_indexed_block INTEGER NOT NULL,
                    is_syncing INTEGER DEFAULT 0,
                    last_balance_update_block INTEGER DEFAULT 0,
                    FOREIGN KEY (chain_id) REFERENCES chains(chain_id)
                )
            """)
            
            await conn.commit()
    
    # Chain management methods
    async def register_chain(self, chain_id: int, chain_name: str, rpc_url: str, 
                            token_address: str, start_block: int):
        """Register a new chain configuration."""
        async with self.get_connection() as conn:
            await conn.execute("""
                INSERT OR REPLACE INTO chains 
                (chain_id, chain_name, rpc_url, token_address, start_block, is_active)
                VALUES (?, ?, ?, ?, ?, 1)
            """, (chain_id, chain_name, rpc_url, token_address, start_block))
            
            # Initialize sync state for this chain
            await conn.execute("""
                INSERT OR IGNORE INTO sync_state (chain_id, last_indexed_block, is_syncing)
                VALUES (?, ?, 0)
            """, (chain_id, start_block - 1))
            
            await conn.commit()
    
    async def get_all_chains(self) -> List[Dict]:
        """Get all registered chains."""
        async with self.get_connection() as conn:
            cursor = await conn.execute("""
                SELECT chain_id, chain_name, rpc_url, token_address, start_block, is_active
                FROM chains
                WHERE is_active = 1
            """)
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    
    async def get_chain_config(self, chain_id: int) -> Optional[Dict]:
        """Get configuration for a specific chain."""
        async with self.get_connection() as conn:
            cursor = await conn.execute("""
                SELECT chain_id, chain_name, rpc_url, token_address, start_block, is_active
                FROM chains
                WHERE chain_id = ? AND is_active = 1
            """, (chain_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None
    
    # Transfer methods (now chain-aware)
    async def insert_transfers(self, chain_id: int, transfers: List[Tuple]):
        """
        Batch insert transfer events.
        
        Args:
            chain_id: Chain ID
            transfers: List of tuples (block_number, tx_hash, log_index, from_addr, to_addr, value)
        """
        async with self.get_connection() as conn:
            await conn.executemany("""
                INSERT OR IGNORE INTO transfers 
                (chain_id, block_number, tx_hash, log_index, from_address, to_address, value)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, [(chain_id, *t) for t in transfers])
            await conn.commit()
    
    # Sync state methods (now chain-aware)
    async def get_last_indexed_block(self, chain_id: int) -> int:
        """Get the last indexed block number for a chain."""
        async with self.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT last_indexed_block FROM sync_state WHERE chain_id = ?",
                (chain_id,)
            )
            row = await cursor.fetchone()
            return row["last_indexed_block"] if row else 0
    
    async def update_last_indexed_block(self, chain_id: int, block_number: int):
        """Update the last indexed block number for a chain."""
        async with self.get_connection() as conn:
            await conn.execute(
                "UPDATE sync_state SET last_indexed_block = ? WHERE chain_id = ?",
                (block_number, chain_id)
            )
            await conn.commit()
    
    async def set_syncing(self, chain_id: int, is_syncing: bool):
        """Set the syncing status for a chain."""
        async with self.get_connection() as conn:
            await conn.execute(
                "UPDATE sync_state SET is_syncing = ? WHERE chain_id = ?",
                (1 if is_syncing else 0, chain_id)
            )
            await conn.commit()
    
    async def is_syncing(self, chain_id: int) -> bool:
        """Check if indexer is currently syncing for a chain."""
        async with self.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT is_syncing FROM sync_state WHERE chain_id = ?",
                (chain_id,)
            )
            row = await cursor.fetchone()
            return bool(row["is_syncing"]) if row else False
    
    async def is_any_syncing(self) -> bool:
        """Check if any chain is currently syncing."""
        async with self.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) as count FROM sync_state WHERE is_syncing = 1"
            )
            row = await cursor.fetchone()
            return (row["count"] if row else 0) > 0
    
    # Address type methods (now chain-aware)
    async def get_all_unique_addresses(self, chain_id: int) -> Set[str]:
        """Get all unique addresses from transfers for a chain."""
        async with self.get_connection() as conn:
            cursor = await conn.execute("""
                SELECT DISTINCT address FROM (
                    SELECT from_address as address FROM transfers WHERE chain_id = ?
                    UNION
                    SELECT to_address as address FROM transfers WHERE chain_id = ?
                )
            """, (chain_id, chain_id))
            rows = await cursor.fetchall()
            return {row["address"] for row in rows}
    
    async def get_unchecked_addresses(self, chain_id: int) -> List[str]:
        """Get addresses that haven't been checked for EOA status for a chain."""
        async with self.get_connection() as conn:
            cursor = await conn.execute("""
                SELECT DISTINCT address FROM (
                    SELECT from_address as address FROM transfers WHERE chain_id = ?
                    UNION
                    SELECT to_address as address FROM transfers WHERE chain_id = ?
                ) 
                WHERE address NOT IN (
                    SELECT address FROM address_types WHERE chain_id = ?
                )
                AND address != ?
            """, (chain_id, chain_id, chain_id, ZERO_ADDRESS))
            rows = await cursor.fetchall()
            return [row["address"] for row in rows]
    
    async def set_address_type(self, chain_id: int, address: str, is_eoa: bool):
        """Set whether an address is an EOA for a chain."""
        async with self.get_connection() as conn:
            await conn.execute("""
                INSERT OR REPLACE INTO address_types (chain_id, address, is_eoa)
                VALUES (?, ?, ?)
            """, (chain_id, address, 1 if is_eoa else 0))
            await conn.commit()
    
    async def batch_set_address_types(self, chain_id: int, address_types: List[Tuple[str, bool]]):
        """Batch set address types for a chain."""
        async with self.get_connection() as conn:
            await conn.executemany("""
                INSERT OR REPLACE INTO address_types (chain_id, address, is_eoa)
                VALUES (?, ?, ?)
            """, [(chain_id, addr, 1 if is_eoa else 0) for addr, is_eoa in address_types])
            await conn.commit()
    
    # Balance methods (now chain-aware)
    async def update_balances_from_transfers(self, chain_id: int, transfers: List[Tuple]):
        """
        Incrementally update balances table from new transfers.
        
        Args:
            chain_id: Chain ID
            transfers: List of tuples (block_number, tx_hash, log_index, from_addr, to_addr, value)
        """
        if not transfers:
            return
        
        # Collect balance changes
        balance_changes: Dict[str, int] = {}
        
        for _, _, _, from_addr, to_addr, value in transfers:
            value_int = int(value)
            
            # Skip zero address
            if from_addr != ZERO_ADDRESS:
                balance_changes[from_addr] = balance_changes.get(from_addr, 0) - value_int
            if to_addr != ZERO_ADDRESS:
                balance_changes[to_addr] = balance_changes.get(to_addr, 0) + value_int
        
        async with self.get_connection() as conn:
            for address, change in balance_changes.items():
                # Get current balance for this chain
                cursor = await conn.execute(
                    "SELECT balance FROM balances WHERE chain_id = ? AND address = ?",
                    (chain_id, address)
                )
                row = await cursor.fetchone()
                current = int(row["balance"]) if row else 0
                new_balance = current + change
                
                if new_balance > 0:
                    await conn.execute("""
                        INSERT OR REPLACE INTO balances (chain_id, address, balance)
                        VALUES (?, ?, ?)
                    """, (chain_id, address, str(new_balance)))
                else:
                    # Remove if balance is zero or negative
                    await conn.execute(
                        "DELETE FROM balances WHERE chain_id = ? AND address = ?",
                        (chain_id, address)
                    )
            
            await conn.commit()
    
    async def rebuild_all_balances(self, chain_id: int):
        """Rebuild the entire balances table from transfers for a chain."""
        async with self.get_connection() as conn:
            # Clear existing balances for this chain
            await conn.execute("DELETE FROM balances WHERE chain_id = ?", (chain_id,))
            
            # Rebuild from transfers
            await conn.execute("""
                INSERT INTO balances (chain_id, address, balance)
                WITH incoming AS (
                    SELECT to_address as address, SUM(CAST(value AS INTEGER)) as total_in
                    FROM transfers
                    WHERE chain_id = ? AND to_address != ?
                    GROUP BY to_address
                ),
                outgoing AS (
                    SELECT from_address as address, SUM(CAST(value AS INTEGER)) as total_out
                    FROM transfers
                    WHERE chain_id = ? AND from_address != ?
                    GROUP BY from_address
                ),
                computed AS (
                    SELECT 
                        COALESCE(i.address, o.address) as address,
                        COALESCE(i.total_in, 0) - COALESCE(o.total_out, 0) as balance
                    FROM incoming i
                    FULL OUTER JOIN outgoing o ON i.address = o.address
                )
                SELECT ?, address, CAST(balance AS TEXT)
                FROM computed
                WHERE balance > 0
            """, (chain_id, ZERO_ADDRESS, chain_id, ZERO_ADDRESS, chain_id))
            
            await conn.commit()
    
    async def get_holders_with_balances(self, chain_id: int, eoa_only: bool = True) -> List[Tuple[str, str]]:
        """
        Get holders from pre-computed balances table for a chain.
        
        Args:
            chain_id: Chain ID
            eoa_only: If True, only return EOA addresses (exclude contracts)
        
        Returns:
            List of tuples (address, balance_string) for addresses with balance > 0
        """
        async with self.get_connection() as conn:
            if eoa_only:
                cursor = await conn.execute("""
                    SELECT b.address, b.balance
                    FROM balances b
                    INNER JOIN address_types at ON b.chain_id = at.chain_id AND b.address = at.address
                    WHERE b.chain_id = ? AND at.is_eoa = 1
                    ORDER BY CAST(b.balance AS INTEGER) DESC
                """, (chain_id,))
            else:
                cursor = await conn.execute("""
                    SELECT address, balance
                    FROM balances
                    WHERE chain_id = ?
                    ORDER BY CAST(balance AS INTEGER) DESC
                """, (chain_id,))
            
            rows = await cursor.fetchall()
            return [(row["address"], row["balance"]) for row in rows]
    
    async def get_holder_count(self, chain_id: int, eoa_only: bool = True) -> int:
        """Get the count of holders with positive balance for a chain."""
        async with self.get_connection() as conn:
            if eoa_only:
                cursor = await conn.execute("""
                    SELECT COUNT(*) as count
                    FROM balances b
                    INNER JOIN address_types at ON b.chain_id = at.chain_id AND b.address = at.address
                    WHERE b.chain_id = ? AND at.is_eoa = 1
                """, (chain_id,))
            else:
                cursor = await conn.execute("""
                    SELECT COUNT(*) as count FROM balances WHERE chain_id = ?
                """, (chain_id,))
            
            row = await cursor.fetchone()
            return row["count"] if row else 0
    
    async def get_transfer_count(self, chain_id: Optional[int] = None) -> int:
        """Get total number of indexed transfers (optionally for a specific chain)."""
        async with self.get_connection() as conn:
            if chain_id is not None:
                cursor = await conn.execute(
                    "SELECT COUNT(*) as count FROM transfers WHERE chain_id = ?",
                    (chain_id,)
                )
            else:
                cursor = await conn.execute("SELECT COUNT(*) as count FROM transfers")
            row = await cursor.fetchone()
            return row["count"] if row else 0
    
    async def get_checked_address_count(self, chain_id: int) -> int:
        """Get count of addresses that have been checked for EOA status for a chain."""
        async with self.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) as count FROM address_types WHERE chain_id = ?",
                (chain_id,)
            )
            row = await cursor.fetchone()
            return row["count"] if row else 0
    
    async def get_eoa_count(self, chain_id: int) -> int:
        """Get count of addresses that are EOAs for a chain."""
        async with self.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) as count FROM address_types WHERE chain_id = ? AND is_eoa = 1",
                (chain_id,)
            )
            row = await cursor.fetchone()
            return row["count"] if row else 0
    
    async def get_contract_addresses(self, chain_id: int) -> List[str]:
        """Get addresses that were marked as contracts (for smart wallet recheck)."""
        async with self.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT address FROM address_types WHERE chain_id = ? AND is_eoa = 0",
                (chain_id,)
            )
            rows = await cursor.fetchall()
            return [row["address"] for row in rows]
    
    async def get_contract_count(self, chain_id: int) -> int:
        """Get count of addresses marked as contracts for a chain."""
        async with self.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) as count FROM address_types WHERE chain_id = ? AND is_eoa = 0",
                (chain_id,)
            )
            row = await cursor.fetchone()
            return row["count"] if row else 0


# Global database instance
db = Database()
