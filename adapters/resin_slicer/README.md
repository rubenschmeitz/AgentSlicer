# Resin-Slicer adapter

The MVP invokes the installed `resin-slicer` executable as a subprocess. It
passes a combined STL, a resolved material profile, explicit support controls,
and an output path. The adapter captures the exact command and output in the
build manifest.

