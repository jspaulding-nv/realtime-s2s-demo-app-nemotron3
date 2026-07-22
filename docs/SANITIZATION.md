# Repository sanitization

The public repository is designed to describe the translation architecture and
its aggregate behavior without identifying evaluation partners, customers,
speakers, or source material.

The 2026-07-22 sanitization performed the following operations across every
owned branch and its reachable history:

- replaced personal names with an approved public GitHub handle where upstream
  attribution was required;
- removed partner, customer, event-domain, workstation-path, host, and private
  endpoint identifiers;
- replaced source titles with neutral sample identifiers;
- removed recorded audio, derived audio, raw event exports, plots, compressed
  logs, and detailed runtime captures from Git history;
- renamed code, schemas, documentation, and temporary resources to use neutral
  evaluation terminology; and
- retained only documentation and aggregate measurements needed to reproduce
  the technical conclusions.

Recorded inputs remain local and ignored. New public evidence must use neutral
sample IDs, omit transcripts and raw paths, minimize session/runtime metadata,
and be checked for secrets and identifying text before commit.

The upstream project is attributed by its public GitHub username and URL. The
upstream repository is outside the scope of this repository's history rewrite.
