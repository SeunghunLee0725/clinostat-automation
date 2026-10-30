"""근육 응답 전압 캡처 REST (2026-10-30). 상태는 `gateway.emg_monitor.EmgMonitor` 가 든다."""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from gateway.emg_monitor import EMG_MS_MAX, EMG_MS_MIN, EmgMonitor
from gateway.http_support import envelope

router = APIRouter()


class EmgCaptureRequest(BaseModel):
    ms: int = Field(ge=EMG_MS_MIN, le=EMG_MS_MAX)
    pga_mv: Optional[Literal[4096, 2048, 1024, 512, 256]] = None


class EmgStreamRequest(BaseModel):
    on: bool
    pga_mv: Optional[Literal[4096, 2048, 1024, 512, 256]] = None


def _monitor(request: Request) -> EmgMonitor:
    return request.app.state.emg


@router.get("/api/emg/latest")
async def emg_latest(request: Request):
    return envelope(request, _monitor(request).latest())


@router.post("/api/emg/capture")
async def emg_capture(request: Request, body: EmgCaptureRequest):
    return envelope(request, _monitor(request).request_capture(body.ms, body.pga_mv))


@router.get("/api/emg/stream")
async def emg_stream_read(request: Request):
    return envelope(request, _monitor(request).stream_snapshot())


@router.post("/api/emg/stream")
async def emg_stream_set(request: Request, body: EmgStreamRequest):
    return envelope(request, _monitor(request).set_stream(body.on, body.pga_mv))
