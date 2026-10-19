# Rust-Snipe

Easily find and snipe toasty.

You describe what you see: the monument you're next to, its biome, what else is around toasty
and in which direction, which way the monument is turned. Rust-Snipe checks that against the
current map of every popular Rust server. It ranks every possible spot and gives you the server,
the grid square and a ready-to-paste `client.connect` line.

It runs locally in your browser. No account or API key is needed.

## Features

* **Visual clue builder**: pick monuments, rocks, power lines, caves and more from pictures,
  so you don't have to know names like "Medium God Rock".
* **Sketch board**: drag things around the main monument to set direction and distance.
* **Monument rotation**: monuments with their own train-tunnel entrance (Launch Site,
  Powerplant, Airfield, Trainyard, Military Tunnels, Water Treatment, Excavator, Outpost,
  harbours, Ferry Terminal, Apartments) always have it in the same spot. Turn the monument
  on the sketch so the tunnel side matches what you saw. That narrows things down a lot.
* **Biomes**: "must be snow", "must NOT be desert", for the monument and for each nearby thing.
* **"Not nearby" clues**, optionally in a direction ("no Military Tunnels to the east").
* **Screenshot bearings**: enter compass readings and each result shows where you stood.
* **Zoomable map preview** with lines to every matched landmark.
* **Wipe tracking**: re-checks servers every 6 hours, downloads new maps after wipes and
  deletes old ones.
* Only standard procedural maps are kept. Custom maps are skipped.

## Quick start

You need **Python 3.9 or newer** ([python.org](https://www.python.org/downloads/); on Windows
tick *"Add python.exe to PATH"* in the installer).

**Windows:** download or clone this repository and double-click **`run.bat`**.

**macOS / Linux:**

```bash
git clone https://github.com/Idontknowhowtodoshit/Rust-Snipe.git
cd Rust-Snipe
./run.sh
```

The first start installs two Python packages (numpy, Pillow) into a local `.venv` folder and
opens `http://127.0.0.1:8765` in your browser. It then downloads the current map of every
tracked server, which takes about 10 minutes (~160 MB). Later starts are instant.

<details>
<summary>Manual setup (without the launchers)</summary>

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip ...
.venv/bin/python rustloc.py ui                   # Windows: .venv\Scripts\python ...
```
</details>

## How to use it

1. **You are at / next to**: click the box and pick the monument from the pictures. Click the
   biome chips once for *must be* (green) or twice for *must NOT be* (red).
2. If the monument shows a **rotation** tag, set which side its train-tunnel entrance is on.
   Drag the orange dot on the sketch, or pick a direction or exact degrees.
3. **Nearby**: add what else you can see. Set a direction (compass point or exact degrees, with
   a ± tolerance) and how many grid squares away it is. Drag it on the sketch to do the same.
4. **Not nearby**: things you're sure are *not* close, optionally only in one direction.
5. *(optional)* **Compass bearings** from a screenshot: the heading at the top of the screen,
   the bearing to the monument and to the first nearby thing. Each result then shows your
   estimated position.
6. **Search**. Click a result to see it on the map. Scroll to zoom and drag to move. Use
   *copy connect* to join the server.

Distances are in grid squares (~146 m), measured to the edge of a monument. Something a little
past your limit still counts partly, because distances are hard to judge in game.

A 1.00 means every clue matched. If the right answer scores low, one clue is probably
off. Check directions first: north is up on the sketch, and degrees run clockwise
(0 = N, 90 = E, 180 = S, 270 = W).

## Wipes and the map cache

* The UI re-checks every tracked server on start (if the last check is over 6 hours old) and
  then every 6 hours. A server whose map changed has wiped: the new map is downloaded and maps
  no server runs any more are deleted. Freshly wiped maps that rustmaps is still rendering are
  retried every 30 minutes.
* The header shows how many servers wiped in the last 24 hours (hover for the list) and has a
  **Sync now** button.
* Downloaded maps live in `maps/` and pictures in `assets/`. Both are rebuilt automatically, so
  deleting them is safe.

## Command line

```bash
python rustloc.py ui [--port 8765] [--no-browser]          # the web UI
python rustloc.py sync [--dry-run]                         # check for wipes, download / delete maps
python rustloc.py servers rustoria "rusty moose"           # add servers matching these names
python rustloc.py servers --list                           # what's in the server index
python rustloc.py fetch https://rustmaps.com/map/<id>      # one map by URL / id (kept forever)
python rustloc.py fetch 4250_548571299                     # one map by SIZE_SEED (needs API key)
python rustloc.py find clues/supermarket_excavator.json    # search with a clue file, draws result.png
python rustloc.py find clues/x.json --org rustoria         # only maps Rustoria servers run now
python rustloc.py bearing --heading 332 --x 1550 --fov 75  # screen x position -> compass bearing
python rustloc.py biome <map id>                           # biome classification preview
```

The default server networks are in `DEFAULT_ORGS` in `rustloc.py`.

### Settings (`.env`, optional)

Copy `.env.example` to `.env`:

| setting | meaning |
|---|---|
| `RUSTMAPS_API_KEY` | only for `fetch SIZE_SEED` (maps no tracked server runs, e.g. a seed from BattleMetrics) |
| `RUSTLOC_SYNC_HOURS` | how often the UI checks for wipes (default 6, `0` = only via *Sync now*) |
| `RUSTLOC_DATA` / `RUSTLOC_ASSETS` | where maps / pictures are stored |

### Clue files

`find` takes the same clues as the UI, as JSON (see `clues/`):

| key | meaning |
|---|---|
| `at` | monument you are at or next to (substring match: `Supermarket`, `Gas Station`, `Lighthouse`, ...) |
| `biome` / `biome_exclude` | biomes that must / must not be around it: `snow`, `tundra`, `arid`/`desert`, `temperate`, `jungle` |
| `at_facing` | compass side of the monument where its own tunnel entrance is (rotation) |
| `landmarks` | things you can see: `type`, compass `bearing`, `tolerance` (deg), `min_dist`/`max_dist` (m), optional `biome` / `biome_exclude` / `facing`. `type` can be `A\|B` (either) or `ocean` (open water in that direction) |
| `absent` | things that must *not* be within `max_dist` (optionally only in `bearing` ± `tolerance`) |
| `heading`, `hfov` | only used to draw the view cone on `result.png` |

**Bearings from a screenshot:** the heading is the compass value under the centre tick at the
top of the screen. To get an object's bearing, run `rustloc.py bearing --heading H --x <object's screen x>`.
Rust's FOV setting is vertical (default 75). If you don't know it, use a wider tolerance.

## How it works

* [rustmaps.com](https://rustmaps.com) lists each server's current map. Its map page includes
  every monument (down to single rocks, power poles and wells) with world coordinates, plus a
  rendered map image. Rust-Snipe downloads both, once per map.
* **Biomes** come from the rendered map colours after a ~35 m blur. They are sampled in a ring
  just *outside* each monument, because monuments themselves render as grey concrete and dirt.
  Snow, arid and temperate are reliable; temperate and jungle are harder to tell apart.
* **Rotation**: each tunnel monument's own train-tunnel entrance sits at a fixed distance from
  its centre. The bearing from the centre to that entrance gives the monument's rotation.
* **Scoring**: each candidate gets one score per clue, and the final score is their average:
  * Direction: a flat plateau inside half the tolerance, then a smooth falloff.
  * Distance: measured to the monument edge, fading out up to 50% past the limit.
  * Biome coverage, rotation, and absent things.
* With two compass bearings, the viewer's position is triangulated. Positions in the sea or too
  far away are ranked down.

## Credits and disclaimer

Map data, map renders and monument pictures come from [rustmaps.com](https://rustmaps.com).
Pictures are downloaded to your own PC on first start and are not part of this repository.
Please use rustmaps fairly: the tool only re-checks servers every few hours and downloads
each map once.

Rust-Snipe is not affiliated with Facepunch Studios or RustMaps. Rust is a trademark of
Facepunch Studios.

## License

[MIT](LICENSE)
