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
        actual = server.server_port
        logger.info(f"Worker metrics on :{actual}/metrics")
        return actual
    except Exception as e:
        logger.warning(f"Worker metrics server not started: {e}")
        return None


def mark_worker_process_dead(pid: int) -> None:
    """Child process exiting: drop its live gauges from the combined numbers."""
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        try:
            from prometheus_client import multiprocess
            multiprocess.mark_process_dead(pid)
        except Exception:
            pass
