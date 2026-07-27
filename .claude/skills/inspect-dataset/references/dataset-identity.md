# Dataset identity

Use the sampled fingerprint for fast orientation. It hashes the file size plus the first and last
1 MiB, so it detects most accidental substitutions without reading a large file completely. It is
not a cryptographic identity for adversarial or append-preserving changes.

Use the full fingerprint when publishing provenance, comparing transfers, or deciding whether a
resumed session still refers to the exact bytes analyzed previously. A full fingerprint reads the
entire file and may be expensive on shared storage.

Format evidence is deliberately conservative. H5AD and 10x H5 are both HDF5 containers; byte
inspection cannot distinguish their internal schemas. A later format-specific skill must open the
container and inspect keys, shapes, feature metadata, and sparse matrix encoding.
