"""Transfer event indexer for ERC20 tokens."""

import asyncio
import logging
from typing import List, Tuple, Dict
from web3 import Web3
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import requests

from app.config import get_settings
from app.database import db

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ERC20 Transfer event signature
TRANSFER_EVENT_SIGNATURE = Web3.keccak(text="Transfer(address,address,uint256)").hex()

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


class TokenIndexer:
    """Indexes Transfer events for an ERC20 token."""
    
    def __init__(self):
        self.settings = get_settings()
        self.w3 = Web3(Web3.HTTPProvider(self.settings.rpc_url, request_kwargs={'timeout': 60}))
        self.token_address = Web3.to_checksum_address(self.settings.token_address)
        self.contract = self.w3.eth.contract(
            address=self.token_address,
            abi=ERC20_TRANSFER_ABI
        )
        self._stop_requested = False
        self._initial_sync_done = False
    
    def stop(self):
        """Request the indexer to stop."""
        self._stop_requested = True
    
    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((requests.exceptions.RequestException, Exception)),
        before_sleep=lambda retry_state: logger.warning(f"Retrying after error, attempt {retry_state.attempt_number}")
    )
    async def get_current_block(self) -> int:
        """Get the current block number from the chain with retry."""
        return self.w3.eth.block_number
    
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
                self.settings.rpc_url,
                json=batch_requests,
                headers={"Content-Type": "application/json"},
                timeout=60
            )
            response.raise_for_status()
            results = response.json()
            
            # Parse results
            eoa_map = {}
            for i, addr in enumerate(addresses):
                # Find matching result by id
                result = next((r for r in results if r.get("id") == i), None)
                if result and "result" in result:
                    code = result["result"]
                    is_eoa = code == "0x" or code == "" or code is None
                    eoa_map[addr] = is_eoa
                else:
                    # Assume contract if we can't determine
                    eoa_map[addr] = False
            
            return eoa_map
            
        except Exception as e:
            logger.error(f"Batch EOA check failed: {e}")
            # Fall back to individual checks
            return await self._fallback_check_eoa(addresses)
    
    async def _fallback_check_eoa(self, addresses: List[str]) -> Dict[str, bool]:
        """Fallback to individual EOA checks if batch fails."""
        results = {}
        for addr in addresses:
            try:
                code = self.w3.eth.get_code(Web3.to_checksum_address(addr))
                results[addr] = code == b'' or code.hex() == '0x'
            except Exception:
                results[addr] = False
            await asyncio.sleep(0.02)
        return results
    
    async def check_and_cache_address_types(self):
        """Check uncached addresses and determine if they're EOAs using batch requests."""
        unchecked = await db.get_unchecked_addresses()
        
        if not unchecked:
            return
        
        logger.info(f"Checking {len(unchecked)} addresses for EOA status...")
        
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
            await db.batch_set_address_types(results)
            
            checked_count += len(results)
            eoa_count = sum(1 for is_eoa in eoa_results.values() if is_eoa)
            eoa_total += eoa_count
            
            logger.info(f"Checked {checked_count}/{len(unchecked)} addresses. Batch: {eoa_count}/{len(batch)} EOAs")
            
            # Small delay between batches
            await asyncio.sleep(0.1)
        
        logger.info(f"Finished checking address types. EOAs found: {eoa_total}/{checked_count}")
    
    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((requests.exceptions.RequestException, Exception)),
        before_sleep=lambda retry_state: logger.warning(f"Retrying fetch_transfer_events, attempt {retry_state.attempt_number}")
    )
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
                logger.warning(f"Failed to decode log: {e}")
                continue
        
        return transfers
    
    async def index_blocks(self, start_block: int, end_block: int):
        """
        Index transfer events for a range of blocks.
        
        Args:
            start_block: Starting block number
            end_block: Ending block number
        """
        batch_size = self.settings.batch_size
        current_block = start_block
        total_transfers = 0
        
        logger.info(f"Indexing blocks {start_block} to {end_block}")
        
        while current_block <= end_block and not self._stop_requested:
            batch_end = min(current_block + batch_size - 1, end_block)
            
            try:
                # Fetch transfers for this batch
                transfers = await self.fetch_transfer_events(current_block, batch_end)
                
                if transfers:
                    await db.insert_transfers(transfers)
                    # Update balances incrementally
                    await db.update_balances_from_transfers(transfers)
                    total_transfers += len(transfers)
                
                # Update progress
                await db.update_last_indexed_block(batch_end)
                
                progress = ((batch_end - start_block) / (end_block - start_block)) * 100 if end_block > start_block else 100
                logger.info(
                    f"Blocks {current_block}-{batch_end} | "
                    f"Transfers: {len(transfers)} | "
                    f"Total: {total_transfers} | "
                    f"Progress: {progress:.1f}%"
                )
                
                current_block = batch_end + 1
                
                # Small delay to avoid overwhelming the RPC
                await asyncio.sleep(0.1)
                
            except Exception as e:
                logger.error(f"Error indexing batch {current_block}-{batch_end}: {e}")
                # Wait before retrying (tenacity handles retries for fetch)
                await asyncio.sleep(5)
                continue
        
        return total_transfers
    
    async def sync(self):
        """
        Main sync loop - indexes from start block and continuously syncs new blocks.
        """
        await db.set_syncing(True)
        self._stop_requested = False
        
        try:
            # Get last indexed block
            last_indexed = await db.get_last_indexed_block()
            start_block = self.settings.start_block
            
            # If we haven't started yet, start from the configured start block
            if last_indexed < start_block:
                last_indexed = start_block - 1
                await db.update_last_indexed_block(last_indexed)
                logger.info(f"Set initial start block to {start_block}")
            
            logger.info(f"Last indexed block: {last_indexed}")
            
            # Check if this is initial sync (no balances yet)
            holder_count = await db.get_holder_count(eoa_only=False)
            if holder_count == 0 and last_indexed >= start_block:
                logger.info("Rebuilding balances table from existing transfers...")
                await db.rebuild_all_balances()
                logger.info("Balances rebuilt successfully")
            
            # Continuous sync loop
            while not self._stop_requested:
                current_chain_block = await self.get_current_block()
                last_indexed = await db.get_last_indexed_block()
                
                if current_chain_block > last_indexed:
                    blocks_behind = current_chain_block - last_indexed
                    logger.info(f"Chain head: {current_chain_block}, Last indexed: {last_indexed}, Behind: {blocks_behind} blocks")
                    
                    await self.index_blocks(last_indexed + 1, current_chain_block)
                    
                    # After indexing new blocks, check address types
                    await self.check_and_cache_address_types()
                    
                    if not self._initial_sync_done:
                        self._initial_sync_done = True
                        logger.info("Initial sync complete!")
                else:
                    logger.debug("Up to date, waiting for new blocks...")
                
                # Wait before checking for new blocks (PulseChain ~10s block time)
                await asyncio.sleep(12)
                
        except Exception as e:
            logger.error(f"Sync error: {e}")
            raise
        finally:
            await db.set_syncing(False)


# Global indexer instance
indexer = TokenIndexer()
