# Bundled evaluation audio

The five source fixtures in this directory are byte-for-byte copies of the
recordings published by the upstream project. They are tracked intentionally so
a fresh clone can reproduce the same evaluation inputs.

The filenames are neutral, but the recordings and their embedded metadata are
unchanged. Do not treat the media payloads as anonymized.

Verify the fixtures before a comparison run:

```bash
sha256sum --check SHA256SUMS
```

The evaluation tools look in this directory by default. To use alternate
consent-cleared inputs without changing the repository, set
`S2S_TEST_AUDIO_DIR` to a private directory containing:

- `preflight.wav`
- `long-form-01.mp3`
- `long-form-02.mp3`
- `long-form-03.mp3`
- `long-form-03-30min.wav`

Additional recordings and all generated audio remain ignored. Do not force-add
them.
