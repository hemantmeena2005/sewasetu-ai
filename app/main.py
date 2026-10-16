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


from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route

async def mcp_citizen_protected_resource(request):
    import oauth
    base = oauth.get_issuer_url()
    return JSONResponse({
        "resource": base + "/mcp/citizen",
        "authorization_servers": [base],
        "scopes_supported": ["citizen", "officer"],
        "bearer_methods_supported": ["header"],
        "resource_documentation": base + "/about"
    }, headers={"Access-Control-Allow-Origin": "*"})

async def mcp_officer_protected_resource(request):
    import oauth
    base = oauth.get_issuer_url()
    return JSONResponse({
        "resource": base + "/mcp/officer",
        "authorization_servers": [base],
        "scopes_supported": ["citizen", "officer"],
        "bearer_methods_supported": ["header"],
        "resource_documentation": base + "/about"
    }, headers={"Access-Control-Allow-Origin": "*"})

# Unified Starlette Application
application = Starlette(
    routes=[
        Route("/mcp/citizen/.well-known/oauth-protected-resource", mcp_citizen_protected_resource, methods=["GET", "OPTIONS"]),
        Route("/mcp/officer/.well-known/oauth-protected-resource", mcp_officer_protected_resource, methods=["GET", "OPTIONS"]),
        Mount("/mcp/citizen", citizen_mcp_app),
        Mount("/mcp/officer", officer_mcp_app),
        Mount("/", WSGIMiddleware(portal.app)),
    ],
    middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["*"]
        )
    ],
    lifespan=app_lifespan
)

class TrailingSlashNormalizeMiddleware:
    """
    Ensures URLs without trailing slashes (e.g. /mcp/citizen and /mcp/officer)
    route seamlessly into mounted Starlette ASGI MCP sub-applications.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if path in ("/mcp/citizen", "/mcp/officer"):
                scope["path"] = path + "/"
        await self.app(scope, receive, send)


app = TrailingSlashNormalizeMiddleware(application)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
