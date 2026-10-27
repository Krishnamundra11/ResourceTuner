"""Dashboard API: scenarios, runs, events, SSE stream, and read-only history.

Contract notes:

* `seq` is the global event cursor AND the SSE `id:`. A client that reconnects with
  `Last-Event-ID: <seq>` (the browser does this automatically) receives only `seq > N`, so
  reconnects neither lose nor repeat events.
* Every mutating path goes through `RunManager` → `VerticalSlice` → Safety Gate → Resource
  Controller. There is no endpoint that touches a container, a cgroup or a shell directly.
* `/api/history/*` is served by `dashboard/history.py`, which imports nothing that can act.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from resourcetuner.dashboard import history as H
from resourcetuner.events.store import EventStore
from resourcetuner.pipeline import stages as S
from resourcetuner.pipeline.runner import (
    MAX_CYCLES, InvalidRunRequest, RunConflict, RunManager, RunNotFound, available_scenarios,
)

SSE_POLL_S = 0.3
SSE_HEARTBEAT_S = 15.0
SSE_BATCH = 200


class StartRunRequest(BaseModel):
    scenario: str = Field(default="build_vs_indexer", max_length=100)
    level: str | None = Field(default=None, max_length=40)
    cycles: int = Field(default=1, ge=1, le=MAX_CYCLES)
    cooldown_s: float = Field(default=20.0, ge=0, le=600)
    baseline_duration: float = Field(default=37.38, gt=0, le=3600)
    trigger_wait_timeout_s: float = Field(default=600.0, ge=10, le=3600)
    start_scenario: bool = True


def build_router(store: EventStore, manager: RunManager, sampler: Any | None = None) -> APIRouter:
    router = APIRouter(prefix="/api")

    # --- live telemetry (unchanged contract, kept for the Overview view) -------------------

    @router.get("/snapshot")
    def snapshot() -> JSONResponse:
        if sampler is None:
            return JSONResponse({"status": "unavailable", "error": "no sampler in this app"}, status_code=503)
        payload, error = sampler.latest()
        if payload is None:
            return JSONResponse({"status": "warming_up", "error": error}, status_code=503)
        return JSONResponse({**payload, "sampler_error": error})

    # --- scenarios -------------------------------------------------------------------------

    @router.get("/scenarios")
    def scenarios() -> dict:
        return {"scenarios": available_scenarios(), "max_cycles": MAX_CYCLES}

    # --- runs ------------------------------------------------------------------------------

    @router.post("/runs", status_code=201)
    def start_run(body: StartRunRequest) -> dict:
        try:
            return manager.start(**body.model_dump())
        except RunConflict as e:
            raise HTTPException(status_code=409, detail={"error": "run_in_progress", "active": e.active,
                                                         "message": str(e)}) from e
        except InvalidRunRequest as e:
            raise HTTPException(status_code=422, detail={"error": "invalid_request", "message": str(e)}) from e

    @router.get("/runs")
    def list_runs(limit: Annotated[int, Query(ge=1, le=200)] = 50,
                  offset: Annotated[int, Query(ge=0)] = 0,
                  status: str | None = None) -> dict:
        return manager.list(limit=limit, offset=offset, status=status)

    @router.get("/runs/active")
    def active_run() -> dict:
        return {"active": manager.active()}

    @router.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        try:
            run = manager.get(run_id)
        except RunNotFound as e:
            raise HTTPException(status_code=404, detail={"error": "run_not_found", "run_id": run_id}) from e
        return {**run, "cycles_seen": store.cycles(run_id), "last_seq": store.last_seq()}

    @router.post("/runs/{run_id}/stop")
    def stop_run(run_id: str) -> dict:
        try:
            return manager.stop(run_id)
        except RunNotFound as e:
            raise HTTPException(status_code=404, detail={"error": "run_not_found", "run_id": run_id}) from e

    @router.get("/runs/{run_id}/events")
    def run_events(run_id: str,
                   after_seq: Annotated[int, Query(ge=0)] = 0,
                   limit: Annotated[int, Query(ge=1, le=2000)] = 500,
                   cycle: int | None = None) -> dict:
        try:
            manager.get(run_id)
        except RunNotFound as e:
            raise HTTPException(status_code=404, detail={"error": "run_not_found", "run_id": run_id}) from e
        events = store.events(run_id=run_id, after_seq=after_seq, limit=limit, cycle=cycle)
        return {"run_id": run_id, "events": events, "after_seq": after_seq,
                "next_seq": events[-1]["seq"] if events else after_seq,
                "stages": list(S.ALL_STAGES), "statuses": list(S.STATUSES)}

    # --- SSE -------------------------------------------------------------------------------

    @router.get("/events/stream")
    async def stream(request: Request, run_id: str | None = None,
                     after_seq: Annotated[int, Query(ge=0)] = 0) -> StreamingResponse:
        """Live events. Resumes from `Last-Event-ID` (preferred) or `?after_seq=`."""
        header = request.headers.get("last-event-id")
        try:
            cursor = int(header) if header is not None else after_seq
        except ValueError:
            cursor = after_seq

        async def gen():
            nonlocal cursor
            # A comment line flushes the response headers so the client's `onopen` fires at once.
            yield f": connected cursor={cursor}\n\n"
            last_beat = time.monotonic()
            while True:
                if await request.is_disconnected():
                    break
                rows = await asyncio.to_thread(store.events, run_id, cursor, SSE_BATCH)
                for row in rows:
                    cursor = row["seq"]
                    yield f"id: {cursor}\nevent: stage\ndata: {json.dumps(row)}\n\n"
                if rows:
                    last_beat = time.monotonic()
                elif time.monotonic() - last_beat >= SSE_HEARTBEAT_S:
                    # Keeps proxies from closing an idle stream and reveals dead clients.
                    yield ": heartbeat\n\n"
                    last_beat = time.monotonic()
                await asyncio.sleep(SSE_POLL_S)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        })

    # --- read-only history -----------------------------------------------------------------

    @router.get("/history/cycles")
    def history_cycles(limit: Annotated[int, Query(ge=1, le=200)] = 50,
                       offset: Annotated[int, Query(ge=0)] = 0) -> dict:
        return H.legacy_cycles(limit=limit, offset=offset)

    @router.get("/history/cycles/{cycle}")
    def history_cycle(cycle: int) -> dict:
        detail = H.legacy_cycle_detail(cycle)
        if detail is None:
            raise HTTPException(status_code=404, detail={"error": "cycle_not_found", "cycle": cycle})
        return detail

    @router.get("/history/actions")
    def history_actions(limit: Annotated[int, Query(ge=1, le=200)] = 50,
                        offset: Annotated[int, Query(ge=0)] = 0) -> dict:
        return H.actions(limit=limit, offset=offset)

    @router.get("/history/policies")
    def history_policies() -> dict:
        return H.policies()

    return router
