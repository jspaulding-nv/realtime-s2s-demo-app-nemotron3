"""FastAPI backend for real-time speech-to-speech translation."""

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from config import SUPPORTED_LANGUAGES, audio_config
from riva_client import riva_client
from websocket_handler import session_manager, SessionStatus
from timing_logger import timing_logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup: connect to Riva
    print("Connecting to Riva services...")
    if riva_client.connect():
        print("Connected to Riva successfully")
    else:
        print("Warning: Failed to connect to Riva - will retry on first request")

    yield

    # Shutdown: disconnect from Riva
    print("Disconnecting from Riva...")
    riva_client.disconnect()


app = FastAPI(
    title="Real-Time Speech-to-Speech Translation",
    description="WebSocket API for real-time translation using NVIDIA Riva",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS configuration for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    """Health check endpoint."""
    return {
        "status": "ok",
        "service": "speech-to-speech-translation",
        "riva_connected": riva_client.is_connected(),
    }


@app.get("/api/languages")
async def get_languages():
    """Get list of supported target languages."""
    languages = []
    for code, config in SUPPORTED_LANGUAGES.items():
        languages.append({
            "code": code,
            "name": config["name"],
            "available": config["available"],
        })
    return {"languages": languages}


@app.get("/api/config")
async def get_config():
    """Get audio configuration for frontend."""
    return {
        "sampleRate": audio_config.sample_rate,
        "chunkSize": audio_config.chunk_size,
        "channels": audio_config.channels,
    }


@app.post("/api/test/start")
async def test_start():
    """Start a latency measurement test session."""
    timing_logger.start_test()
    return {"status": "started"}


@app.post("/api/test/stop")
async def test_stop():
    """Stop the current latency measurement test session."""
    timing_logger.stop_test()
    return {"status": "stopped"}


@app.get("/api/test/export")
async def test_export():
    """Export all timing events from the current or last test session."""
    return {"events": timing_logger.get_all_events()}


@app.websocket("/ws/metrics")
async def websocket_metrics(websocket: WebSocket):
    """WebSocket endpoint for real-time timing event streaming."""
    await websocket.accept()
    queue = timing_logger.subscribe()
    try:
        while True:
            event = await queue.get()
            await websocket.send_json({
                "stage": event.stage,
                "timestamp": event.timestamp,
                "chunk_index": event.chunk_index,
                "source_position_sec": event.source_position_sec,
                "audio_bytes_len": event.audio_bytes_len,
                "wall_clock": event.wall_clock,
            })
    except Exception:
        pass  # client disconnected
    finally:
        timing_logger.unsubscribe(queue)


@app.websocket("/ws/translate")
async def websocket_translate(websocket: WebSocket):
    """WebSocket endpoint for real-time translation."""
    await websocket.accept()

    # Create session
    session = await session_manager.create_session(websocket)
    if not session:
        await websocket.send_json({
            "type": "error",
            "message": "Failed to connect to Riva services"
        })
        await websocket.close()
        return

    await session.send_status(SessionStatus.CONNECTED, "Connected to translation service")

    try:
        while True:
            # Receive message (can be text or binary)
            message = await websocket.receive()

            if message["type"] == "websocket.disconnect":
                break

            if "text" in message:
                # JSON control message
                data = json.loads(message["text"])
                await handle_control_message(session, data)

            elif "bytes" in message:
                # Binary audio data
                await session.process_audio(message["bytes"])

    except WebSocketDisconnect:
        print("Client disconnected")
    except Exception as e:
        print(f"WebSocket error: {e}")
        await session.send_error(str(e))
    finally:
        await session_manager.remove_session(session)


async def handle_control_message(session, data: dict) -> None:
    """Handle JSON control messages from client."""
    msg_type = data.get("type")

    if msg_type == "start_stream":
        target_language = data.get("targetLanguage", "es-US")
        await session.start_stream(target_language)

    elif msg_type == "stop_stream":
        await session.stop_stream()

    elif msg_type == "ping":
        await session.websocket.send_json({"type": "pong"})

    else:
        await session.send_error(f"Unknown message type: {msg_type}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
