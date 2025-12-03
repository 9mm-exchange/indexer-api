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
            # Transfers table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS transfers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    block_number INTEGER NOT NULL,
                    tx_hash TEXT NOT NULL,
                    log_index INTEGER NOT NULL,
                    from_address TEXT NOT NULL,
                    to_address TEXT NOT NULL,
                    value TEXT NOT NULL,
                    UNIQUE(tx_hash, log_index)
                )
            """)
            
            # Create indexes for fast queries
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_transfers_from 
                ON transfers(from_address)
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_transfers_to 
                ON transfers(to_address)
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_transfers_block 
                ON transfers(block_number)
            """)
            
            # Address types table - caches whether address is EOA or contract
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS address_types (
                    address TEXT PRIMARY KEY,
                    is_eoa INTEGER NOT NULL
                )
            """)
            
            # Pre-computed balances table for fast queries
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS balances (
                    address TEXT PRIMARY KEY,
                    balance TEXT NOT NULL
                )
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_balances_balance 
                ON balances(CAST(balance AS INTEGER) DESC)
            """)
            
            # Sync state table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_indexed_block INTEGER NOT NULL,
                    is_syncing INTEGER DEFAULT 0,
                    last_balance_update_block INTEGER DEFAULT 0
                )
            """)
            
            # Initialize sync state if not exists
            await conn.execute("""
                INSERT OR IGNORE INTO sync_state (id, last_indexed_block, is_syncing, last_balance_update_block)
                VALUES (1, 0, 0, 0)
            """)
            
            await conn.commit()
    
    async def insert_transfers(self, transfers: List[Tuple]):
        """
        Batch insert transfer events.
        
        Args:
            transfers: List of tuples (block_number, tx_hash, log_index, from_addr, to_addr, value)
        """
        async with self.get_connection() as conn:
            await conn.executemany("""
                INSERT OR IGNORE INTO transfers 
                (block_number, tx_hash, log_index, from_address, to_address, value)
                VALUES (?, ?, ?, ?, ?, ?)
            """, transfers)
            await conn.commit()
    
    async def get_last_indexed_block(self) -> int:
        """Get the last indexed block number."""
        async with self.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT last_indexed_block FROM sync_state WHERE id = 1"
            )
            row = await cursor.fetchone()
            return row["last_indexed_block"] if row else 0
    
    async def update_last_indexed_block(self, block_number: int):
        """Update the last indexed block number."""
        async with self.get_connection() as conn:
            await conn.execute(
                "UPDATE sync_state SET last_indexed_block = ? WHERE id = 1",
                (block_number,)
            )
            await conn.commit()
    
    async def set_syncing(self, is_syncing: bool):
        """Set the syncing status."""
        async with self.get_connection() as conn:
            await conn.execute(
                "UPDATE sync_state SET is_syncing = ? WHERE id = 1",
                (1 if is_syncing else 0,)
            )
            await conn.commit()
    
    async def is_syncing(self) -> bool:
        """Check if indexer is currently syncing."""
        async with self.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT is_syncing FROM sync_state WHERE id = 1"
            )
            row = await cursor.fetchone()
            return bool(row["is_syncing"]) if row else False
    
    async def get_all_unique_addresses(self) -> Set[str]:
        """Get all unique addresses from transfers."""
        async with self.get_connection() as conn:
            cursor = await conn.execute("""
                SELECT DISTINCT address FROM (
                    SELECT from_address as address FROM transfers
                    UNION
                    SELECT to_address as address FROM transfers
                )
            """)
            rows = await cursor.fetchall()
            return {row["address"] for row in rows}
    
    async def get_unchecked_addresses(self) -> List[str]:
        """Get addresses that haven't been checked for EOA status."""
        async with self.get_connection() as conn:
            cursor = await conn.execute("""
                SELECT DISTINCT address FROM (
                    SELECT from_address as address FROM transfers
                    UNION
                    SELECT to_address as address FROM transfers
                ) 
                WHERE address NOT IN (SELECT address FROM address_types)
                AND address != ?
            """, (ZERO_ADDRESS,))
            rows = await cursor.fetchall()
            return [row["address"] for row in rows]
    
    async def set_address_type(self, address: str, is_eoa: bool):
        """Set whether an address is an EOA."""
        async with self.get_connection() as conn:
            await conn.execute("""
                INSERT OR REPLACE INTO address_types (address, is_eoa)
                VALUES (?, ?)
            """, (address, 1 if is_eoa else 0))
            await conn.commit()
    
    async def batch_set_address_types(self, address_types: List[Tuple[str, bool]]):
        """Batch set address types."""
        async with self.get_connection() as conn:
            await conn.executemany("""
                INSERT OR REPLACE INTO address_types (address, is_eoa)
                VALUES (?, ?)
            """, [(addr, 1 if is_eoa else 0) for addr, is_eoa in address_types])
            await conn.commit()
    
    async def update_balances_from_transfers(self, transfers: List[Tuple]):
        """
        Incrementally update balances table from new transfers.
        
        Args:
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
                # Get current balance
                cursor = await conn.execute(
                    "SELECT balance FROM balances WHERE address = ?",
                    (address,)
                )
                row = await cursor.fetchone()
                current = int(row["balance"]) if row else 0
                new_balance = current + change
                
                if new_balance > 0:
                    await conn.execute("""
                        INSERT OR REPLACE INTO balances (address, balance)
                        VALUES (?, ?)
                    """, (address, str(new_balance)))
                else:
                    # Remove if balance is zero or negative
                    await conn.execute(
                        "DELETE FROM balances WHERE address = ?",
                        (address,)
                    )
            
            await conn.commit()
    
    async def rebuild_all_balances(self):
        """Rebuild the entire balances table from transfers. Use for initial sync."""
        async with self.get_connection() as conn:
            # Clear existing balances
            await conn.execute("DELETE FROM balances")
            
            # Rebuild from transfers
            await conn.execute("""
                INSERT INTO balances (address, balance)
                WITH incoming AS (
                    SELECT to_address as address, SUM(CAST(value AS INTEGER)) as total_in
                    FROM transfers
                    WHERE to_address != ?
                    GROUP BY to_address
                ),
                outgoing AS (
                    SELECT from_address as address, SUM(CAST(value AS INTEGER)) as total_out
                    FROM transfers
                    WHERE from_address != ?
                    GROUP BY from_address
                ),
                computed AS (
                    SELECT 
                        COALESCE(i.address, o.address) as address,
                        COALESCE(i.total_in, 0) - COALESCE(o.total_out, 0) as balance
                    FROM incoming i
                    FULL OUTER JOIN outgoing o ON i.address = o.address
                )
                SELECT address, CAST(balance AS TEXT)
                FROM computed
                WHERE balance > 0
            """, (ZERO_ADDRESS, ZERO_ADDRESS))
            
            await conn.commit()
    
    async def get_holders_with_balances(self, eoa_only: bool = True) -> List[Tuple[str, str]]:
        """
        Get holders from pre-computed balances table.
        
        Args:
            eoa_only: If True, only return EOA addresses (exclude contracts)
        
        Returns:
            List of tuples (address, balance_string) for addresses with balance > 0
        """
        async with self.get_connection() as conn:
            if eoa_only:
                cursor = await conn.execute("""
                    SELECT b.address, b.balance
                    FROM balances b
                    INNER JOIN address_types at ON b.address = at.address
                    WHERE at.is_eoa = 1
                    ORDER BY CAST(b.balance AS INTEGER) DESC
                """)
            else:
                cursor = await conn.execute("""
                    SELECT address, balance
                    FROM balances
                    ORDER BY CAST(balance AS INTEGER) DESC
                """)
            
            rows = await cursor.fetchall()
            return [(row["address"], row["balance"]) for row in rows]
    
    async def get_holder_count(self, eoa_only: bool = True) -> int:
        """Get the count of holders with positive balance."""
        async with self.get_connection() as conn:
            if eoa_only:
                cursor = await conn.execute("""
                    SELECT COUNT(*) as count
                    FROM balances b
                    INNER JOIN address_types at ON b.address = at.address
                    WHERE at.is_eoa = 1
                """)
            else:
                cursor = await conn.execute("""
                    SELECT COUNT(*) as count FROM balances
                """)
            
            row = await cursor.fetchone()
            return row["count"] if row else 0
    
    async def get_transfer_count(self) -> int:
        """Get total number of indexed transfers."""
        async with self.get_connection() as conn:
            cursor = await conn.execute("SELECT COUNT(*) as count FROM transfers")
            row = await cursor.fetchone()
            return row["count"] if row else 0
    
    async def get_checked_address_count(self) -> int:
        """Get count of addresses that have been checked for EOA status."""
        async with self.get_connection() as conn:
            cursor = await conn.execute("SELECT COUNT(*) as count FROM address_types")
            row = await cursor.fetchone()
            return row["count"] if row else 0
    
    async def get_eoa_count(self) -> int:
        """Get count of addresses that are EOAs."""
        async with self.get_connection() as conn:
            cursor = await conn.execute("SELECT COUNT(*) as count FROM address_types WHERE is_eoa = 1")
            row = await cursor.fetchone()
            return row["count"] if row else 0


# Global database instance
db = Database()
