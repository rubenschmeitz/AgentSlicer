# Prepare resin miniatures

Use Agent-Slicer's public CLI or SDK only.

1. Initialize or load a resin project.
2. Add source models; never modify originals.
3. Resolve machine, material, purpose, and quality presets.
4. Run `plan` and inspect all blockers.
5. Do not bypass missing hollowing, drainage, orientation, support, or production
   validation capabilities.
6. Build only when the plan is unblocked.
7. Run `agent-slicer analyze` on every sliced artifact when UVtools is available.
   Treat remaining islands, resin traps, suction cups, or large overhangs as
   unresolved evidence; do not call the artifact print-ready.
8. Return the report, previews, hashes, and validation status. Never send the
   output to a printer without a separate explicit request.
