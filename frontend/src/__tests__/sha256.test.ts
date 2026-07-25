import { createHash } from 'node:crypto';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { sha256Hex, sha256HexFallback } from '../utils/sha256';

function patternedBytes(length: number): Uint8Array {
  return Uint8Array.from(
    { length },
    (_, index) => (index * 131 + 17) & 0xff,
  );
}

function expectedSha256(bytes: Uint8Array): string {
  return createHash('sha256').update(bytes).digest('hex');
}

describe('browser SHA-256 fallback', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it.each([0, 3, 55, 56, 63, 64, 65, 127, 128, 255])(
    'matches SHA-256 across the %i-byte padding boundary case',
    (length) => {
      const bytes = patternedBytes(length);
      expect(sha256HexFallback(bytes.buffer as ArrayBuffer))
        .toBe(expectedSha256(bytes));
    },
  );

  it('uses the exact fallback digest when subtle crypto is unavailable', async () => {
    vi.stubGlobal('crypto', {});
    const bytes = patternedBytes(4097);

    await expect(sha256Hex(bytes.buffer as ArrayBuffer))
      .resolves.toBe(expectedSha256(bytes));
  });

  it('falls back deterministically when native digest rejects', async () => {
    vi.stubGlobal('crypto', {
      subtle: {
        digest: vi.fn().mockRejectedValue(new Error('secure context required')),
      },
    });
    const bytes = patternedBytes(1024);

    await expect(sha256Hex(bytes.buffer as ArrayBuffer))
      .resolves.toBe(expectedSha256(bytes));
  });
});
