# Lokale integratie

Deze map bevat de Agent-Slicer-bron die op 14 september 2026 vanaf de andere pc is overgezet. De oorspronkelijke review-snapshot staat in:

`../../incoming/agent-slicer-agent-slicer-20260914/`

De primaire Python-bron staat in `agent_slicer/`. De benodigde resin-companion staat in `companion/Resin-Slicer/`. De overdrachtsdetails, bronpaden, beperkingen en installatie-instructies staan in `TRANSFER.md` en `README.md`.

## Relatie met deze 3D-printrepo

- De algemene workflow, projectbrief en gebruikersspecifieke regels staan in de bovenliggende repo en `AGENTS.md`.
- De bovenliggende Mars 5 Ultra-profielen blijven de uitgangsbasis voor deze werkruimte.
- De overgezette slicer mag die profielen niet stilzwijgend vervangen of Mars 3 Pro-instellingen als Mars 5 Ultra gebruiken.
- Meshy- en Tripo-tasks blijven expliciete, door de gebruiker goed te keuren acties.
- Een build, export of validatie mag geen fysieke printerverbinding starten.

## Huidige lokale status (2026-09-14)

- Python 3.12.10 is geïnstalleerd en beide projectomgevingen zijn aangemaakt:
  `tools/agent-slicer/.venv` en `companion/Resin-Slicer/.venv`.
- Agent-Slicer is editable geïnstalleerd; de lokale Resin-Slicer executable
  wordt automatisch gevonden vanuit de companion-map.
- Blender 5.2.1 staat op `C:\Program Files\Blender Foundation\Blender 5.2\blender.exe`.
- UVtools 4.4.3 staat op `C:\Program Files\UVtools\UVtoolsCmd.exe`.
- `scripts\agent-slicer.ps1` koppelt deze runtimepaden voor agents en houdt
  zware tijdelijke bestanden buiten versiebeheer.
- De Electron-dependencies zijn met de meegeleverde `npm` geïnstalleerd en de
  UI-productiebuild/syntaxcheck is geslaagd. Gebruik vanuit de repo-root
  `scripts\resin-slicer-ui.ps1 check` of `start`.
- De acht Mars 5 Ultra-resinbestanden zijn gecontroleerd met
  `scripts\check-mars5-profiles.ps1` en als Agent-Slicer- en Resin-Slicer-
  startprofielen geregistreerd.
- Agent-Slicer: 98 tests uitgevoerd, status `OK`; 2 Linux-only tests
  overgeslagen omdat zij process-groups vereisen. De twee echte Blender-
  integratietests zijn uitgevoerd met Blender 5.2.1.
- Resin-Slicer: 121 tests geslaagd.
- Een geïntegreerde `test-smoke`-projectbuild is afgerond met CTB-output en
  geslaagde basisvalidatie.

De profielen en bestandswriters zijn daarmee lokaal werkend op software-niveau.
De Mars 5 Ultra-output is nog geen firmware- of printgarantie: kalibreer resin,
controleer een klein bestand in CHITUBOX/UVtools en voer pas daarna een kleine
fysieke test uit. Er is tijdens deze integratie geen printerverbinding gestart.
