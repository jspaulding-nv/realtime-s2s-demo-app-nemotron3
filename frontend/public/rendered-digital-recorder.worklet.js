/* global AudioWorkletProcessor, currentFrame, registerProcessor */

const CHANNEL_COUNT = 2;

function requireSafeInteger(value, minimum = 0) {
  return Number.isSafeInteger(value) && value >= minimum;
}

function quantizePcm16(value) {
  if (!Number.isFinite(value)) return 0;
  const clipped = Math.max(-1, Math.min(1, value));
  return Math.max(-32768, Math.min(32767, Math.round(clipped * 32768)));
}

class RenderedDigitalRecorderProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const requestedChunkFrames = options.processorOptions?.chunkFrames;
    const requestedMaximumFrames = options.processorOptions?.maximumFrames;
    this.chunkFrames = requireSafeInteger(requestedChunkFrames, 128)
      ? requestedChunkFrames
      : 8000;
    this.maximumFrames = requireSafeInteger(requestedMaximumFrames, 1)
      ? requestedMaximumFrames
      : 16000 * 180;
    this.pending = new Int16Array(this.chunkFrames * CHANNEL_COUNT);
    this.pendingFrames = 0;
    this.pendingStartContextFrame = null;
    this.sequence = 0;
    this.captureStartContextFrame = null;
    this.nextContextFrame = null;
    this.sourceClock = null;
    this.readyReported = false;
    this.captureArmed = false;
    this.recording = true;

    this.port.onmessage = (event) => {
      const message = event.data;
      if (!message || typeof message.type !== 'string') return;
      if (message.type === 'arm_source_clock') {
        this.armSourceClock(message);
      } else if (message.type === 'stop') {
        this.stopCapture();
      } else if (message.type === 'abort') {
        this.recording = false;
        this.pendingFrames = 0;
        this.port.postMessage({ type: 'aborted' });
      }
    };
  }

  armSourceClock(message) {
    const {
      sourceStartContextFrame,
      sourceFrameCount,
      sourceChunkFrames,
    } = message;
    const invalid = (
      this.sourceClock !== null
      || !this.readyReported
      || !requireSafeInteger(sourceStartContextFrame)
      || !requireSafeInteger(sourceFrameCount, 1)
      || !requireSafeInteger(sourceChunkFrames, 1)
      || sourceFrameCount % sourceChunkFrames !== 0
    );
    if (invalid) {
      this.recording = false;
      this.port.postMessage({
        type: 'capture_error',
        code: 'invalid_source_clock',
      });
      return;
    }

    this.sourceClock = {
      sourceStartContextFrame,
      sourceFrameCount,
      sourceChunkFrames,
      nextChunkIndex: 0,
    };
    // Chrome may jump its AudioContext frame axis during the processor's
    // silent startup. That warm-up is outside the evidence epoch. The first
    // quantum after this arm establishes captureStartContextFrame; every
    // subsequent quantum must remain exactly contiguous.
    this.captureArmed = true;
    this.captureStartContextFrame = null;
    this.nextContextFrame = null;
    this.pendingFrames = 0;
    this.pendingStartContextFrame = null;
    this.sequence = 0;
    this.port.postMessage({
      type: 'source_clock_armed',
      sourceStartContextFrame,
      sourceFrameCount,
      sourceChunkFrames,
    });
  }

  flushPending() {
    if (this.pendingFrames === 0) return;
    const sampleCount = this.pendingFrames * CHANNEL_COUNT;
    const block = this.pending.slice(0, sampleCount);
    this.port.postMessage({
      type: 'pcm_block',
      sequence: this.sequence,
      startContextFrame: this.pendingStartContextFrame,
      frameCount: this.pendingFrames,
      interleavedPcm16: block,
    }, [block.buffer]);
    this.sequence += 1;
    this.pendingFrames = 0;
    this.pendingStartContextFrame = null;
  }

  stopCapture() {
    if (!this.recording) return;
    this.recording = false;
    this.flushPending();
    this.port.postMessage({
      type: 'stopped',
      captureStartContextFrame: this.captureStartContextFrame,
      captureEndContextFrameExclusive: this.nextContextFrame,
      blockCount: this.sequence,
    });
  }

  emitSourceBoundaries(observedContextFrame) {
    if (this.sourceClock === null) return;
    const clock = this.sourceClock;
    const totalChunks = clock.sourceFrameCount / clock.sourceChunkFrames;
    while (clock.nextChunkIndex < totalChunks) {
      const sourceSampleEndExclusive = (
        (clock.nextChunkIndex + 1) * clock.sourceChunkFrames
      );
      const boundaryContextFrame = (
        clock.sourceStartContextFrame + sourceSampleEndExclusive
      );
      // process() runs before the quantum beginning at currentFrame is
      // rendered. A source-end boundary is eligible only after currentFrame
      // has reached it; using the quantum end would send up to 128 frames
      // early while falsely labelling the callback as late.
      if (boundaryContextFrame > observedContextFrame) break;
      this.port.postMessage({
        type: 'source_chunk_due',
        chunkIndex: clock.nextChunkIndex,
        sourceSampleStart: (
          clock.nextChunkIndex * clock.sourceChunkFrames
        ),
        sourceSampleEndExclusive,
        boundaryContextFrame,
        deliveredAfterContextFrame: observedContextFrame,
      });
      clock.nextChunkIndex += 1;
    }
  }

  process(inputs, outputs) {
    if (!this.recording) return false;

    const output = outputs[0] ?? [];
    const outputLeft = output[0];
    const outputRight = output[1];
    const frameCount = outputLeft?.length ?? outputRight?.length ?? 128;
    const blockStartContextFrame = currentFrame;
    const blockEndContextFrame = blockStartContextFrame + frameCount;

    // Keep a non-muted destination path alive, but never expose the monitored
    // source or translated channels through this recorder output.
    outputLeft?.fill(0);
    outputRight?.fill(0);
    if (!this.readyReported) {
      this.readyReported = true;
      this.port.postMessage({
        type: 'ready',
        readyContextFrame: blockStartContextFrame,
      });
    }
    if (!this.captureArmed) return true;

    if (this.captureStartContextFrame === null) {
      if (
        blockStartContextFrame
        > this.sourceClock.sourceStartContextFrame
      ) {
        this.recording = false;
        this.port.postMessage({
          type: 'capture_error',
          code: 'capture_started_after_source',
          sourceStartContextFrame: (
            this.sourceClock.sourceStartContextFrame
          ),
          observedContextFrame: blockStartContextFrame,
        });
        return false;
      }
      this.captureStartContextFrame = blockStartContextFrame;
      this.nextContextFrame = blockStartContextFrame;
      this.port.postMessage({
        type: 'started',
        captureStartContextFrame: blockStartContextFrame,
      });
    }
    if (this.nextContextFrame !== blockStartContextFrame) {
      this.recording = false;
      this.pendingFrames = 0;
      this.port.postMessage({
        type: 'capture_error',
        code: 'noncontiguous_render_quantum',
        expectedContextFrame: this.nextContextFrame,
        observedContextFrame: blockStartContextFrame,
      });
      return false;
    }
    this.emitSourceBoundaries(blockStartContextFrame);

    const sourceInput = inputs[0]?.[0];
    const translatedInput = inputs[1]?.[0];
    let offset = 0;
    while (offset < frameCount) {
      if (this.pendingFrames === 0) {
        this.pendingStartContextFrame = blockStartContextFrame + offset;
      }
      const available = this.chunkFrames - this.pendingFrames;
      const take = Math.min(available, frameCount - offset);
      for (let index = 0; index < take; index += 1) {
        const frameIndex = offset + index;
        const sourceSample = sourceInput?.[frameIndex] ?? 0;
        const translatedSample = translatedInput?.[frameIndex] ?? 0;
        const pendingIndex = (
          (this.pendingFrames + index) * CHANNEL_COUNT
        );
        this.pending[pendingIndex] = quantizePcm16(sourceSample);
        this.pending[pendingIndex + 1] = quantizePcm16(translatedSample);
      }
      this.pendingFrames += take;
      offset += take;
      if (this.pendingFrames === this.chunkFrames) this.flushPending();
    }

    this.nextContextFrame = blockEndContextFrame;

    if (
      this.captureStartContextFrame !== null
      && blockEndContextFrame - this.captureStartContextFrame
        >= this.maximumFrames
    ) {
      this.flushPending();
      this.recording = false;
      this.port.postMessage({
        type: 'capture_error',
        code: 'capture_frame_limit_exceeded',
      });
      return false;
    }
    return true;
  }
}

registerProcessor(
  'rendered-digital-recorder',
  RenderedDigitalRecorderProcessor,
);
