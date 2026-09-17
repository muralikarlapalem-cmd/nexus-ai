"""Shared pytest fixtures for NEXUS AI test suite."""
import pytest

@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"
