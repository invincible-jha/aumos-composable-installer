"""CLI command: aumos serve-installer — launch the GUI installer backend.

Gap #11: GUI installer backend.

Starts a local FastAPI server on localhost:8080 that the browser-based
GUI installer communicates with. Binds to localhost only for security.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel

from aumos_common.observability import get_logger

logger = get_logger(__name__)
console = Console()

app = typer.Typer(
    help="Launch the AumOS GUI installer backend.",
    no_args_is_help=False,
)


@app.command("run")
def serve_installer_run(
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind to (default: localhost only)."),
    port: int = typer.Option(8080, "--port", "-p", help="Port to listen on."),
    open_browser: bool = typer.Option(True, "--open-browser/--no-browser", help="Open browser automatically."),
) -> None:
    """Start the AumOS GUI installer backend server.

    Launches a FastAPI server that the browser-based installer UI communicates
    with to orchestrate AumOS installation without CLI access.

    Args:
        host: Host address to bind (defaults to localhost for security).
        port: Port number.
        open_browser: Whether to open the browser automatically.
    """
    try:
        import uvicorn
    except ImportError:
        console.print("[red]Error:[/red] uvicorn is required for the GUI installer.")
        console.print("Install it with: pip install uvicorn")
        raise typer.Exit(code=1)

    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    from aumos_composable_installer.installer_api.router import installer_router

    fastapi_app = FastAPI(
        title="AumOS GUI Installer",
        description="Backend API for the AumOS browser-based GUI installer.",
        version="1.0.0",
    )

    fastapi_app.add_middleware(
        CORSMiddleware,
        allow_origins=[f"http://localhost:{port}", f"http://127.0.0.1:{port}"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    fastapi_app.include_router(installer_router)

    url = f"http://{host}:{port}"
    console.print(Panel(f"[bold cyan]AumOS GUI Installer[/bold cyan]\nListening at {url}", expand=False))
    console.print("Press Ctrl+C to stop.")

    if open_browser and host in ("127.0.0.1", "localhost"):
        import threading
        import webbrowser
        import time

        def _open() -> None:
            time.sleep(1.5)
            webbrowser.open(f"{url}/docs")

        threading.Thread(target=_open, daemon=True).start()

    uvicorn.run(fastapi_app, host=host, port=port, log_level="warning")
