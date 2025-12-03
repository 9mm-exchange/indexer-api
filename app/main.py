"""FastAPI application for the Multi-Chain Token Indexer."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Request, Response, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from cachetools import TTLCache
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST

from app.config import get_settings
from app.database import db
from app.indexer import multi_indexer
from app.models import (
    HoldersResponse, HealthResponse, Holder, SyncStatus, 
    MultiChainSyncStatus, ChainInfo, ChainsResponse
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Response cache (TTL = 30 seconds)
response_cache: TTLCache = TTLCache(maxsize=100, ttl=30)

# Prometheus metrics
REQUEST_COUNT = Counter(
    'indexer_requests_total',
    'Total requests',
    ['method', 'endpoint', 'status']
)
REQUEST_LATENCY = Histogram(
    'indexer_request_latency_seconds',
    'Request latency',
    ['endpoint']
)
HOLDER_COUNT = Gauge(
    'indexer_holder_count',
    'Number of EOA token holders',
    ['chain_id']
)
TRANSFER_COUNT = Gauge(
    'indexer_transfer_count',
    'Total indexed transfers',
    ['chain_id']
)
LAST_INDEXED_BLOCK = Gauge(
    'indexer_last_indexed_block',
    'Last indexed block number',
    ['chain_id']
)
BLOCKS_BEHIND = Gauge(
    'indexer_blocks_behind',
    'Number of blocks behind chain head',
    ['chain_id']
)
SYNC_IN_PROGRESS = Gauge(
    'indexer_sync_in_progress',
    'Whether sync is in progress (1=yes, 0=no)',
    ['chain_id']
)

# Background task reference
sync_task: Optional[asyncio.Task] = None
metrics_task: Optional[asyncio.Task] = None


async def update_metrics():
    """Background task to update Prometheus metrics."""
    while True:
        try:
            chain_ids = multi_indexer.get_all_chain_ids()
            for chain_id in chain_ids:
                chain_config = await db.get_chain_config(chain_id)
                if chain_config:
                    holder_count = await db.get_holder_count(chain_id, eoa_only=True)
                    transfer_count = await db.get_transfer_count(chain_id)
                    last_block = await db.get_last_indexed_block(chain_id)
                    is_syncing = await db.is_syncing(chain_id)
                    
                    indexer = multi_indexer.get_indexer(chain_id)
                    if indexer:
                        try:
                            chain_head = await indexer.get_current_block()
                            BLOCKS_BEHIND.labels(chain_id=chain_id).set(max(0, chain_head - last_block))
                        except:
                            pass
            
                    HOLDER_COUNT.labels(chain_id=chain_id).set(holder_count)
                    TRANSFER_COUNT.labels(chain_id=chain_id).set(transfer_count)
                    LAST_INDEXED_BLOCK.labels(chain_id=chain_id).set(last_block)
                    SYNC_IN_PROGRESS.labels(chain_id=chain_id).set(1 if is_syncing else 0)
        except Exception as e:
            logger.error(f"Error updating metrics: {e}")
        
        await asyncio.sleep(15)


async def start_background_sync():
    """Start the background synchronization task."""
    try:
        await multi_indexer.sync_all()
    except asyncio.CancelledError:
        logger.info("Background sync task cancelled")
    except Exception as e:
        logger.error(f"Background sync error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    global sync_task, metrics_task
    
    # Startup
    logger.info("Starting Multi-Chain Token Indexer...")
    settings = get_settings()
    
    # Initialize database
    await db.connect()
    logger.info("Database connected (WAL mode enabled)")
    
    # Initialize multi-chain indexer
    await multi_indexer.initialize()
    
    # Start background tasks
    sync_task = asyncio.create_task(start_background_sync())
    metrics_task = asyncio.create_task(update_metrics())
    logger.info("Background tasks started")
    
    yield
    
    # Shutdown
    logger.info("Shutting down...")
    
    # Stop the indexer
    multi_indexer.stop()
    
    if sync_task:
        sync_task.cancel()
        try:
            await sync_task
        except asyncio.CancelledError:
            pass
    
    if metrics_task:
        metrics_task.cancel()
        try:
            await metrics_task
        except asyncio.CancelledError:
            pass
    
    await db.close()
    logger.info("Shutdown complete")


# Create FastAPI app
app = FastAPI(
    title="Multi-Chain Token Indexer",
    description="API for querying EOA token holders from indexed Transfer events across multiple EVM chains",
    version="2.0.0",
    lifespan=lifespan
)

# Add Gzip compression middleware (min 1KB to compress)
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    """Middleware to track request metrics."""
    start_time = time.time()
    response = await call_next(request)
    
    # Record metrics (skip /metrics endpoint to avoid recursion)
    if request.url.path != "/metrics":
        latency = time.time() - start_time
        REQUEST_COUNT.labels(
            method=request.method,
            endpoint=request.url.path,
            status=response.status_code
        ).inc()
        REQUEST_LATENCY.labels(endpoint=request.url.path).observe(latency)
    
    return response


@app.get("/chains", response_model=ChainsResponse)
async def get_chains():
    """Get all registered chains."""
    try:
        chains_data = await db.get_all_chains()
        chains = [
            ChainInfo(
                chain_id=chain["chain_id"],
                chain_name=chain["chain_name"],
                token_address=chain["token_address"],
                start_block=chain["start_block"],
                is_active=bool(chain["is_active"])
            )
            for chain in chains_data
        ]
        return ChainsResponse(chains=chains)
    except Exception as e:
        logger.error(f"Error fetching chains: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch chains")


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """
    Health check endpoint for Kubernetes probes.
    
    Returns the current sync status of all chains.
    """
    try:
        chains_data = await db.get_all_chains()
        chains = [
            ChainInfo(
                chain_id=chain["chain_id"],
                chain_name=chain["chain_name"],
                token_address=chain["token_address"],
                start_block=chain["start_block"],
                is_active=bool(chain["is_active"])
            )
            for chain in chains_data
        ]
        
        any_syncing = await db.is_any_syncing()
        
        return HealthResponse(
            status="healthy",
            chains=chains,
            any_syncing=any_syncing
        )
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        raise HTTPException(status_code=503, detail="Service unavailable")


@app.get("/holders", response_model=HoldersResponse)
async def get_holders(
    chain_id: int = Query(..., description="Chain ID to query"),
    include_contracts: bool = Query(False, description="Include contract addresses")
):
    """
    Get all token holders with their balances for a specific chain.
    
    Response is cached for 30 seconds and gzip compressed.
    
    Query Parameters:
        - chain_id: Chain ID to query (required)
        - include_contracts: If true, include contract addresses (default: false, EOA only)
    
    Excludes (by default):
    - Zero address
    - Contract addresses (unless include_contracts=true)
    
    Returns:
        - chain_id: The chain ID
        - chain_name: The chain name
        - token_address: The indexed token contract address
        - holder_count: Total number of addresses with positive balance
        - last_indexed_block: The last block that was indexed
        - sync_in_progress: Whether the indexer is currently syncing
        - holders: List of all holders with their balances (in wei as string)
    """
    # Check cache first (different cache keys for different filters)
    cache_key = f"holders_response_{chain_id}_{'all' if include_contracts else 'eoa'}"
    if cache_key in response_cache:
        return response_cache[cache_key]
    
    try:
        # Get chain config
        chain_config = await db.get_chain_config(chain_id)
        if not chain_config:
            raise HTTPException(status_code=404, detail=f"Chain {chain_id} not found")
        
        # Get holder data (EOA only or all based on parameter)
        holders_data = await db.get_holders_with_balances(chain_id, eoa_only=not include_contracts)
        last_block = await db.get_last_indexed_block(chain_id)
        is_syncing = await db.is_syncing(chain_id)
        
        holders = [
            Holder(address=addr, balance=balance)
            for addr, balance in holders_data
        ]
        
        response = HoldersResponse(
            chain_id=chain_id,
            chain_name=chain_config["chain_name"],
            token_address=chain_config["token_address"],
            holder_count=len(holders),
            last_indexed_block=last_block,
            sync_in_progress=is_syncing,
            holders=holders
        )
        
        # Cache the response
        response_cache[cache_key] = response
        
        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching holders: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch holder data")


@app.get("/status", response_model=MultiChainSyncStatus)
async def get_sync_status(chain_id: Optional[int] = Query(None, description="Optional chain ID filter")):
    """
    Get detailed sync status of the indexer.
    
    Query Parameters:
        - chain_id: Optional chain ID to filter (if not provided, returns all chains)
    
    Returns progress information about the indexing process.
    """
    try:
        if chain_id:
            chain_config = await db.get_chain_config(chain_id)
            if not chain_config:
                raise HTTPException(status_code=404, detail=f"Chain {chain_id} not found")
            
            chain_configs = [chain_config]
        else:
            chain_configs = await db.get_all_chains()
        
        statuses = []
        for chain_config in chain_configs:
            cid = chain_config["chain_id"]
            last_block = await db.get_last_indexed_block(cid)
            is_syncing = await db.is_syncing(cid)
            addresses_checked = await db.get_checked_address_count(cid)
            
            indexer = multi_indexer.get_indexer(cid)
            chain_head = 0
            if indexer:
                try:
                    chain_head = await indexer.get_current_block()
                except:
                    pass
            
            statuses.append(SyncStatus(
                chain_id=cid,
                chain_name=chain_config["chain_name"],
            last_indexed_block=last_block,
            chain_head_block=chain_head,
            blocks_behind=max(0, chain_head - last_block),
            is_syncing=is_syncing,
            addresses_checked=addresses_checked
            ))
        
        return MultiChainSyncStatus(chains=statuses)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching sync status: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch sync status")


@app.get("/stats")
async def get_stats(chain_id: Optional[int] = Query(None, description="Optional chain ID filter")):
    """
    Get indexer statistics.
    
    Query Parameters:
        - chain_id: Optional chain ID to filter (if not provided, returns all chains)
    
    Returns counts and other useful metrics.
    """
    # Check cache
    cache_key = f"stats_response_{chain_id if chain_id else 'all'}"
    if cache_key in response_cache:
        return response_cache[cache_key]
    
    try:
        if chain_id:
            chain_config = await db.get_chain_config(chain_id)
            if not chain_config:
                raise HTTPException(status_code=404, detail=f"Chain {chain_id} not found")
            
            chain_configs = [chain_config]
        else:
            chain_configs = await db.get_all_chains()
        
        stats = []
        for chain_config in chain_configs:
            cid = chain_config["chain_id"]
            transfer_count = await db.get_transfer_count(cid)
            holder_count = await db.get_holder_count(cid, eoa_only=True)
            last_block = await db.get_last_indexed_block(cid)
            is_syncing = await db.is_syncing(cid)
            addresses_checked = await db.get_checked_address_count(cid)
            eoa_count = await db.get_eoa_count(cid)
            
            stats.append({
                "chain_id": cid,
                "chain_name": chain_config["chain_name"],
                "token_address": chain_config["token_address"],
                "total_transfers_indexed": transfer_count,
                "eoa_holder_count": holder_count,
                "total_addresses_checked": addresses_checked,
                "total_eoa_addresses": eoa_count,
                "total_contract_addresses": addresses_checked - eoa_count,
                "last_indexed_block": last_block,
                "sync_in_progress": is_syncing,
                "start_block": chain_config["start_block"]
            })
        
        response = stats[0] if chain_id else {"chains": stats}
        
        response_cache[cache_key] = response
        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching stats: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch stats")


@app.get("/metrics")
async def prometheus_metrics():
    """
    Prometheus metrics endpoint.
    
    Returns metrics in Prometheus text format for scraping.
    """
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
