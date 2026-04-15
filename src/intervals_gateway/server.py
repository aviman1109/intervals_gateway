"""intervals.icu Gateway MCP — write workouts / schedule events / read fitness.

Boilerplate mirrors ~/ai-platform/mcps/openai_mcp/server.py (FastMCP + Starlette,
OAuth disabled, same transport security). Intervals.icu uses HTTP Basic auth
(username literal 'API_KEY', password = athlete api key).

Scope (Gateway Layer per ARCHITECTURE.md):
- create_workout / schedule_event → WRITE
- get_athlete_zones / list_workouts / get_fitness → READ

Deliberately NOT included: activity read (that's garmin-reader's job),
athlete profile edit, custom event types beyond WORKOUT.
"""
from __future__ import annotations

import os
import sys
from typing import Any

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route


SERVER_INSTRUCTIONS = """
intervals.icu Gateway — write workouts and events to intervals.icu.

intervals.icu auto-syncs to Zwift iPad + Garmin Connect, so workouts written
here appear on both platforms without touching Zwift/Garmin APIs directly.

Write tools:
- create_workout(name, description, sport?, folder_id?) — intervals DSL description
- schedule_event(workout_id, date, name?) — schedule onto calendar

Read tools:
- get_athlete_zones() — power / HR / pace zones per sport
- list_workouts(limit?) — library workouts
- get_fitness(days?) — Fitness / Fatigue / Form history
""".strip()


BASE_URL = "https://intervals.icu/api/v1"


def _csv(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


async def _oauth_disabled(_req):
    return JSONResponse({"error": "OAuth disabled"}, status_code=404)


def build_app() -> FastMCP:
    athlete_id = os.environ.get("INTERVALS_ATHLETE_ID")
    api_key = os.environ.get("INTERVALS_API_KEY")
    if not athlete_id or not api_key:
        raise RuntimeError("INTERVALS_ATHLETE_ID and INTERVALS_API_KEY must be set")

    client = httpx.AsyncClient(
        base_url=BASE_URL,
        auth=("API_KEY", api_key),
        timeout=20.0,
    )

    app = FastMCP(name="intervals-gateway", instructions=SERVER_INSTRUCTIONS)
    app.settings.streamable_http_path = os.getenv("MCP_PATH", "/mcp")
    app.settings.stateless_http = True
    app.settings.json_response = True
    app.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_csv(os.getenv("MCP_ALLOWED_HOSTS")) or ["127.0.0.1:*", "localhost:*"],
        allowed_origins=_csv(os.getenv("MCP_ALLOWED_ORIGINS")) or ["http://127.0.0.1:*", "http://localhost:*"],
    )

    async def _req(method: str, path: str, **kw) -> Any:
        r = await client.request(method, path, **kw)
        r.raise_for_status()
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return r.text

    async def _default_folder_id() -> int:
        folders = await _req("GET", f"/athlete/{athlete_id}/folders")
        if not folders:
            raise RuntimeError("No folders in intervals.icu library — create one first")
        return folders[0]["id"]

    @app.tool()
    async def create_workout(
        name: str,
        description: str,
        sport: str = "Ride",
        folder_id: int | None = None,
    ) -> dict:
        """
        Create a workout in intervals.icu library.

        Args:
            name: Workout name.
            description: intervals DSL body (e.g. "- 10m 60%\\n- 3x\\n  - 8m 95%\\n  - 2m 50%").
                See https://intervals.icu/workout for DSL syntax.
            sport: Ride / Run / Swim / WeightTraining / ... (default Ride).
            folder_id: Library folder id; defaults to first folder (usually "Workouts").

        Returns: created workout object.
        """
        if folder_id is None:
            folder_id = await _default_folder_id()
        payload: dict[str, Any] = {
            "name": name,
            "description": description,
            "type": sport,
            "folder_id": folder_id,
        }
        return await _req("POST", f"/athlete/{athlete_id}/workouts", json=payload)

    @app.tool()
    async def schedule_event(
        workout_id: int,
        date: str,
        name: str | None = None,
    ) -> dict:
        """
        Schedule a library workout onto a calendar date.

        intervals.icu events don't auto-inherit from workout_id; we fetch the
        workout and inline name/description/type into the event body.

        Args:
            workout_id: intervals.icu workout id.
            date: YYYY-MM-DD (local date).
            name: Override event name (defaults to workout name).

        Returns: created event object.
        """
        wo = await _req("GET", f"/athlete/{athlete_id}/workouts/{workout_id}")
        payload: dict[str, Any] = {
            "start_date_local": f"{date}T00:00:00",
            "category": "WORKOUT",
            "workout_id": workout_id,
            "type": wo["type"],
            "name": name or wo["name"],
            "description": wo.get("description", ""),
        }
        return await _req("POST", f"/athlete/{athlete_id}/events", json=payload)

    @app.tool()
    async def get_athlete_zones() -> dict:
        """
        Read zones for all sports (power / HR / pace thresholds).

        Returns: {"athlete_id": ..., "sports": [sportSettings...]}.
        sportSettings fields include: types, ftp, lthr, power_zones, hr_zones,
        pace_zones, threshold_pace, etc.
        """
        data = await _req("GET", f"/athlete/{athlete_id}")
        return {
            "athlete_id": athlete_id,
            "sports": data.get("sportSettings", []),
        }

    @app.tool()
    async def list_workouts(limit: int = 20) -> dict:
        """List workouts in the athlete's library."""
        data = await _req("GET", f"/athlete/{athlete_id}/workouts", params={"limit": limit})
        return {"count": len(data), "workouts": data}

    @app.tool()
    async def get_fitness(days: int = 30) -> dict:
        """
        Fitness / Fatigue / Form history (CTL / ATL / TSB).

        Args:
            days: Trailing N days (default 30).

        Returns: {"days": [...wellness entries with ctl/atl/form...]}
        """
        from datetime import date, timedelta
        newest = date.today()
        oldest = newest - timedelta(days=days)
        data = await _req(
            "GET",
            f"/athlete/{athlete_id}/wellness",
            params={"oldest": oldest.isoformat(), "newest": newest.isoformat()},
        )
        slim = [
            {k: d.get(k) for k in ("id", "ctl", "atl", "ctlLoad", "atlLoad", "rampRate") if k in d}
            for d in data
        ]
        return {"athlete_id": athlete_id, "days": slim}

    return app


def main() -> None:
    app = build_app()
    transport = os.getenv("MCP_TRANSPORT", "http").lower()
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "38104"))

    print(f"intervals-gateway on {host}:{port}", file=sys.stderr)

    if transport == "stdio":
        app.run()
        return

    http_app = app.streamable_http_app()
    root = Starlette(
        lifespan=getattr(http_app.router, "lifespan_context", None),
        routes=[
            Route("/.well-known/oauth-protected-resource", _oauth_disabled, methods=["GET"]),
            Route("/.well-known/oauth-authorization-server", _oauth_disabled, methods=["GET"]),
            Mount("", app=http_app),
        ],
    )
    uvicorn.run(root, host=host, port=port)


if __name__ == "__main__":
    main()
