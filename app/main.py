"""FastAPI application for the PulseChain Token Indexer."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from cachetools import TTLCache
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST

from app.config import get_settings
from app.database import db
from app.indexer import indexer
from app.models import HoldersResponse, HealthResponse, Holder, SyncStatus

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
    'Number of EOA token holders'
)
TRANSFER_COUNT = Gauge(
    'indexer_transfer_count',
    'Total indexed transfers'
)
LAST_INDEXED_BLOCK = Gauge(
    'indexer_last_indexed_block',
    'Last indexed block number'
)
BLOCKS_BEHIND = Gauge(
    'indexer_blocks_behind',
    'Number of blocks behind chain head'
)
SYNC_IN_PROGRESS = Gauge(
    'indexer_sync_in_progress',
    'Whether sync is in progress (1=yes, 0=no)'
)

# Background task reference
sync_task: Optional[asyncio.Task] = None
metrics_task: Optional[asyncio.Task] = None


async def update_metrics():
    """Background task to update Prometheus metrics."""
    while True:
        try:
            holder_count = await db.get_holder_count(eoa_only=True)
            transfer_count = await db.get_transfer_count()
            last_block = await db.get_last_indexed_block()
            is_syncing = await db.is_syncing()
            chain_head = await indexer.get_current_block()
            
            HOLDER_COUNT.set(holder_count)
            TRANSFER_COUNT.set(transfer_count)
            LAST_INDEXED_BLOCK.set(last_block)
            BLOCKS_BEHIND.set(max(0, chain_head - last_block))
            SYNC_IN_PROGRESS.set(1 if is_syncing else 0)
        except Exception as e:
            logger.error(f"Error updating metrics: {e}")
        
        await asyncio.sleep(15)


async def start_background_sync():
    """Start the background synchronization task."""
    try:
        await indexer.sync()
    except asyncio.CancelledError:
        logger.info("Background sync task cancelled")
    except Exception as e:
        logger.error(f"Background sync error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    global sync_task, metrics_task
    
    # Startup
    logger.info("Starting PulseChain Token Indexer...")
    settings = get_settings()
    
    logger.info(f"Token Address: {settings.token_address}")
    logger.info(f"RPC URL: {settings.rpc_url}")
    logger.info(f"Start Block: {settings.start_block}")
    
    # Initialize database
    await db.connect()
    logger.info("Database connected (WAL mode enabled)")
    
    # Start background tasks
    sync_task = asyncio.create_task(start_background_sync())
    metrics_task = asyncio.create_task(update_metrics())
    logger.info("Background tasks started")
    
    yield
    
    # Shutdown
    logger.info("Shutting down...")
    
    # Stop the indexer
    indexer.stop()
    
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
    title="PulseChain Token Indexer",
    description="API for querying EOA token holders from indexed Transfer events",
    version="1.0.0",
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


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """
    Health check endpoint for Kubernetes probes.
    
    Returns the current sync status of the indexer.
    """
    try:
        last_block = await db.get_last_indexed_block()
        is_syncing = await db.is_syncing()
        
        return HealthResponse(
            status="healthy",
            last_indexed_block=last_block,
            sync_in_progress=is_syncing
        )
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        raise HTTPException(status_code=503, detail="Service unavailable")


@app.get("/holders", response_model=HoldersResponse)
async def get_holders(include_contracts: bool = False):
    """
    Get all token holders with their balances.
    
    Response is cached for 30 seconds and gzip compressed.
    
    Query Parameters:
        - include_contracts: If true, include contract addresses (default: false, EOA only)
    
    Excludes (by default):
    - Zero address
    - Contract addresses (unless include_contracts=true)
    
    Returns:
        - token_address: The indexed token contract address
        - holder_count: Total number of addresses with positive balance
        - last_indexed_block: The last block that was indexed
        - sync_in_progress: Whether the indexer is currently syncing
        - holders: List of all holders with their balances (in wei as string)
    """
    settings = get_settings()
    
    # Check cache first (different cache keys for different filters)
    cache_key = f"holders_response_{'all' if include_contracts else 'eoa'}"
    if cache_key in response_cache:
        return response_cache[cache_key]
    
    try:
        # Get holder data (EOA only or all based on parameter)
        holders_data = await db.get_holders_with_balances(eoa_only=not include_contracts)
        last_block = await db.get_last_indexed_block()
        is_syncing = await db.is_syncing()
        
        holders = [
            Holder(address=addr, balance=balance)
            for addr, balance in holders_data
        ]
        
        response = HoldersResponse(
            token_address=settings.token_address,
            holder_count=len(holders),
            last_indexed_block=last_block,
            sync_in_progress=is_syncing,
            holders=holders
        )
        
        # Cache the response
        response_cache[cache_key] = response
        
        return response
    except Exception as e:
        logger.error(f"Error fetching holders: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch holder data")


@app.get("/status", response_model=SyncStatus)
async def get_sync_status():
    """
    Get detailed sync status of the indexer.
    
    Returns progress information about the indexing process.
    """
    try:
        last_block = await db.get_last_indexed_block()
        is_syncing = await db.is_syncing()
        chain_head = await indexer.get_current_block()
        addresses_checked = await db.get_checked_address_count()
        
        return SyncStatus(
            last_indexed_block=last_block,
            chain_head_block=chain_head,
            blocks_behind=max(0, chain_head - last_block),
            is_syncing=is_syncing,
            addresses_checked=addresses_checked
        )
    except Exception as e:
        logger.error(f"Error fetching sync status: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch sync status")


@app.get("/stats")
async def get_stats():
    """
    Get indexer statistics.
    
    Returns counts and other useful metrics.
    """
    settings = get_settings()
    
    # Check cache
    cache_key = "stats_response"
    if cache_key in response_cache:
        return response_cache[cache_key]
    
    try:
        transfer_count = await db.get_transfer_count()
        holder_count = await db.get_holder_count(eoa_only=True)
        last_block = await db.get_last_indexed_block()
        is_syncing = await db.is_syncing()
        addresses_checked = await db.get_checked_address_count()
        eoa_count = await db.get_eoa_count()
        
        response = {
            "token_address": settings.token_address,
            "total_transfers_indexed": transfer_count,
            "eoa_holder_count": holder_count,
            "total_addresses_checked": addresses_checked,
            "total_eoa_addresses": eoa_count,
            "total_contract_addresses": addresses_checked - eoa_count,
            "last_indexed_block": last_block,
            "sync_in_progress": is_syncing,
            "start_block": settings.start_block
        }
        
        response_cache[cache_key] = response
        return response
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
