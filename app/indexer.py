"""Transfer event indexer for ERC20 tokens."""

import asyncio
import logging
import traceback
from typing import List, Tuple, Dict
from web3 import Web3
import requests

from app.config import get_settings, ChainConfig
from app.database import db

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ERC20 Transfer event signature
TRANSFER_EVENT_SIGNATURE = Web3.keccak(text="Transfer(address,address,uint256)").hex()

# Smart wallet bytecode patterns to whitelist (treat as EOA/user wallets)
# These are contract wallets controlled by users, not DeFi/token contracts
SMART_WALLET_PATTERNS = [
    "0xef01",      # ERC-4337 Account Abstraction (Coinbase Smart Wallet, etc.)
    "0xef0100",    # ERC-4337 with version byte
    "0x363d3d373d3d3d363d73",  # EIP-1167 Minimal Proxy (used by Safe, etc.)
]

def is_smart_wallet(code: str) -> bool:
    """
    Check if bytecode matches known smart wallet patterns.
    
    Smart wallets are contract wallets controlled by users (not DeFi protocols).
    Examples: Coinbase Smart Wallet, Safe (Gnosis Safe), Argent, etc.
    
    Args:
        code: Bytecode hex string (with 0x prefix)
        
    Returns:
        True if code matches a smart wallet pattern
    """
    if not code or code == "0x":
        return False
    
    code_lower = code.lower()
    for pattern in SMART_WALLET_PATTERNS:
        if code_lower.startswith(pattern.lower()):
            return True
    
    return False

# Standard ERC20 ABI for Transfer event
ERC20_TRANSFER_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "from", "type": "address"},
            {"indexed": True, "name": "to", "type": "address"},
            {"indexed": False, "name": "value", "type": "uint256"}
        ],
        "name": "Transfer",
        "type": "event"
    }
]


class ChainIndexer:
    """Indexes Transfer events for an ERC20 token on a specific chain."""
    
    # Default batch sizes per chain (some RPCs have lower limits)
    DEFAULT_BATCH_SIZES = {
        1: 1000,      # Ethereum - strict 1k limit on public RPCs
        369: 2000,    # PulseChain - can timeout with large batches
        8453: 10000,  # Base - handles larger batches well
        146: 10000,   # Sonic - handles larger batches well
    }
    
    def __init__(self, chain_config: ChainConfig):
        self.chain_config = chain_config
        self.chain_id = chain_config.chain_id
        self.w3 = Web3(Web3.HTTPProvider(chain_config.rpc_url, request_kwargs={'timeout': 60}))
        self.token_address = Web3.to_checksum_address(chain_config.token_address)
        self.contract = self.w3.eth.contract(
            address=self.token_address,
            abi=ERC20_TRANSFER_ABI
        )
        self._stop_requested = False
        self._initial_sync_done = False
        # Adaptive batch size - starts with chain-specific default or global setting
        settings = get_settings()
        self._batch_size = self.DEFAULT_BATCH_SIZES.get(chain_config.chain_id, settings.batch_size)
        self._min_batch_size = 100  # Don't go below this
    
    def stop(self):
        """Request the indexer to stop."""
        self._stop_requested = True
    
    async def get_current_block(self) -> int:
        """Get the current block number from the chain with retry."""
        for attempt in range(5):
            try:
                return self.w3.eth.block_number
            except Exception as e:
                if attempt < 4:
                    wait_time = min(30, 2 ** attempt)
                    logger.warning(f"[Chain {self.chain_id}] Retrying get_current_block after error, attempt {attempt + 1}: {e}")
                    await asyncio.sleep(wait_time)
                else:
                    raise
    
    async def batch_check_eoa(self, addresses: List[str]) -> Dict[str, bool]:
        """
        Batch check if addresses are EOAs using JSON-RPC batch requests.
        
        Args:
            addresses: List of addresses to check
            
        Returns:
            Dict mapping address to is_eoa boolean
        """
        if not addresses:
            return {}
        
        # Build batch request
        batch_requests = []
        for i, addr in enumerate(addresses):
            batch_requests.append({
                "jsonrpc": "2.0",
                "method": "eth_getCode",
                "params": [Web3.to_checksum_address(addr), "latest"],
                "id": i
            })
        
        try:
            # Send batch request
            response = requests.post(
                self.chain_config.rpc_url,
                json=batch_requests,
                headers={"Content-Type": "application/json"},
                timeout=60
            )
            response.raise_for_status()
            results = response.json()
            
            # Parse results
            eoa_map = {}
            smart_wallet_count = 0
            for i, addr in enumerate(addresses):
                # Find matching result by id
                result = next((r for r in results if r.get("id") == i), None)
                if result and "result" in result:
                    code = result["result"]
                    # Check if EOA (no code) or smart wallet (whitelisted pattern)
                    if code == "0x" or code == "" or code is None:
                        eoa_map[addr] = True
                    elif is_smart_wallet(code):
                        eoa_map[addr] = True
                        smart_wallet_count += 1
                    else:
                        eoa_map[addr] = False
                else:
                    # Assume contract if we can't determine
                    eoa_map[addr] = False
            
            if smart_wallet_count > 0:
                logger.debug(f"[Chain {self.chain_id}] Found {smart_wallet_count} smart wallets in batch")
            
            return eoa_map
            
        except Exception as e:
            logger.error(f"[Chain {self.chain_id}] Batch EOA check failed: {e}")
            # Fall back to individual checks
            return await self._fallback_check_eoa(addresses)
    
    async def _fallback_check_eoa(self, addresses: List[str]) -> Dict[str, bool]:
        """Fallback to individual EOA checks if batch fails."""
        results = {}
        for addr in addresses:
            try:
                code = self.w3.eth.get_code(Web3.to_checksum_address(addr))
                code_hex = code.hex() if isinstance(code, bytes) else code
                # Check if EOA (no code) or smart wallet (whitelisted pattern)
                if code == b'' or code_hex == '0x' or code_hex == '':
                    results[addr] = True
                elif is_smart_wallet('0x' + code_hex if not code_hex.startswith('0x') else code_hex):
                    results[addr] = True
                else:
                    results[addr] = False
            except Exception:
                results[addr] = False
            await asyncio.sleep(0.02)
        return results
    
    async def check_and_cache_address_types(self):
        """Check uncached addresses and determine if they're EOAs using batch requests."""
        unchecked = await db.get_unchecked_addresses(self.chain_id)
        
        if not unchecked:
            return
        
        logger.info(f"[Chain {self.chain_id}] Checking {len(unchecked)} addresses for EOA status...")
        
        batch_size = 100  # Batch size for RPC requests
        checked_count = 0
        eoa_total = 0
        
        for i in range(0, len(unchecked), batch_size):
            if self._stop_requested:
                break
            
            batch = unchecked[i:i + batch_size]
            
            # Batch check EOA status
            eoa_results = await self.batch_check_eoa(batch)
            
            # Save to database
            results = [(addr, is_eoa) for addr, is_eoa in eoa_results.items()]
            await db.batch_set_address_types(self.chain_id, results)
            
            checked_count += len(results)
            eoa_count = sum(1 for is_eoa in eoa_results.values() if is_eoa)
            eoa_total += eoa_count
            
            logger.info(f"[Chain {self.chain_id}] Checked {checked_count}/{len(unchecked)} addresses. Batch: {eoa_count}/{len(batch)} EOAs")
            
            # Small delay between batches
            await asyncio.sleep(0.1)
        
        logger.info(f"[Chain {self.chain_id}] Finished checking address types. EOAs found: {eoa_total}/{checked_count}")
    
    async def recheck_smart_wallets(self) -> Dict[str, int]:
        """
        Recheck addresses previously marked as contracts to detect smart wallets.
        
        This is useful after updating smart wallet patterns to reclassify
        addresses that were incorrectly marked as contracts.
        
        Returns:
            Dict with counts: {'rechecked': N, 'smart_wallets_found': M}
        """
        contracts = await db.get_contract_addresses(self.chain_id)
        
        if not contracts:
            logger.info(f"[Chain {self.chain_id}] No contract addresses to recheck")
            return {'rechecked': 0, 'smart_wallets_found': 0}
        
        logger.info(f"[Chain {self.chain_id}] Rechecking {len(contracts)} contract addresses for smart wallets...")
        
        batch_size = 100
        rechecked = 0
        smart_wallets_found = 0
        
        for i in range(0, len(contracts), batch_size):
            batch = contracts[i:i + batch_size]
            
            # Batch check - this now includes smart wallet detection
            eoa_results = await self.batch_check_eoa(batch)
            
            # Only update addresses that are now identified as EOA/smart wallets
            updates = [(addr, True) for addr, is_eoa in eoa_results.items() if is_eoa]
            if updates:
                await db.batch_set_address_types(self.chain_id, updates)
                smart_wallets_found += len(updates)
            
            rechecked += len(batch)
            logger.info(f"[Chain {self.chain_id}] Rechecked {rechecked}/{len(contracts)}. Smart wallets found so far: {smart_wallets_found}")
            
            await asyncio.sleep(0.1)
        
        logger.info(f"[Chain {self.chain_id}] Recheck complete. Found {smart_wallets_found} smart wallets out of {rechecked} contracts")
        return {'rechecked': rechecked, 'smart_wallets_found': smart_wallets_found}
    
    async def fetch_transfer_events(
        self, 
        from_block: int, 
        to_block: int
    ) -> List[Tuple]:
        """
        Fetch Transfer events for a block range with retry.
        
        Returns:
            List of tuples (block_number, tx_hash, log_index, from_addr, to_addr, value)
        """
        for attempt in range(5):
            try:
                # Create event filter
                event_filter = {
                    "fromBlock": from_block,
                    "toBlock": to_block,
                    "address": self.token_address,
                    "topics": [TRANSFER_EVENT_SIGNATURE]
                }
                
                # Fetch logs
                logs = self.w3.eth.get_logs(event_filter)
                
                transfers = []
                for log in logs:
                    try:
                        # Topics: [event_sig, from_address, to_address]
                        from_addr = Web3.to_checksum_address("0x" + log["topics"][1].hex()[-40:])
                        to_addr = Web3.to_checksum_address("0x" + log["topics"][2].hex()[-40:])
                        
                        # Data contains the value
                        value = int(log["data"].hex(), 16)
                        
                        transfers.append((
                            log["blockNumber"],
                            log["transactionHash"].hex(),
                            log["logIndex"],
                            from_addr,
                            to_addr,
                            str(value)
                        ))
                    except Exception as e:
                        logger.warning(f"[Chain {self.chain_id}] Failed to decode log: {e}")
                        continue
                
                return transfers
            except Exception as e:
                error_str = str(e).lower()
                # Check if error is due to block range being too large
                if "range" in error_str or "too large" in error_str or "timeout" in error_str or "exceeded" in error_str:
                    # Reduce batch size for future requests
                    old_batch = self._batch_size
                    self._batch_size = max(self._min_batch_size, self._batch_size // 2)
                    if self._batch_size != old_batch:
                        logger.warning(f"[Chain {self.chain_id}] Reducing batch size from {old_batch} to {self._batch_size} due to RPC limits")
                    # Return empty to trigger retry with smaller batch in index_blocks
                    raise
                
                if attempt < 4:
                    wait_time = min(30, 2 ** attempt)
                    logger.warning(f"[Chain {self.chain_id}] Retrying fetch_transfer_events, attempt {attempt + 1}: {e}")
                    await asyncio.sleep(wait_time)
                else:
                    raise
        
        return []
    
    async def index_blocks(self, start_block: int, end_block: int):
        """
        Index transfer events for a range of blocks.
        Uses adaptive batch sizing - automatically reduces batch size if RPC rejects.
        
        Args:
            start_block: Starting block number
            end_block: Ending block number
        """
        current_block = start_block
        total_transfers = 0
        consecutive_errors = 0
        
        logger.info(f"[Chain {self.chain_id}] Indexing blocks {start_block} to {end_block} (batch size: {self._batch_size})")
        
        while current_block <= end_block and not self._stop_requested:
            # Use adaptive batch size
            batch_end = min(current_block + self._batch_size - 1, end_block)
            
            try:
                # Fetch transfers for this batch
                transfers = await self.fetch_transfer_events(current_block, batch_end)
                
                if transfers:
                    await db.insert_transfers(self.chain_id, transfers)
                    # Update balances incrementally
                    await db.update_balances_from_transfers(self.chain_id, transfers)
                    total_transfers += len(transfers)
                
                # Update progress
                await db.update_last_indexed_block(self.chain_id, batch_end)
                
                progress = ((batch_end - start_block) / (end_block - start_block)) * 100 if end_block > start_block else 100
                logger.info(
                    f"[Chain {self.chain_id}] Blocks {current_block}-{batch_end} | "
                    f"Transfers: {len(transfers)} | "
                    f"Total: {total_transfers} | "
                    f"Progress: {progress:.1f}% | "
                    f"Batch: {self._batch_size}"
                )
                
                current_block = batch_end + 1
                consecutive_errors = 0
                
                # Small delay to avoid overwhelming the RPC
                await asyncio.sleep(0.05)
                
            except Exception as e:
                consecutive_errors += 1
                error_str = str(e).lower()
                
                # If batch size related error, the batch size was already reduced in fetch_transfer_events
                # Just retry with the new smaller batch
                if "range" in error_str or "too large" in error_str or "timeout" in error_str or "exceeded" in error_str:
                    logger.warning(f"[Chain {self.chain_id}] Retrying with smaller batch size: {self._batch_size}")
                    await asyncio.sleep(1)
                    continue
                
                logger.error(f"[Chain {self.chain_id}] Error indexing batch {current_block}-{batch_end}: {e}")
                logger.error(f"[Chain {self.chain_id}] Full traceback:\n{traceback.format_exc()}")
                
                # If too many consecutive errors, reduce batch size anyway
                if consecutive_errors >= 3 and self._batch_size > self._min_batch_size:
                    self._batch_size = max(self._min_batch_size, self._batch_size // 2)
                    logger.warning(f"[Chain {self.chain_id}] Reducing batch size to {self._batch_size} after {consecutive_errors} errors")
                
                await asyncio.sleep(5)
                continue
        
        return total_transfers
    
    async def sync(self):
        """
        Main sync loop - indexes from start block and continuously syncs new blocks.
        """
        await db.set_syncing(self.chain_id, True)
        self._stop_requested = False
        
        try:
            # Get last indexed block
            last_indexed = await db.get_last_indexed_block(self.chain_id)
            start_block = self.chain_config.start_block
            
            # If we haven't started yet, start from the configured start block
            if last_indexed < start_block:
                last_indexed = start_block - 1
                await db.update_last_indexed_block(self.chain_id, last_indexed)
                logger.info(f"[Chain {self.chain_id}] Set initial start block to {start_block}")
            
            logger.info(f"[Chain {self.chain_id}] Last indexed block: {last_indexed}")
            
            # Check if this is initial sync (no balances yet)
            holder_count = await db.get_holder_count(self.chain_id, eoa_only=False)
            if holder_count == 0 and last_indexed >= start_block:
                logger.info(f"[Chain {self.chain_id}] Rebuilding balances table from existing transfers...")
                await db.rebuild_all_balances(self.chain_id)
                logger.info(f"[Chain {self.chain_id}] Balances rebuilt successfully")
            
            # Continuous sync loop
            while not self._stop_requested:
                current_chain_block = await self.get_current_block()
                last_indexed = await db.get_last_indexed_block(self.chain_id)
                
                if current_chain_block > last_indexed:
                    blocks_behind = current_chain_block - last_indexed
                    logger.info(f"[Chain {self.chain_id}] Chain head: {current_chain_block}, Last indexed: {last_indexed}, Behind: {blocks_behind} blocks")
                    
                    await self.index_blocks(last_indexed + 1, current_chain_block)
                    
                    # After indexing new blocks, check address types
                    await self.check_and_cache_address_types()
                    
                    if not self._initial_sync_done:
                        self._initial_sync_done = True
                        logger.info(f"[Chain {self.chain_id}] Initial sync complete!")
                else:
                    logger.debug(f"[Chain {self.chain_id}] Up to date, waiting for new blocks...")
                
                # Wait before checking for new blocks (adjust per chain if needed)
                await asyncio.sleep(12)
                
        except Exception as e:
            logger.error(f"[Chain {self.chain_id}] Sync error: {e}")
            raise
        finally:
            await db.set_syncing(self.chain_id, False)


class MultiChainIndexer:
    """Manages multiple chain indexers."""
    
    def __init__(self):
        self.settings = get_settings()
        self.indexers: Dict[int, ChainIndexer] = {}
        self._stop_requested = False
    
    async def initialize(self):
        """Initialize all chain indexers from configuration."""
        chains = self.settings.get_chains()
        
        logger.info(f"Initializing {len(chains)} chain indexers...")
        
        for chain_config in chains:
            # Register chain in database
            await db.register_chain(
                chain_id=chain_config.chain_id,
                chain_name=chain_config.chain_name,
                rpc_url=chain_config.rpc_url,
                token_address=chain_config.token_address,
                start_block=chain_config.start_block
            )
            
            # Create indexer for this chain
            indexer = ChainIndexer(chain_config)
            self.indexers[chain_config.chain_id] = indexer
            
            logger.info(
                f"Registered chain: {chain_config.chain_name} (ID: {chain_config.chain_id}) "
                f"Token: {chain_config.token_address} Start: {chain_config.start_block}"
            )
    
    def stop(self):
        """Stop all indexers."""
        self._stop_requested = True
        for indexer in self.indexers.values():
            indexer.stop()
    
    async def sync_all(self):
        """Start syncing all chains concurrently."""
        tasks = []
        for chain_id, indexer in self.indexers.items():
            task = asyncio.create_task(indexer.sync())
            tasks.append(task)
            logger.info(f"Started sync task for chain {chain_id}")
        
        # Wait for all tasks (they run indefinitely until stopped)
        await asyncio.gather(*tasks, return_exceptions=True)
    
    def get_indexer(self, chain_id: int) -> ChainIndexer:
        """Get indexer for a specific chain."""
        return self.indexers.get(chain_id)
    
    def get_all_chain_ids(self) -> List[int]:
        """Get all registered chain IDs."""
        return list(self.indexers.keys())


# Global multi-chain indexer instance
multi_indexer = MultiChainIndexer()
