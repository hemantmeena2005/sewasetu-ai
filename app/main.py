# -*- coding: utf-8 -*-
"""
Sewa Setu - Unified ASGI Application Entrypoint.

Bridges the Flask web portal (WSGI) and the Model Context Protocol servers (ASGI)
under a single unified HTTP interface with native Streamable HTTP support for both
Citizen MCP and Officer MCP servers.
"""

from contextlib import asynccontextmanager
from starlette.applications import Starlette
from starlette.routing import Mount
from a2wsgi import WSGIMiddleware

import app as portal
import mcp_citizen
import mcp_officer

# Construct citizen and officer MCP ASGI applications
citizen_mcp_app = mcp_citizen.create_citizen_mcp_app()
officer_mcp_app = mcp_officer.create_officer_mcp_app()


@asynccontextmanager
async def app_lifespan(app):
    """Manage background task groups and lifespans for all mounted MCP servers."""
    import services
    services.init_db_if_needed()
    async with citizen_mcp_app.router.lifespan_context(citizen_mcp_app):
        async with officer_mcp_app.raw_app.router.lifespan_context(officer_mcp_app.raw_app):
            yield


# Unified Starlette Application
application = Starlette(
    routes=[
        Mount("/mcp/citizen", citizen_mcp_app),
        Mount("/mcp/officer", officer_mcp_app),
        Mount("/", WSGIMiddleware(portal.app)),
    ],
    lifespan=app_lifespan
)

app = application

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
