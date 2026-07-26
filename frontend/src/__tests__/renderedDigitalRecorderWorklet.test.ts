import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { runInNewContext } from 'node:vm';
import { describe, expect, it } from 'vitest';

type WorkletMessage = Record<string, unknown>;
type ProcessorConstructor = new (options: Record<string, unknown>) => {
  port: {
    onmessage: ((event: { data: WorkletMessage }) => void) | null;
  };
  process: (
    inputs: Float32Array[][],
    outputs: Float32Array[][],
  ) => boolean;
};

interface WorkletHarness {
  messages: WorkletMessage[];
  process: (
    contextFrame: number,
    sourceValue?: number,
    translatedValue?: number,
  ) => boolean;
  send: (message: WorkletMessage) => void;
}

function createHarness(): WorkletHarness {
  const messages: WorkletMessage[] = [];
  let Processor: ProcessorConstructor | null = null;
  class MockAudioWorkletProcessor {
    port = {
      onmessage: null as (
        ((event: { data: WorkletMessage }) => void) | null
      ),
      postMessage: (message: WorkletMessage) => {
        messages.push(message);
      },
    };
  }
  const sandbox = {
    AudioWorkletProcessor: MockAudioWorkletProcessor,
    Int16Array,
    Number,
    Math,
    currentFrame: 0,
    registerProcessor: (
      _name: string,
      value: typeof Processor,
    ) => {
      Processor = value;
    },
  };
  const path = resolve(
    process.cwd(),
    'public/rendered-digital-recorder.worklet.js',
  );
  runInNewContext(readFileSync(path, 'utf8'), sandbox);
  const RegisteredProcessor = Processor as ProcessorConstructor | null;
  if (RegisteredProcessor === null) {
    throw new Error('worklet did not register');
  }
  const processor = new RegisteredProcessor({
    processorOptions: {
      chunkFrames: 128,
      maximumFrames: 16_000,
    },
  });

  return {
    messages,
    process: (
      contextFrame,
      sourceValue = 0,
      translatedValue = 0,
    ) => {
      sandbox.currentFrame = contextFrame;
      const source = new Float32Array(128).fill(sourceValue);
      const translated = new Float32Array(128).fill(translatedValue);
      const outputLeft = new Float32Array(128).fill(1);
      const outputRight = new Float32Array(128).fill(1);
      const keepAlive = processor.process(
        [[source], [translated]],
        [[outputLeft, outputRight]],
      );
      expect(Array.from(outputLeft)).toEqual(
        Array.from(new Float32Array(128)),
      );
      expect(Array.from(outputRight)).toEqual(
        Array.from(new Float32Array(128)),
      );
      return keepAlive;
    },
    send: (message) => {
      processor.port.onmessage?.({ data: message });
    },
  };
}

describe('rendered-digital recorder worklet', () => {
  it('excludes pre-arm warm-up gaps and PCM from the capture epoch', () => {
    const harness = createHarness();

    expect(harness.process(0, 0.25, 0.5)).toBe(true);
    expect(harness.process(4_096, 0.25, 0.5)).toBe(true);
    expect(harness.messages).toEqual([
      { type: 'ready', readyContextFrame: 0 },
    ]);

    harness.send({
      type: 'arm_source_clock',
      sourceStartContextFrame: 8_000,
      sourceFrameCount: 256,
      sourceChunkFrames: 128,
    });
    expect(harness.process(4_224, 0.125, 0.25)).toBe(true);
    harness.send({ type: 'stop' });

    expect(harness.messages.map((message) => message.type)).toEqual([
      'ready',
      'source_clock_armed',
      'started',
      'pcm_block',
      'stopped',
    ]);
    const block = harness.messages.find(
      (message) => message.type === 'pcm_block',
    );
    expect(block).toMatchObject({
      sequence: 0,
      startContextFrame: 4_224,
      frameCount: 128,
    });
    const pcm = block?.interleavedPcm16;
    expect(pcm).toBeInstanceOf(Int16Array);
    expect(Array.from(pcm as Int16Array).slice(0, 4)).toEqual([
      4096, 8192, 4096, 8192,
    ]);
    expect(harness.messages).not.toContainEqual(
      expect.objectContaining({ type: 'capture_error' }),
    );
  });

  it('fails closed on a post-arm render-quantum gap', () => {
    const harness = createHarness();
    harness.process(0);
    harness.send({
      type: 'arm_source_clock',
      sourceStartContextFrame: 1_024,
      sourceFrameCount: 256,
      sourceChunkFrames: 128,
    });
    expect(harness.process(128)).toBe(true);

    expect(harness.process(384)).toBe(false);
    expect(harness.messages).toContainEqual({
      type: 'capture_error',
      code: 'noncontiguous_render_quantum',
      expectedContextFrame: 256,
      observedContextFrame: 384,
    });
  });

  it('fails closed when the first captured quantum is after source start', () => {
    const harness = createHarness();
    harness.process(0);
    harness.send({
      type: 'arm_source_clock',
      sourceStartContextFrame: 1_024,
      sourceFrameCount: 256,
      sourceChunkFrames: 128,
    });

    expect(harness.process(1_152)).toBe(false);
    expect(harness.messages).toContainEqual({
      type: 'capture_error',
      code: 'capture_started_after_source',
      sourceStartContextFrame: 1_024,
      observedContextFrame: 1_152,
    });
    expect(harness.messages).not.toContainEqual(
      expect.objectContaining({ type: 'started' }),
    );
  });
});
