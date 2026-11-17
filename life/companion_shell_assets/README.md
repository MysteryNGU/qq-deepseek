# Companion desktop shell

The Python `Companion` remains the only cognition, persona, memory, project and
executor owner. `ServiceThread` retains SQLite ownership on its asyncio thread.
The sidecar starts only the pinned Coopanion `DesktopPetWorld`, its loopback
PetServer, its original Electron transparent host, and the original console chat
page/UI primitives. It never creates a Cortico Bot, Cormini Persona or provider.

## Runtime and source

- Upstream source: `vendor/coopanion`, commit
  `8829fd4fd0916f2c65f824daa63e503eff042b8a`, AGPL-3.0-or-later.
  Original license, notices and figure credits remain there. Pinned Cortico
  submodule `83623831133a01679db5b5459eea7d11be590988` retains its MIT license.
- Isolated JS dependencies/Electron: `data/companion-runtime/coopanion-shell`.
  Electron 44.4.4 supplies Node 24 when `ELECTRON_RUN_AS_NODE=1`; the native host
  removes that variable before opening the GUI. No DSH or Codex Node is needed.
- Frontend: `chat.ts` embeds upstream `console/features/chat/index.ts`, not a
  replacement chat renderer. `build.mjs` bundles it and compiles/copies upstream
  styles into the isolated runtime's `web` directory. Run it with Node >=22.
- Live2D: PixiJS 6.5.10, `@pixi/unsafe-eval` 6.5.10 and
  pixi-live2d-display 0.4.0 (MIT); official Live2D Cubism Core 5.1.0 is cached
  separately with source URL/SHA256 and original proprietary license header.
  Its official URL is recorded in `live2d-core/SOURCE.json`.

Installed model files and their notices remain unchanged. Generated figure packs
contain adapter code and directory junctions to those model directories and the
shared rendering libraries. They expose no character home, config, persona or
project root. Each pack keeps Coopanion's opaque iframe sandbox/CSP; Pixi's
official compatibility module avoids adding `unsafe-eval`.

## Bridge

JSON lines use the existing `@companion-pet ` prefix. Renderer input calls
`worker.call('chat', text)`. Only the core's assistant message event renders a
reply; the chat return value is never rendered again. Upstream's display history
is a bounded, nonpersistent projection of the core snapshot. Figure switches
do not open another core or write conversation memory. The user's selected body
is stored in their home's `data/companion/appearance.json`; a first run defaults
to 神宫白子/面饼0. The original walking Q-version whale remains selectable.

This embedding's chat API accepts text only. The build disables the upstream
image-picker option, the input shows a text-only hint, and pasted/dropped images
are rejected visibly. The host also rejects attachment-bearing input before
calling the core, so it cannot silently discard an image and charge for its
remaining text. This does not change the separate computer screenshot executor.

Assistant message events retain their durable message ID, turn ID and part
index. `bubble-queue.js` paces presentation before `pet_say`, using upstream's
script parser and reading-time estimate. Thus the original desktop queue and
main chat receive one bubble at a time. New user input cancels unfinished model
generation, with all user inputs retained for the next response. Already-saved
followups are immediately recorded in the main chat without playing on the pet;
the active desktop speech and queued lines stop through `companion-interrupt`.
This small local adaptation lives in upstream `web/pet-app.js`. The queue is display-only;
closing it does not delete the core's history. The isolated native acceptance
report is `data/test-results/coopanion-multi-bubble-smoke/report.json`.

`CompanionShell.control(op, args)` returns a concurrent Future with the actual
upstream tool receipt. Operations are `pet_say`, `pet_ask`, `pet_move_to`,
`pet_act`, `pet_set`, `pet_quiet`, and `pet_see_self`. Cancellation aborts the
upstream operation and stops an outstanding walk. Core executors may await it
with `asyncio.wrap_future`; this bridge never makes a model call.

The native host samples the current figure-frame layout on request every two
seconds. Changed `body_context` values update the same core through
`update_body`; they never call cognition. Only current body/vocabulary/self
permissions are selected from the World's prompt variables; the complete dress
catalog is excluded. Position is a fraction of the current stage, with unknown
vertical position left null for floor-bound bodies. `roam` is the effective
setting (including temporary quiet); `roam_preference` retains the saved choice.
`calm` still permits occasional spontaneous walking, while `off` disables only
spontaneous choices and allows explicit movement when the body can walk.

`attach_body` receives the shell's control callback after readiness.
`observe_body` receives actual touch/figure/answer observations with unique IDs.
The upstream touch merge is retained; one grab observation is added per gesture
because upstream intentionally omits it, and merged drop observations include
their actual normalized x coordinate. No model is called by these bridge
methods; the core decides whether and when to react.

Move receipts derive their structured status from actual page
`arrived`/`interrupted` events correlated with the outgoing walk ID. A timeout or
disconnection is failed, never inferred as arrival from receipt prose.
`pet_act` reports only `completion: accepted`. Native checks are recorded in
`data/test-results/coopanion-embodied-smoke` (Q movement/drag/drop/poke),
`coopanion-face-body-smoke` (actual face, eight valid actions), and
`companion-body-paid-1794649900` (real provider/public chat chooses move,
receives native arrival, then confirms it). The initial combined check's face
attempt preceded the corrected figure manifest and fell back to Coo; its
face portion is superseded by the separate passing face check.

All seven Live2D models remain idle/drag bodies, without advertising walking
capability. The exploratory front-facing hakuko mesh gait is disabled: it did
not meet the requested side-view stroll. Only the original Q-version whale
currently advertises and performs walking. Original moc3/texture data are
not modified.

## Checks

`python -m unittest tests.test_companion_shell tests.test_companion_desktop
tests.test_hakuko_gait` checks the bridge, owner-thread lifecycle, default entry,
saved choices, real model/executor phase and gait restoration.

Actual native GUI acceptance artifacts are under
`data/test-results/coopanion-hakuko-smoke`, `coopanion-hakuko-walk`, and
`coopanion-paid-shell`. The paid check uses a read-only SQLite backup of the
real character plus its persona in an isolated home: one actual API call,
persisted note, hakuko→whale→hakuko, unchanged history/notes after switching,
and clean owned-process exit. It does not write the real character.
