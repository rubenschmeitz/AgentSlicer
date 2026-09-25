# Blender adapter

Blender 4.5 is a transitional, headless geometry backend. The first bounded
operation is `hollow_and_drain`: it accepts a manifold upright STL, creates an
inward shell, selects deterministic bottom-region drain positions, applies exact
Boolean cutters, and rejects results that are non-manifold or have implausible
material volume.

It is intentionally not yet a general repair or semantic foot-recognition
adapter. Source meshes are never changed in place, and all results include a
JSON before/after report. Public Agent-Slicer schemas do not expose Blender
object names, modifiers, or `.blend` internals.
