"""intervals.icu Gateway MCP — write workouts / schedule events / read fitness.

Boilerplate mirrors ~/ai-platform/mcps/openai_mcp/server.py (FastMCP + Starlette,
OAuth disabled, same transport security). Intervals.icu uses HTTP Basic auth
(username literal 'API_KEY', password = athlete api key).

Scope (Gateway Layer per ARCHITECTURE.md):
- create_workout / schedule_event / update_wellness → WRITE
- get_athlete_zones / list_workouts / get_fitness / get_wellness → READ

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

from intervals_gateway.otel import setup_otel
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
- create_note_event(date, note, name?) — add NOTE-category annotation to calendar
- update_event(event_id, note) — add/update note on an existing calendar event
- upload_activity(fit_path, start_time_local?, replace_existing?) — upload FIT,
  optionally replacing a Zwift-uploaded version in the same time window
- update_wellness(date, ...) — daily wellness: subjective 1-4 scores, sleep,
  resting HR / HRV / weight, free-text comments

Read tools:
- get_athlete_zones() — power / HR / pace zones per sport
- list_workouts(limit?) — library workouts
- get_fitness(days?) — Fitness / Fatigue / Form history
- list_events(start_date, end_date) — list calendar events between dates
- list_activities(start_date, end_date) — list uploaded activities between dates
- get_wellness(date) / list_wellness(start_date, end_date) — daily wellness records

Subjective wellness scores are 1-4 where **1 = best, 4 = worst** (verified against
126 days of Garmin-synced sleepQuality vs sleepScore: q1 -> score 90-96,
q4 -> score 37-59). Do not invert.
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
    async def list_events(start_date: str, end_date: str) -> dict:
        """
        List calendar events between dates.

        Args:
            start_date: YYYY-MM-DD (inclusive).
            end_date: YYYY-MM-DD (inclusive).

        Returns: {"count": N, "events": [...]} each event has id, name, type, start_date_local, description.
        """
        data = await _req(
            "GET",
            f"/athlete/{athlete_id}/events",
            params={"oldest": start_date, "newest": end_date},
        )
        slim = [
            {k: e.get(k) for k in ("id", "name", "type", "start_date_local", "description") if k in e}
            for e in (data if isinstance(data, list) else [])
        ]
        return {"count": len(slim), "events": slim}

    @app.tool()
    async def create_note_event(date: str, note: str, name: str = "主觀體感") -> dict:
        """
        Create a NOTE-category calendar event on a specific date.
        Use this to record subjective training feedback, daily state, or any
        non-workout annotation on the intervals.icu calendar.

        Args:
            date: YYYY-MM-DD.
            note: Text content of the note.
            name: Event title (default "主觀體感").

        Returns: created event object.
        """
        payload: dict[str, Any] = {
            "start_date_local": f"{date}T00:00:00",
            "category": "NOTE",
            "name": name,
            "description": note,
        }
        return await _req("POST", f"/athlete/{athlete_id}/events", json=payload)

    @app.tool()
    async def update_event(event_id: int, note: str) -> dict:
        """
        Add or replace the description/note on an existing calendar event.

        Args:
            event_id: intervals.icu event id (from list_events or schedule_event response).
            note: Text to set as the event description (replaces existing).

        Returns: updated event object.
        """
        return await _req(
            "PUT",
            f"/athlete/{athlete_id}/events/{event_id}",
            json={"description": note},
        )

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
    async def list_activities(start_date: str, end_date: str) -> dict:
        """
        List uploaded activities (not planned events) between dates.

        Args:
            start_date: YYYY-MM-DD (inclusive).
            end_date: YYYY-MM-DD (inclusive).

        Returns: {"count": N, "activities": [...]} with id/start_date_local/type/source/name.
        """
        data = await _req(
            "GET",
            f"/athlete/{athlete_id}/activities",
            params={"oldest": start_date, "newest": end_date},
        )
        slim = [
            {k: a.get(k) for k in ("id", "start_date_local", "type", "source", "name", "moving_time") if k in a}
            for a in (data if isinstance(data, list) else [])
        ]
        return {"count": len(slim), "activities": slim}

    @app.tool()
    async def upload_activity(
        fit_path: str,
        start_time_local: str | None = None,
        replace_existing: bool = True,
        overlap_window_seconds: int = 120,
    ) -> dict:
        """
        Upload a FIT file to intervals.icu, optionally replacing a
        Zwift-uploaded duplicate in the same start-time window.

        Args:
            fit_path: Absolute path to .fit readable from this container
                (typically /data/fit-cache/... written by garmin-mcp).
            start_time_local: ISO-local start time (e.g. "2026-04-16T05:48:19").
                If provided, enables overlap detection against existing
                activities. If omitted, upload-only without replacement.
            replace_existing: Delete overlapping activity before upload (default True).
            overlap_window_seconds: How close in start time counts as same activity.

        Returns: {uploaded_activity_id, replaced_activity_id?, fit_path, ...}.
        """
        from datetime import datetime, timedelta
        from pathlib import Path

        p = Path(fit_path)
        if not p.exists():
            raise RuntimeError(f"FIT file not found: {fit_path}")

        replaced_id: str | None = None
        replaced_source: str | None = None

        if start_time_local and replace_existing:
            target_ts = datetime.fromisoformat(start_time_local)
            window_start = (target_ts - timedelta(seconds=overlap_window_seconds)).date().isoformat()
            window_end = (target_ts + timedelta(seconds=overlap_window_seconds)).date().isoformat()
            existing = await _req(
                "GET",
                f"/athlete/{athlete_id}/activities",
                params={"oldest": window_start, "newest": window_end},
            )
            for act in (existing if isinstance(existing, list) else []):
                raw_start = act.get("start_date_local")
                if not raw_start:
                    continue
                try:
                    act_ts = datetime.fromisoformat(raw_start)
                except ValueError:
                    continue
                if abs((act_ts - target_ts).total_seconds()) <= overlap_window_seconds:
                    act_id = act.get("id")
                    r = await client.delete(f"/activity/{act_id}")
                    r.raise_for_status()
                    replaced_id = act_id
                    replaced_source = act.get("source")
                    break

        with p.open("rb") as f:
            files = {"file": (p.name, f, "application/octet-stream")}
            r = await client.post(f"/athlete/{athlete_id}/activities", files=files)
        r.raise_for_status()
        payload = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text}

        uploaded_id = payload.get("id") if isinstance(payload, dict) else None

        return {
            "uploaded_activity_id": uploaded_id,
            "replaced_activity_id": replaced_id,
            "replaced_source": replaced_source,
            "fit_path": str(p),
            "size_bytes": p.stat().st_size,
            "upload_response": payload,
        }

    _WELLNESS_FIELDS = (
        "id", "weight", "restingHR", "hrv", "sleepSecs", "sleepScore",
        "sleepQuality", "soreness", "fatigue", "stress", "mood", "motivation",
        "injury", "spO2", "readiness", "comments", "ctl", "atl",
    )

    @app.tool()
    async def get_wellness(date: str) -> dict:
        """
        Read one day's wellness record.

        Args:
            date: YYYY-MM-DD (local date).

        Returns: wellness fields including weight / restingHR / hrv / sleepSecs /
        sleepScore / subjective 1-4 scores / comments / ctl / atl.
        Fields Garmin has not synced come back as null.
        """
        data = await _req("GET", f"/athlete/{athlete_id}/wellness/{date}")
        return {k: data.get(k) for k in _WELLNESS_FIELDS if k in data}

    @app.tool()
    async def list_wellness(start_date: str, end_date: str) -> dict:
        """
        List wellness records between dates (for trend analysis).

        Args:
            start_date: YYYY-MM-DD (inclusive).
            end_date: YYYY-MM-DD (inclusive).

        Returns: {"count": N, "days": [...]}.
        """
        data = await _req(
            "GET",
            f"/athlete/{athlete_id}/wellness",
            params={"oldest": start_date, "newest": end_date},
        )
        slim = [
            {k: d.get(k) for k in _WELLNESS_FIELDS if k in d}
            for d in (data if isinstance(data, list) else [])
        ]
        return {"count": len(slim), "days": slim}

    @app.tool()
    async def update_wellness(
        date: str,
        soreness: int | None = None,
        fatigue: int | None = None,
        stress: int | None = None,
        mood: int | None = None,
        motivation: int | None = None,
        injury: int | None = None,
        sleep_secs: int | None = None,
        sleep_score: float | None = None,
        sleep_quality: int | None = None,
        resting_hr: int | None = None,
        hrv: float | None = None,
        weight: float | None = None,
        spo2: int | None = None,
        comments: str | None = None,
        clear: list[str] | None = None,
    ) -> dict:
        """
        Write daily wellness. This is the SSoT for subjective daily state —
        record it here, not in markdown.

        Only the arguments you pass are written; omitted fields keep their
        current value (typically whatever Garmin synced). To blank a field
        that should not have been set, list it in `clear`.

        **Subjective scores are 1-4 where 1 = best and 4 = worst.** Verified
        empirically, do not invert:
            soreness    1 none      -> 4 severe
            fatigue     1 fresh     -> 4 exhausted
            stress      1 relaxed   -> 4 very stressed
            mood        1 great     -> 4 terrible
            motivation  1 high      -> 4 none
            injury      1 none      -> 4 severe
        Pass only what the athlete actually reported. Never guess a score they
        did not give — leave it out.

        Args:
            date: YYYY-MM-DD (local date).
            sleep_secs: Total sleep in seconds. Overwrites the Garmin-synced
                value — use when Garmin's sleep-onset detection is verifiably
                wrong, and say so in comments so the edit is auditable.
            sleep_score: 0-100. sleep_quality: 1-4 (see scale above).
            resting_hr / hrv / weight / spo2: objective measurements.
            comments: Free text — session detail, symptoms, why a value was
                corrected. Use this for anything that has no dedicated field
                (e.g. a 0-10 pain scale, GERD status, water temperature).
            clear: Field names to set back to null, e.g. ["fatigue", "mood"].
                Use camelCase API names (sleepSecs, restingHR, spO2).

        Returns: the updated wellness record.
        """
        payload: dict[str, Any] = {"id": date}
        for key, value in (
            ("soreness", soreness),
            ("fatigue", fatigue),
            ("stress", stress),
            ("mood", mood),
            ("motivation", motivation),
            ("injury", injury),
            ("sleepSecs", sleep_secs),
            ("sleepScore", sleep_score),
            ("sleepQuality", sleep_quality),
            ("restingHR", resting_hr),
            ("hrv", hrv),
            ("weight", weight),
            ("spO2", spo2),
            ("comments", comments),
        ):
            if value is not None:
                payload[key] = value

        for key in ("soreness", "fatigue", "stress", "mood", "motivation", "injury", "sleepQuality"):
            v = payload.get(key)
            if v is not None and v != -1 and not 1 <= int(v) <= 4:
                raise ValueError(f"{key} must be 1-4 (1 = best, 4 = worst), got {v}")

        # intervals.icu PUT 語意是「只改有提供的欄位」，null 視同未提供；
        # 官方清空慣例是送 -1（實測 2026-08-06：PUT null 不變、PUT -1 → null）。
        _CLEARABLE = {
            "soreness", "fatigue", "stress", "mood", "motivation", "injury",
            "sleepSecs", "sleepScore", "sleepQuality", "restingHR", "hrv",
            "weight", "spO2",
        }
        for key in clear or []:
            if key not in _CLEARABLE:
                raise ValueError(f"Cannot clear '{key}' — clearable: {sorted(_CLEARABLE)}")
            payload[key] = -1

        if len(payload) == 1:
            raise ValueError("Nothing to update — pass at least one field besides date")

        data = await _req("PUT", f"/athlete/{athlete_id}/wellness/{date}", json=payload)
        return {k: data.get(k) for k in _WELLNESS_FIELDS if k in data}

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
    setup_otel()
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
