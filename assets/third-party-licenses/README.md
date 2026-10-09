# Pinned upstream notice fallbacks

These are full, unchanged upstream license and attribution texts for the exact
Python dependency versions named in `manifest.json`. They supplement wheels that
omit notices. Installed license files (including the spelling `LICENCE`) take
precedence. The collector never requests network access and rejects missing,
unmatched, linked or modified fallback files rather than emitting an identifier
as a license.

Each file records its SHA256 and original URL. PyPI source-archive members also
record the archive's published SHA256 and exact member path. GitHub release files
use immutable commits resolved from the named release tag. The proxy_tools package
has no release tag: its published 0.1.0 source and setup file were compared byte
for byte with the recorded upstream commit. Its `ATTRIBUTION.txt` explains the
difference between its MIT package metadata and the full upstream BSD license.
That file is a Forge provenance note, not a replacement license.

Primp and its included h2 notices use MIT. Its included rustls notice explicitly
offers Apache-2.0, MIT or ISC at the recipient's option. The manifest describes
that choice; all three full texts and the upstream choice notice remain included.

To review an update, obtain the named official source, check the archive hash
where applicable, extract only the named regular text members, and compare every
stored file's SHA256. For GitHub files, fetch the commit URL recorded in the
manifest and compare the SHA256. Preserve upstream bytes and all notices from
the selected source archive. Add a new exact-version entry rather than applying
an older license fallback to a different dependency version.
