# Repository sanitization

The repository's text, paths, and generated evidence are designed to describe
the translation architecture without naming evaluation partners, customers,
speakers, or source material. The bundled source recordings are an explicit
exception: their upstream-published media bytes are retained unchanged for
reproducibility, while their tracked filenames are neutral.

The 2026-07-22 sanitization performed the following operations across every
owned branch and its reachable history:

- replaced personal names with an approved public GitHub handle where upstream
  attribution was required;
- removed partner, customer, event-domain, workstation-path, host, and private
  endpoint identifiers;
- replaced source titles with neutral sample identifiers;
- removed identifying audio filenames, derived audio, raw event exports, plots,
  compressed logs, and detailed runtime captures from rewritten Git history;
- renamed code, schemas, documentation, and temporary resources to use neutral
  evaluation terminology; and
- retained only sanitized documentation and aggregate measurements needed to
  explain the technical conclusions.

After the rewrite, the five source fixtures were restored byte-for-byte under
neutral sample IDs so a fresh clone can reproduce the evaluation. Their hashes
are pinned in `test_audio/SHA256SUMS`; the media payloads and embedded metadata
are intentionally not anonymized. Additional inputs, generated audio, and raw
runtime evidence remain ignored.

New public textual evidence must use neutral sample IDs, omit transcripts and
raw paths, minimize session/runtime metadata, and be checked for secrets and
identifying text before commit.

The upstream project is attributed by its public GitHub username and URL. The
upstream repository is outside the scope of this repository's history rewrite.
