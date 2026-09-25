# UVtools adapter

The implemented `resin.analyze.uvtools` capability reads sliced files through
`UVtoolsCmd`, extracts selected machine/print properties, and counts islands,
overhangs, resin traps, suction cups, touching bounds, print-height findings,
and empty layers. It writes a compact JSON result through `agent-slicer analyze`.

This is analysis, not automatic repair and not yet the production-validation
capability. Detector findings require policy and often visual review.
