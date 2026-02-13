# Speech-to-Speech Lag Testing System - Implementation Plan

## CONTEXT FOR CLAUDE CODE

This is a detailed implementation plan for adding lag/drift measurement capabilities to an existing WebSocket-based Speech-to-Speech translation system. The system currently streams audio from browser → backend (FastAPI) → Nvidia Riva (gRPC) → backend → browser. We need to measure cumulative drift over 30+ minute sessions without playing audio through speakers.

**Repository:** https://github.com/jgough-essextec/realtime-s2s-demo-app

**Current Tech Stack (assumed):**
- Frontend: React + TypeScript + Web Audio API
- Backend: FastAPI (Python) + WebSockets + gRPC (for Riva)
- Audio: 16kHz mono PCM, 300ms chunks (4800 samples)

---

## PHASE 1: CODEBASE EVALUATION

### 1.1 Frontend Analysis

**Objective:** Verify we can replace live microphone input with pre-recorded file playback and inject timing metadata.

**Files to Examine:**
```
src/
  components/
    - AudioRecorder.tsx / AudioCapture.tsx / MicrophoneInput.tsx (or similar)
  hooks/
    - useAudioStream.ts / useWebSocket.ts (or similar)
  utils/
    - audioUtils.ts / webSocketClient.ts (or similar)
```

**Questions to Answer:**

1. **Audio Input Architecture:**
   - How is `navigator.mediaDevices.getUserMedia()` currently used?
   - Where are audio chunks extracted from the MediaStream?
   - What AudioContext/AudioWorklet patterns are used?
   - Is there a ScriptProcessorNode or AudioWorkletProcessor?

2. **File Playback Capability:**
   - Can we use `<audio>` element + MediaElementSourceNode?
   - Or do we need AudioBufferSourceNode with manual chunking?
   - How do we simulate real-time streaming from a file?

3. **Chunking Logic:**
   - Where is the 4800-sample chunk size enforced?
   - How are chunks converted to binary for WebSocket?
   - Can we inject sequence numbers without breaking existing protocol?

4. **WebSocket Message Format:**
   - Current structure: `{type: 'audio', data: ArrayBuffer}` or similar?
   - Can we extend to: `{type: 'audio', data: ArrayBuffer, seq: number, timestamp: number}`?
   - Are there TypeScript interfaces we need to update?

5. **Audio Playback (Output):**
   - How is translated audio currently played?
   - Is there an AudioContext destination we can disconnect?
   - Can we write to a "silent" audio context or null destination?

**Required Capabilities:**
- ✅ Replace MediaStream with file-based audio source
- ✅ Maintain exact 300ms chunking behavior
- ✅ Add metadata to WebSocket messages
- ✅ Disable speaker output while keeping processing active
- ✅ Log received audio timestamps without playback

---

### 1.2 Backend Analysis

**Objective:** Verify we can inject timing logs at all gRPC and WebSocket touchpoints without breaking the streaming pipeline.

**Files to Examine:**
```
app/
  main.py (FastAPI app initialization)
  routes/
    - websocket.py / s2s.py (WebSocket endpoint)
  services/
    - riva_client.py / translation_service.py (gRPC to Riva)
  models/
    - audio_models.py (data structures)
```

**Questions to Answer:**

1. **WebSocket Handler:**
   - Where does `websocket.receive_bytes()` happen?
   - Is there a message loop we can inject timing logs into?
   - Can we preserve sequence numbers from frontend?

2. **Riva gRPC Integration:**
   - What's the exact gRPC method? (likely `StreamingTranslateSpeechToSpeech`)
   - How is the iterator queue implemented?
   - Where do chunks get sent to Riva?
   - Where do translated chunks come back from Riva?

3. **Timing Injection Points:**
   - **Point A:** WebSocket receive (when chunk arrives from frontend)
   - **Point B:** gRPC send (when chunk sent to Riva)
   - **Point C:** gRPC receive (when translated chunk comes back)
   - **Point D:** WebSocket send (when chunk sent to frontend)
   - Can we log all 4 without blocking the async streams?

4. **Logging Strategy:**
   - Should we use Python's `logging` module or something more structured?
   - Can we use `structlog` or write to JSON logs?
   - Need separate log stream for timing data vs. application logs?

5. **Metrics Export:**
   - Should we create a separate WebSocket endpoint for real-time metrics?
   - Or write to a file/database that dashboard polls?
   - Can we use FastAPI's background tasks?

**Required Capabilities:**
- ✅ Access to all 4 timing measurement points
- ✅ Non-blocking logging that doesn't affect stream latency
- ✅ Associate sequence numbers across the full pipeline
- ✅ Export timing data to dashboard (WebSocket or HTTP endpoint)

---

### 1.3 Library Compatibility Check

**Frontend Libraries to Verify:**
```bash
# Check package.json for:
- Web Audio API capabilities (built-in, but check polyfills)
- WebSocket library (native WebSocket or socket.io?)
- Audio encoding libraries (if any)
```

**Backend Libraries to Verify:**
```bash
# Check requirements.txt or pyproject.toml for:
- fastapi
- websockets or starlette (WebSocket support)
- grpcio (gRPC client)
- nvidia-riva-client (if using official SDK)
- asyncio capabilities (Python 3.8+)
```

**Potential Issues to Identify:**
- WebSocket library version compatibility with metadata extensions
- gRPC async stream handling in Python
- Audio file decoding libraries (ffmpeg, pydub, soundfile)
- Timestamp precision (JavaScript's `performance.now()` vs Python's `time.perf_counter()`)

---

## PHASE 2: EPICS AND STORIES

### Epic 1: Test Infrastructure Setup

**Story 1.1: Prepare Test Audio File**
- **Acceptance Criteria:**
  - Obtain or create a 30-minute audio file (MP3/MP4)
  - Convert to 16kHz mono WAV format
  - Validate audio quality and duration
  - Store in `/test-assets/` directory
- **Technical Notes:**
  - Use `ffmpeg` for conversion: `ffmpeg -i input.mp3 -ar 16000 -ac 1 output.wav`
  - Verify with `ffprobe`

**Story 1.2: Create Timing Data Models**
- **Acceptance Criteria:**
  - Define TypeScript interfaces for timing metadata
  - Define Python dataclasses/Pydantic models for timing logs
  - Ensure sequence number + timestamp schema
- **Technical Notes:**
  ```typescript
  interface TimedAudioChunk {
    type: 'audio';
    data: ArrayBuffer;
    seq: number;
    timestamp: number;  // performance.now()
    sourcePosition: number;  // seconds into source file
  }
  ```
  ```python
  from pydantic import BaseModel
  
  class TimingEvent(BaseModel):
      seq: int
      stage: str  # 'ws_receive', 'grpc_send', 'grpc_receive', 'ws_send'
      timestamp: float  # time.perf_counter()
      source_position: float  # seconds
  ```

---

### Epic 2: Frontend Testing Mode

**Story 2.1: Implement File-Based Audio Source**
- **Acceptance Criteria:**
  - Create `FileAudioSource` component/hook
  - Load WAV file into AudioBuffer
  - Stream from buffer in 300ms chunks (4800 samples)
  - Maintain real-time playback speed
  - Track current position in source file
- **Technical Notes:**
  - Use `AudioContext.decodeAudioData()`
  - Use `AudioBufferSourceNode` or manual chunking with `setInterval`
  - Calculate `sourcePosition = currentSample / sampleRate`

**Story 2.2: Add Testing Mode Toggle**
- **Acceptance Criteria:**
  - Add UI toggle: "Live Mic" vs "Test Mode"
  - When Test Mode enabled:
    - Show file upload or use default test file
    - Replace mic stream with file stream
    - Add sequence numbers to each chunk
    - Display current test progress (time elapsed)
- **Technical Notes:**
  - Use React state: `const [testMode, setTestMode] = useState(false)`
  - Conditional rendering based on mode

**Story 2.3: Inject Timing Metadata in WebSocket Messages**
- **Acceptance Criteria:**
  - Add `seq` (auto-incrementing) to each audio message
  - Add `timestamp` (`performance.now()`) when chunk is sent
  - Add `sourcePosition` (seconds into source file)
  - Update WebSocket send logic
- **Technical Notes:**
  ```typescript
  let sequenceNumber = 0;
  const sendAudioChunk = (audioData: Float32Array, sourcePos: number) => {
    const message = {
      type: 'audio',
      data: audioData.buffer,
      seq: sequenceNumber++,
      timestamp: performance.now(),
      sourcePosition: sourcePos
    };
    websocket.send(JSON.stringify(message));
  };
  ```

**Story 2.4: Silent Output Mode**
- **Acceptance Criteria:**
  - Receive translated audio chunks normally
  - Log timestamps and sequence numbers
  - Do NOT play audio through speakers
  - Option: write to disconnected AudioContext
- **Technical Notes:**
  ```typescript
  // Create silent audio context
  const silentContext = new AudioContext();
  const silentDestination = silentContext.createMediaStreamDestination();
  // Connect source to silent destination instead of speakers
  source.connect(silentDestination);
  ```

**Story 2.5: Frontend Timing Logger**
- **Acceptance Criteria:**
  - Log when each chunk is sent: `{seq, sentAt, sourcePosition}`
  - Log when each translated chunk is received: `{seq, receivedAt}`
  - Store logs in array or IndexedDB
  - Expose logs via React context or global state
- **Technical Notes:**
  ```typescript
  const timingLogs: TimingEvent[] = [];
  
  // On send
  timingLogs.push({
    seq: message.seq,
    stage: 'frontend_send',
    timestamp: performance.now(),
    sourcePosition: message.sourcePosition
  });
  
  // On receive
  timingLogs.push({
    seq: receivedMessage.seq,
    stage: 'frontend_receive',
    timestamp: performance.now()
  });
  ```

---

### Epic 3: Backend Instrumentation

**Story 3.1: Add Timing Middleware to WebSocket Handler**
- **Acceptance Criteria:**
  - Log timestamp when audio chunk received from frontend
  - Extract and preserve `seq` and `sourcePosition`
  - Store in per-connection timing dictionary
- **Technical Notes:**
  ```python
  import time
  
  @app.websocket("/ws/s2s")
  async def websocket_endpoint(websocket: WebSocket):
      await websocket.accept()
      timing_logs = []
      
      async for message in websocket.iter_json():
          if message['type'] == 'audio':
              timing_logs.append({
                  'seq': message['seq'],
                  'stage': 'ws_receive',
                  'timestamp': time.perf_counter(),
                  'source_position': message['sourcePosition']
              })
              # Continue processing...
  ```

**Story 3.2: Instrument Riva gRPC Send**
- **Acceptance Criteria:**
  - Log timestamp just before sending chunk to Riva
  - Associate with sequence number
- **Technical Notes:**
  ```python
  # In streaming_s2s_response_generator or similar
  def generate_requests():
      for chunk in audio_chunks:
          timing_logs.append({
              'seq': chunk.seq,
              'stage': 'grpc_send',
              'timestamp': time.perf_counter()
          })
          yield riva_request
  ```

**Story 3.3: Instrument Riva gRPC Receive**
- **Acceptance Criteria:**
  - Log timestamp when translated chunk received from Riva
  - Match to original sequence number (may need tracking dict)
- **Technical Notes:**
  ```python
  responses = riva_client.streaming_s2s(generate_requests())
  for response in responses:
      timing_logs.append({
          'seq': response.seq,  # Need to track seq through Riva
          'stage': 'grpc_receive',
          'timestamp': time.perf_counter()
      })
  ```

**Story 3.4: Instrument WebSocket Send**
- **Acceptance Criteria:**
  - Log timestamp just before sending translated chunk to frontend
- **Technical Notes:**
  ```python
  timing_logs.append({
      'seq': chunk.seq,
      'stage': 'ws_send',
      'timestamp': time.perf_counter()
  })
  await websocket.send_bytes(translated_audio)
  ```

**Story 3.5: Create Metrics Export Endpoint**
- **Acceptance Criteria:**
  - New WebSocket endpoint: `/ws/metrics`
  - Stream timing events in real-time
  - Or HTTP endpoint: `GET /metrics` returns JSON array
- **Technical Notes:**
  ```python
  @app.websocket("/ws/metrics")
  async def metrics_endpoint(websocket: WebSocket):
      await websocket.accept()
      while True:
          if timing_logs:
              await websocket.send_json(timing_logs)
              timing_logs.clear()
          await asyncio.sleep(1)
  ```

---

### Epic 4: Lag Analysis Dashboard

**Story 4.1: Create Dashboard React App**
- **Acceptance Criteria:**
  - Separate React app or new page in existing app
  - Connect to `/ws/metrics` WebSocket
  - Receive timing events in real-time
- **Technical Notes:**
  - Use `recharts` or `chart.js` for visualization
  - WebSocket client to metrics endpoint

**Story 4.2: Calculate Cumulative Drift**
- **Acceptance Criteria:**
  - For each received chunk, calculate:
    - `elapsedTime = currentTime - testStartTime`
    - `expectedPosition = sourcePosition` (from original chunk)
    - `drift = elapsedTime - expectedPosition`
  - Store drift values over time
- **Technical Notes:**
  ```typescript
  const calculateDrift = (event: TimingEvent) => {
    const testStartTime = timingEvents[0].timestamp;
    const elapsedTime = (event.timestamp - testStartTime) / 1000; // Convert to seconds
    const drift = elapsedTime - event.sourcePosition;
    return drift;
  };
  ```

**Story 4.3: Real-Time Drift Graph**
- **Acceptance Criteria:**
  - Line chart: X-axis = elapsed time (0-30 min), Y-axis = drift (seconds)
  - Update in real-time as data arrives
  - Color-code: green (<5s), yellow (5-20s), red (>20s)
- **Technical Notes:**
  ```tsx
  <LineChart data={driftData}>
    <XAxis dataKey="elapsedTime" label="Elapsed Time (min)" />
    <YAxis dataKey="drift" label="Cumulative Drift (s)" />
    <Line type="monotone" dataKey="drift" stroke="#8884d8" />
    <ReferenceLine y={20} stroke="red" label="Warning Threshold" />
  </LineChart>
  ```

**Story 4.4: Latency Breakdown View**
- **Acceptance Criteria:**
  - Show average latency for each stage:
    - Frontend → Backend (WebSocket)
    - Backend → Riva (gRPC send)
    - Riva processing time (gRPC roundtrip)
    - Backend → Frontend (WebSocket)
  - Display as stacked bar chart or table
- **Technical Notes:**
  ```typescript
  const calculateStageLatencies = (logs: TimingEvent[]) => {
    const grouped = groupBy(logs, 'seq');
    return Object.values(grouped).map(events => ({
      wsIngress: events.find(e => e.stage === 'ws_receive'),
      grpcSend: events.find(e => e.stage === 'grpc_send'),
      grpcReceive: events.find(e => e.stage === 'grpc_receive'),
      wsEgress: events.find(e => e.stage === 'ws_send')
    }));
  };
  ```

**Story 4.5: Export Test Results**
- **Acceptance Criteria:**
  - Button to export timing data as CSV
  - Include: seq, all 4 timestamps, calculated drift
  - Download to local file
- **Technical Notes:**
  ```typescript
  const exportCSV = () => {
    const csv = timingLogs.map(log => 
      `${log.seq},${log.stage},${log.timestamp},${log.sourcePosition}`
    ).join('\n');
    downloadFile('timing-data.csv', csv);
  };
  ```

---

## PHASE 3: CORE IMPLEMENTATION PLAN

### 3.1 Architecture Changes

```
┌─────────────────────────────────────────────────────────────┐
│ FRONTEND (Test Mode)                                        │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────┐    ┌────────────────────────────────┐    │
│  │  WAV File    │───▶│  FileAudioSource Component     │    │
│  │  (30 min)    │    │  - Decode to AudioBuffer       │    │
│  └──────────────┘    │  - Chunk into 4800 samples     │    │
│                      │  - Track sourcePosition        │    │
│                      └────────────┬───────────────────┘    │
│                                   │                         │
│                                   ▼                         │
│                      ┌────────────────────────────────┐    │
│                      │  WebSocket Client              │    │
│                      │  - Add seq, timestamp          │    │
│                      │  - Send: {audio, seq, ts, pos} │    │
│                      │  - Log send event              │    │
│                      └────────────┬───────────────────┘    │
│                                   │                         │
└───────────────────────────────────┼─────────────────────────┘
                                    │ WebSocket
                                    │
┌───────────────────────────────────┼─────────────────────────┐
│ BACKEND                           ▼                         │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌────────────────────────────────┐                         │
│  │  WebSocket Handler             │                         │
│  │  - Log: ws_receive (Point A)   │                         │
│  │  - Extract seq, ts, pos        │                         │
│  └────────────┬───────────────────┘                         │
│               │                                              │
│               ▼                                              │
│  ┌────────────────────────────────┐                         │
│  │  Riva gRPC Client              │                         │
│  │  - Log: grpc_send (Point B)    │                         │
│  │  - Send chunk to Riva          │                         │
│  │                                 │                         │
│  │  - Receive from Riva           │                         │
│  │  - Log: grpc_receive (Point C) │                         │
│  └────────────┬───────────────────┘                         │
│               │                                              │
│               ▼                                              │
│  ┌────────────────────────────────┐                         │
│  │  WebSocket Handler             │                         │
│  │  - Log: ws_send (Point D)      │                         │
│  │  - Send to frontend            │                         │
│  └────────────┬───────────────────┘                         │
│               │                                              │
│               ▼                                              │
│  ┌────────────────────────────────┐                         │
│  │  Metrics Exporter              │                         │
│  │  - WebSocket /ws/metrics       │                         │
│  │  - Stream timing events        │                         │
│  └────────────────────────────────┘                         │
│                                                              │
└─────────────────────────────────────────────────────────────┘
                                    │
                                    │ WebSocket /ws/metrics
                                    │
┌───────────────────────────────────▼─────────────────────────┐
│ DASHBOARD                                                    │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌────────────────────────────────┐                         │
│  │  Metrics WebSocket Client      │                         │
│  │  - Receive timing events       │                         │
│  └────────────┬───────────────────┘                         │
│               │                                              │
│               ▼                                              │
│  ┌────────────────────────────────┐                         │
│  │  Drift Calculator              │                         │
│  │  drift = elapsedTime - srcPos  │                         │
│  └────────────┬───────────────────┘                         │
│               │                                              │
│               ▼                                              │
│  ┌────────────────────────────────┐                         │
│  │  Real-time Chart               │                         │
│  │  - Line graph of drift         │                         │
│  │  - Latency breakdown           │                         │
│  └────────────────────────────────┘                         │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 Data Flow

**Audio Chunk Flow with Timing:**
```
1. Frontend: WAV File → AudioBuffer → 4800-sample chunks
   └─ Attach: {seq: 0, timestamp: 1000.5, sourcePosition: 0.3}

2. Frontend → Backend (WebSocket)
   └─ Message: {type: 'audio', data: <binary>, seq: 0, timestamp: 1000.5, sourcePosition: 0.3}

3. Backend receives
   └─ Log: {seq: 0, stage: 'ws_receive', timestamp: 1000.8, sourcePosition: 0.3}

4. Backend → Riva (gRPC)
   └─ Log: {seq: 0, stage: 'grpc_send', timestamp: 1000.9}

5. Riva processes and returns
   └─ Log: {seq: 0, stage: 'grpc_receive', timestamp: 1150.2}

6. Backend → Frontend (WebSocket)
   └─ Log: {seq: 0, stage: 'ws_send', timestamp: 1150.3}

7. Frontend receives
   └─ Log: {seq: 0, stage: 'frontend_receive', timestamp: 1150.6}
   └─ Calculate drift: (1150.6 - 1000.5) - 0.3 = 149.8ms latency

8. All logs → Dashboard via /ws/metrics
   └─ Display cumulative drift over 30 minutes
```

### 3.3 Critical Implementation Details

**Frontend: File Audio Source**
```typescript
// src/hooks/useFileAudioSource.ts

export const useFileAudioSource = (audioFilePath: string) => {
  const [audioBuffer, setAudioBuffer] = useState<AudioBuffer | null>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const sequenceRef = useRef(0);
  const startTimeRef = useRef(0);
  
  // Load and decode WAV file
  useEffect(() => {
    const loadAudio = async () => {
      const response = await fetch(audioFilePath);
      const arrayBuffer = await response.arrayBuffer();
      const audioContext = new AudioContext({ sampleRate: 16000 });
      const decoded = await audioContext.decodeAudioData(arrayBuffer);
      setAudioBuffer(decoded);
    };
    loadAudio();
  }, [audioFilePath]);
  
  // Stream chunks in real-time
  const startStreaming = (onChunk: (chunk: TimedAudioChunk) => void) => {
    if (!audioBuffer) return;
    
    const CHUNK_SIZE = 4800; // samples
    const SAMPLE_RATE = 16000; // Hz
    const CHUNK_DURATION = CHUNK_SIZE / SAMPLE_RATE; // 0.3 seconds
    
    startTimeRef.current = performance.now();
    let currentSample = 0;
    
    const intervalId = setInterval(() => {
      if (currentSample >= audioBuffer.length) {
        clearInterval(intervalId);
        setIsPlaying(false);
        return;
      }
      
      // Extract chunk
      const chunk = new Float32Array(CHUNK_SIZE);
      const channelData = audioBuffer.getChannelData(0);
      for (let i = 0; i < CHUNK_SIZE && currentSample + i < channelData.length; i++) {
        chunk[i] = channelData[currentSample + i];
      }
      
      // Create timed chunk
      const timedChunk: TimedAudioChunk = {
        type: 'audio',
        data: chunk.buffer,
        seq: sequenceRef.current++,
        timestamp: performance.now(),
        sourcePosition: currentSample / SAMPLE_RATE
      };
      
      onChunk(timedChunk);
      currentSample += CHUNK_SIZE;
      
    }, CHUNK_DURATION * 1000); // 300ms intervals
    
    setIsPlaying(true);
    return () => clearInterval(intervalId);
  };
  
  return { audioBuffer, isPlaying, startStreaming };
};
```

**Backend: Timing Logger**
```python
# app/services/timing_logger.py

from dataclasses import dataclass, asdict
from typing import List, Dict
import time
import asyncio
from collections import defaultdict

@dataclass
class TimingEvent:
    seq: int
    stage: str  # 'ws_receive', 'grpc_send', 'grpc_receive', 'ws_send'
    timestamp: float
    source_position: float = None

class TimingLogger:
    def __init__(self):
        self.events: List[TimingEvent] = []
        self.seq_tracking: Dict[int, Dict] = defaultdict(dict)
        
    def log(self, seq: int, stage: str, source_position: float = None):
        event = TimingEvent(
            seq=seq,
            stage=stage,
            timestamp=time.perf_counter(),
            source_position=source_position
        )
        self.events.append(event)
        self.seq_tracking[seq][stage] = event.timestamp
        
    def get_events(self, clear: bool = True) -> List[Dict]:
        events = [asdict(e) for e in self.events]
        if clear:
            self.events.clear()
        return events
    
    def get_latency_breakdown(self, seq: int) -> Dict[str, float]:
        """Calculate latencies between stages for a given sequence number"""
        stages = self.seq_tracking.get(seq, {})
        return {
            'ws_ingress_latency': stages.get('grpc_send', 0) - stages.get('ws_receive', 0),
            'riva_processing': stages.get('grpc_receive', 0) - stages.get('grpc_send', 0),
            'ws_egress_latency': stages.get('ws_send', 0) - stages.get('grpc_receive', 0),
            'total_latency': stages.get('ws_send', 0) - stages.get('ws_receive', 0)
        }
```

**Backend: WebSocket Handler with Timing**
```python
# app/routes/websocket.py

from fastapi import WebSocket
from app.services.timing_logger import TimingLogger
import json

timing_logger = TimingLogger()

@app.websocket("/ws/s2s")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    
    try:
        while True:
            # Receive audio chunk
            message_raw = await websocket.receive_text()
            message = json.loads(message_raw)
            
            if message['type'] == 'audio':
                # LOG POINT A: WebSocket Receive
                timing_logger.log(
                    seq=message['seq'],
                    stage='ws_receive',
                    source_position=message['sourcePosition']
                )
                
                # Extract audio data
                audio_data = base64.b64decode(message['data'])
                
                # Process through Riva (next step)
                await process_audio_chunk(
                    audio_data, 
                    message['seq'], 
                    websocket
                )
                
    except WebSocketDisconnect:
        pass
```

**Backend: Riva Integration with Timing**
```python
# app/services/riva_service.py

async def process_audio_chunk(audio_data: bytes, seq: int, websocket: WebSocket):
    
    # LOG POINT B: gRPC Send
    timing_logger.log(seq=seq, stage='grpc_send')
    
    # Send to Riva
    riva_request = create_riva_request(audio_data)
    
    # This is pseudo-code - adapt to your actual Riva streaming implementation
    response_stream = riva_client.streaming_s2s([riva_request])
    
    async for riva_response in response_stream:
        # LOG POINT C: gRPC Receive
        timing_logger.log(seq=seq, stage='grpc_receive')
        
        # Extract translated audio
        translated_audio = riva_response.audio
        
        # LOG POINT D: WebSocket Send
        timing_logger.log(seq=seq, stage='ws_send')
        
        # Send back to frontend
        await websocket.send_bytes(translated_audio)
```

**Backend: Metrics Endpoint**
```python
# app/routes/metrics.py

@app.websocket("/ws/metrics")
async def metrics_endpoint(websocket: WebSocket):
    await websocket.accept()
    
    try:
        while True:
            # Get timing events (clears internal buffer)
            events = timing_logger.get_events(clear=True)
            
            if events:
                await websocket.send_json(events)
            
            # Send updates every 500ms
            await asyncio.sleep(0.5)
            
    except WebSocketDisconnect:
        pass

@app.get("/api/metrics/export")
async def export_metrics():
    """Export all timing data as JSON for download"""
    events = timing_logger.get_events(clear=False)
    return JSONResponse(content=events)
```

**Dashboard: Drift Calculation**
```typescript
// src/components/DriftCalculator.ts

interface TimingEvent {
  seq: number;
  stage: string;
  timestamp: number;
  source_position?: number;
}

class DriftCalculator {
  private testStartTime: number | null = null;
  private driftData: Array<{ elapsedTime: number; drift: number; seq: number }> = [];
  
  processEvent(event: TimingEvent) {
    // Only calculate drift on frontend_receive events
    if (event.stage !== 'frontend_receive') return;
    
    if (this.testStartTime === null) {
      this.testStartTime = event.timestamp;
    }
    
    const elapsedTime = (event.timestamp - this.testStartTime) / 1000; // seconds
    const expectedPosition = event.source_position || 0;
    const drift = elapsedTime - expectedPosition;
    
    this.driftData.push({
      elapsedTime: elapsedTime / 60, // convert to minutes for chart
      drift,
      seq: event.seq
    });
    
    return { elapsedTime, drift };
  }
  
  getDriftData() {
    return this.driftData;
  }
  
  getAverageDrift() {
    if (this.driftData.length === 0) return 0;
    const sum = this.driftData.reduce((acc, d) => acc + d.drift, 0);
    return sum / this.driftData.length;
  }
  
  getMaxDrift() {
    if (this.driftData.length === 0) return 0;
    return Math.max(...this.driftData.map(d => d.drift));
  }
}
```

### 3.4 Testing Strategy

**Unit Tests:**
- Frontend: Test file audio chunking with mock AudioBuffer
- Frontend: Test sequence number generation
- Backend: Test timing logger stores events correctly
- Backend: Test latency calculation functions

**Integration Tests:**
- End-to-end test with 1-minute file
- Verify all 4 timing points are logged
- Verify sequence numbers match across pipeline
- Verify drift calculation is correct

**Load Tests:**
- Run full 30-minute test
- Monitor memory usage (timing logs accumulation)
- Verify no missed chunks
- Verify WebSocket doesn't drop messages

### 3.5 Deliverables Checklist

**Code Changes:**
- [ ] Frontend: `useFileAudioSource` hook
- [ ] Frontend: Test mode toggle UI
- [ ] Frontend: Silent output mode
- [ ] Frontend: Timing metadata in WebSocket messages
- [ ] Backend: `TimingLogger` service
- [ ] Backend: WebSocket handler instrumentation
- [ ] Backend: Riva gRPC instrumentation
- [ ] Backend: `/ws/metrics` endpoint
- [ ] Dashboard: Metrics WebSocket client
- [ ] Dashboard: Drift calculation logic
- [ ] Dashboard: Real-time chart component
- [ ] Dashboard: CSV export

**Configuration:**
- [ ] Test audio file (30-min WAV, 16kHz mono)
- [ ] Environment variables for test mode
- [ ] Metrics endpoint configuration

**Documentation:**
- [ ] README: How to run test mode
- [ ] README: How to interpret drift metrics
- [ ] Code comments for timing injection points

**Validation:**
- [ ] Run 30-minute test successfully
- [ ] Generate drift graph
- [ ] Export CSV of timing data
- [ ] Verify drift is within acceptable range (<20-30 seconds)

---

## INSTRUCTIONS FOR CLAUDE CODE

1. **First, examine the codebase** following the evaluation plan in Phase 1
2. **Report findings** on which libraries/patterns are currently used
3. **Identify gaps** where implementation plan needs adjustment
4. **Propose file structure changes** based on actual codebase layout
5. **Implement in order** of Epics 1 → 2 → 3 → 4
6. **Test incrementally** after each Epic
7. **Document assumptions** where implementation deviates from plan

**Key Questions to Answer After Evaluation:**
- Can we extend WebSocket messages without breaking protocol?
- Is there an existing AudioContext we can leverage?
- What's the exact Riva gRPC method signature?
- Do we need to add any new dependencies?
- Are there existing logging patterns to follow?

**Success Criteria:**
- 30-minute test completes without crashes
- All sequence numbers match across pipeline
- Drift graph shows cumulative lag over time
- CSV export contains complete timing data
- System identifies if lag exceeds 20-30 second threshold


