# Real miniature trial - 2026-08-28

## Scope

Two source STLs from the user's Miniatures library were copied only into ignored
Agent-Slicer work projects. No source file was modified and no printer was
contacted.

## Berry ping-pong miniature

- Source: `Berry/berrynocolor.stl`
- Size: 58.718 x 40.620 x 74.996 mm
- Source topology: 72,956 triangles, manifold in Blender
- Hollow method: uniformly contracted internal cavity
- Cavity scale: 0.926145
- Approximate wall request: 1.5 mm; uniform wall is not guaranteed
- Drains: two 3.0 mm bottom-region Boolean openings
- Material volume: 20,696.631 to 5,070.000 mm3 (24.50%)
- Final topology: closed/manifold, 129,814 STL triangles, zero degenerates

Standard support slice:

- 85 supports, 1,540 layers, 6.317 ml, CTB v5
- UVtools: 9,076 islands, 53 resin traps, 1 overhang

Dense experimental support slice:

- 435 supports, 1,600 layers, 9.990 ml, CTB v5
- UVtools: 9,032 islands, 328 resin traps, 7 overhangs

The dense preset did not materially solve islands and made other findings worse.

## Tea-drinking miniature

- Source: `Meshy_AI_Tea_drinking_0208145724_generate.stl`
- Source units were ambiguous; explicitly scaled from 1,879.973 to 70 mm height
- Source topology: 661,684 triangles, manifold in Blender
- Hollow method: uniformly contracted internal cavity
- Cavity scale: 0.942902
- Drains: two 3.0 mm bottom-region Boolean openings
- Material volume: 60,737.595 to 11,471.725 mm3 (18.89%)
- Final topology: closed/manifold
- Float32 plate export removed two triangles that collapsed during quantization;
  the corrected plate has 995,036 triangles and zero degenerates
- Slice: 153 supports, 1,440 layers, 12.539 ml, CTB v5
- UVtools: 5,084 islands, 16 resin traps, 50 overhangs

## Conclusion

The bounded approximate hollowing and drain workflow works on both tested
manifold miniatures and produces auditable geometry. Resin-Slicer's current
automatic support generator does not achieve production-ready island coverage
on either detailed miniature. Both CTBs are format-readable Mars 3 Pro software
artifacts, but neither should be sent to a printer without improved orientation,
support generation, and another independent analysis pass.

