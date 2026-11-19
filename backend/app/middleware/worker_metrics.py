"""
NEXUS AI · app/middleware/worker_metrics.py
Prometheus endpoint for the Celery worker.

The worker runs agents in several child processes. prometheus_client's
multiprocess mode has each process write its numbers to files in
PROMETHEUS_MULTIPROC_DIR (set in docker-compose); this endpoint adds them up.

This module must not import app.middleware.tracing: the folder has to be
cleared before any metric object exists, or the processes would write to
files that were just deleted.
"""

import os
import shutil

from loguru import logger


def start_worker_metrics_server(port: int = 9101) -> int | None:
    """
    Main worker process, before the children start: clear old metric files and
    serve the combined metrics on `port`. Returns the port, or None if not started.
    """
    path = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if not path:
        return None
    try:
        shutil.rmtree(path, ignore_errors=True)
        os.makedirs(path, exist_ok=True)
        from prometheus_client import CollectorRegistry, multiprocess, start_http_server

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        server, _ = start_http_server(port, registry=registry)

        # Now that the folder is clean: create the known series at 0, so
        # Prometheus sees the first agent run or sync of each kind
        from app.middleware.tracing import init_metric_labels
        init_metric_labels(_connection_names())
        actual = server.server_port
        logger.info(f"Worker metrics on :{actual}/metrics")
        return actual
    except Exception as e:
        logger.warning(f"Worker metrics server not started: {e}")
        return None


def _connection_names() -> list[str]:
    """Names of the integration connections, for their metric series. [] if the DB isn't reachable."""
    import asyncio

    async def _load() -> list[str]:
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        from app.config import settings
        from app.models.integration import IntegrationConnection

        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return list((await conn.execute(select(IntegrationConnection.name))).scalars())
        finally:
            await engine.dispose()

    try:
        return asyncio.run(asyncio.wait_for(_load(), timeout=10))
    except Exception as e:
        logger.warning(f"Worker metrics: could not read connection names ({e})")
        return []


def mark_worker_process_dead(pid: int) -> None:
    """Child process exiting: drop its live gauges from the combined numbers."""
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        try:
            from prometheus_client import multiprocess
            multiprocess.mark_process_dead(pid)
        except Exception:
            pass
