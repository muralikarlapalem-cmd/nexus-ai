"""
Built-in connectors. Importing a module here registers its connector.
To add a source system, create a module in this folder and import it below.
"""

from app.integrations.connectors import generic_rest, servicenow  # noqa: F401
