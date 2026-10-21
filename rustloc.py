"""Rust-Snipe (rustloc engine) - find where a Rust screenshot was taken, on every popular server's map.

Commands
  servers [terms...]                  find popular servers on rustmaps, download their current maps
  sync    [--dry-run]                 re-check all tracked servers: download maps after wipes, delete old ones
  fetch   <map url | id | SIZE_SEED>  download one map (SIZE_SEED needs RUSTMAPS_API_KEY in .env)
  list    <map id> [--type TEXT]      list monuments (optionally filtered)
  bearing --heading H --x PX ...      convert a screen x-position to a compass bearing
  find    <clues.json> [--org TEXT]   rank every candidate location in every downloaded map
  biome   <map id>                    write a biome-classification preview image
  ui      [--port 8765] [--no-browser]  web UI: describe what's next to what, search all maps

World coordinates are rustmaps' (x = east, y = north, origin at the map centre).
Compass bearings are Rust's: 0 = N, 90 = E.
"""
import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

if sys.version_info < (3, 9):
    sys.exit("Rust-Snipe needs Python 3.9 or newer")

ROOT = Path(__file__).resolve().parent


def load_env():
    """Read KEY=VALUE lines from .env next to this file into the environment (real env vars win)."""
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.split(" #")[0]                     # allow trailing comments
                if v.strip():
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
# Keep paths short: Windows MAX_PATH bites in deep folders. Override with RUSTLOC_DATA (relative = to this folder).
MAPS = ROOT / os.environ.get("RUSTLOC_DATA", "maps")
UA = {"User-Agent": "Mozilla/5.0 (rustloc)"}

# Reference colours of each biome on rustmaps' rendered map (map_raw_normalized.png),
# after a ~35 m box blur, sampled from the interiors of known biome areas.
# Temperate vs jungle is the weakest split: their colours are close.
BIOME_REFS = {
    "snow":      [(240, 247, 247), (224, 231, 230)],
    "tundra":    [(136, 143, 124), (151, 159, 142)],
    "arid":      [(139, 138, 94), (152, 148, 110), (162, 158, 121)],
    "temperate": [(105, 106, 68), (108, 110, 71), (100, 104, 60)],
    "jungle":    [(82, 93, 40), (81, 94, 33)],
}
ALIASES = {"desert": "arid", "forest": "temperate", "grass": "temperate", "arctic": "snow"}


# ---------------------------------------------------------------- data access
API = "https://api.rustmaps.com"
SERVERS = MAPS / "servers.json"
# Search terms for popular server networks (matched against server names by rustmaps).
DEFAULT_ORGS = [
    "rustoria", "rustafied", "moose", "rusticated", "stevious", "vital", "reddit", "atlas",
    "survivors", "bloo lagoon", "upsurge", "rustinity", "facepunch", "rusty chicken",
    "lone.design", "rusthub", "paranoid", "rustopia", "werust", "mirage", "pickle", "ukn",
]


def http_get(url, headers=None, data=None, method=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})}, data=data, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def api(path, params=None, key=False, method=None, body=None):
    """Call api.rustmaps.com. /internal/v1 needs no key (it's what the website uses); /v4 needs one."""
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {}
    if key:
        k = os.environ.get("RUSTMAPS_API_KEY")
        if not k:
            sys.exit(f"RUSTMAPS_API_KEY is not set - put it in {ROOT / '.env'}")
        headers["X-API-Key"] = k
    if body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(body).encode()
    try:
        return json.loads(http_get(url, headers, body, method))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"meta": {"status": "Failed", "statusCode": e.code}, "data": None}


def save_map(d):
    if not d.get("imageUrl"):
        raise ValueError("map has no rendered image yet")
    out = MAPS / d["id"][:8]
    out.mkdir(parents=True, exist_ok=True)
    # image first: a folder with map.json is treated as complete
    (out / "map.png").write_bytes(http_get(d["imageUrl"]))
    (out / "map.json").write_text(json.dumps(d, indent=1))
    return out


CUSTOM_FILE = MAPS / "custom.json"   # ids of custom maps sync skipped (so they aren't fetched again)


def custom_ids():
    try:
        return set(json.loads(CUSTOM_FILE.read_text()))
    except Exception:
        return set()


def fetch_map_id(map_id, refresh=False, quiet=False, skip_custom=False, log=print):
    """Download one map by its rustmaps id (cached: a map id never changes content).
    skip_custom: don't save custom maps (seed 1 / edited), just remember their id."""
    out = MAPS / map_id[:8]
    if not refresh and (out / "map.json").exists() and (out / "map.png").exists():
        return map_id
    r = api(f"/internal/v1/maps/{map_id}")
    d = r.get("data")
    if not d or not d.get("monuments"):
        # fall back to the data embedded in the website page
        html = http_get(f"https://rustmaps.com/map/{map_id}").decode("utf8")
        i = html.find("window.pageData")
        if i < 0:
            log(f"  ! map {map_id}: not available ({r.get('meta')})")
            return None
        d = json.JSONDecoder().raw_decode(html[html.index("{", i):])[0]["data"]
    if not d or not d.get("monuments"):
        log(f"  ! map {map_id[:8]}: rustmaps hasn't rendered it yet ({(d or {}).get('state')}), retried next sync")
        return None
    if skip_custom and not is_classic(d):
        CUSTOM_FILE.write_text(json.dumps(sorted(custom_ids() | {map_id})))
        return "custom"
    out = save_map(d)
    if not quiet:
        print(f"saved {out.name}  seed={d['seed']} size={d['size']} monuments={len(d['monuments'])}")
    return d["id"]


def fetch_seed(size, seed):
    """Look a procedural map up by size + seed (needs RUSTMAPS_API_KEY); request generation if missing."""
    r = api(f"/v4/maps/{size}/{seed}", key=True)
    code = r.get("meta", {}).get("statusCode")
    if r.get("data") and r["data"].get("id"):
        return fetch_map_id(r["data"]["id"])
    if code == 404:
        g = api("/v4/maps", key=True, method="POST", body={"size": int(size), "seed": int(seed), "staging": False})
        print(f"map {size}_{seed} not generated yet - requested generation ({g.get('meta')}); retry in a few minutes")
    else:
        print(f"map {size}_{seed}: {r.get('meta')}")
    return None


def fetch(ref):
    """Accepts a rustmaps URL, a map id, or SIZE_SEED."""
    ref = re.sub(r".*rustmaps\.com/map/", "", ref).strip("/")
    m = re.fullmatch(r"(\d{3,5})_(\d+)", ref)
    map_id = fetch_seed(*m.groups()) if m else fetch_map_id(ref, refresh=True)
    if map_id:
        (MAPS / map_id[:8] / "keep").touch()       # prune_maps never deletes maps fetched by hand
    return map_id


STALE_DAYS = 7      # forget a server rustmaps hasn't listed for this long (empty servers drop out of search)
SYNC_FILE = MAPS / "sync.json"


def update_servers(terms, with_players=True, log=print):
    """Find servers by name on rustmaps and record their current map. A server whose map id changed
    since last time has wiped. Downloads every current map that isn't cached yet (retries failures)."""
    known = json.loads(SERVERS.read_text()) if SERVERS.exists() else {}
    now = time.time()
    for v in known.values():
        v.setdefault("seen", now)            # entries from before 'seen' was recorded
    found = {}
    for t in terms:
        try:
            r = api("/internal/v1/servers/search", {"input": t, "onlyServersWithPlayers": str(with_players).lower()})
        except Exception as e:
            log(f"{t!r}: search failed ({e})")
            continue
        rows = r.get("data") or []
        log(f"{t!r}: {len(rows)} servers")
        for row in rows:
            if row.get("mapId"):
                found[f"{row['ip']}:{row['gamePort']}"] = {**row, "query": t, "seen": now}
        time.sleep(0.3)

    wiped = [{"name": v["name"], "old": known[k]["mapId"], "new": v["mapId"], "at": v.get("lastWipeUtc")}
             for k, v in found.items() if k in known and known[k]["mapId"] != v["mapId"]]
    added = [v["name"] for k, v in found.items() if k not in known]
    known.update(found)
    gone = [known.pop(k)["name"] for k in [k for k, v in known.items() if now - v["seen"] > STALE_DAYS * 86400]]
    MAPS.mkdir(parents=True, exist_ok=True)
    SERVERS.write_text(json.dumps(known, indent=1))
    update_geo(log=log)                                  # regions of any new servers
    for w in wiped:
        log(f"wiped: {w['name']}  ({w['old'][:8]} -> {w['new'][:8]})")
    for n in added:
        log(f"new server: {n}")
    for n in gone:
        log(f"dropped (not listed for {STALE_DAYS} days): {n}")

    ids = sorted({v["mapId"] for v in known.values()})
    skip = custom_ids()
    new = [i for i in ids if i not in skip and not (MAPS / i[:8] / "map.json").exists()]
    log(f"{len(known)} servers on {len(ids)} maps, {len(new)} to download")
    failed, custom = [], 0
    for n, i in enumerate(new, 1):
        names = [v["name"] for v in known.values() if v["mapId"] == i]
        log(f"[{n}/{len(new)}] {names[0][:60]}{f' (+{len(names) - 1})' if len(names) > 1 else ''}")
        try:
            got = fetch_map_id(i, quiet=True, skip_custom=True, log=log)
            if not got:
                failed.append(i)
            elif got == "custom":
                custom += 1
                log("  custom map, skipped")
        except Exception as e:
            log(f"  ! {i}: {e}")
            failed.append(i)
        time.sleep(0.3)
    return {"servers": len(known), "maps": len(ids), "wiped": wiped, "added": added, "dropped": gone,
            "downloaded": len(new) - len(failed) - custom, "custom_skipped": custom, "failed": failed}


def prune_maps(dry_run=False, log=print):
    """Delete cached maps no tracked server runs any more (i.e. from before a wipe), and custom maps
    (seed 1 / edited: only standard procedural maps are searched). Maps you fetched by hand carry a
    'keep' file and are never deleted."""
    import shutil
    current = {s["mapId"][:8] for s in json.loads(SERVERS.read_text()).values()} if SERVERS.exists() else set()
    removed, custom = [], set()
    for p in sorted(MAPS.iterdir()) if MAPS.exists() else []:
        if not p.is_dir() or (p / "keep").exists():
            continue
        if p.name in current:
            try:
                d = json.loads((p / "map.json").read_text())
            except Exception:
                continue
            if is_classic(d):
                continue
            custom.add(d["id"])
        if not current:
            break                                    # no server list: never wipe the whole cache
        if not dry_run:
            shutil.rmtree(p, ignore_errors=True)
            if p.exists():
                log(f"  ! could not delete {p.name} (in use?)")
                continue
        removed.append(p.name)
    if custom and not dry_run:
        # remember them so sync doesn't download them again; forget ids no server runs any more
        live = {s["mapId"] for s in json.loads(SERVERS.read_text()).values()}
        CUSTOM_FILE.write_text(json.dumps(sorted((custom_ids() | custom) & live)))
    log(f"{'would delete' if dry_run else 'deleted'} {len(removed)} old/custom maps" + (f": {', '.join(removed)}" if removed else ""))
    return removed


def sync(terms=None, prune=True, dry_run=False, log=print):
    """Refresh every tracked server network, download new maps after wipes, delete the old ones."""
    known = json.loads(SERVERS.read_text()) if SERVERS.exists() else {}
    terms = terms or sorted(set(DEFAULT_ORGS) | {v.get("query") for v in known.values() if v.get("query")})
    res = update_servers(terms, log=log)
    res["removed"] = prune_maps(dry_run, log=log) if prune else []
    if not dry_run:
        SYNC_FILE.write_text(json.dumps({"time": time.time(), **res}, indent=1))
    return res


def last_sync():
    try:
        return json.loads(SYNC_FILE.read_text())
    except Exception:
        return None


def servers_by_map():
    if not SERVERS.exists():
        return {}
    geo, out = load_geo(), {}
    for s in json.loads(SERVERS.read_text()).values():
        out.setdefault(s["mapId"], []).append({**s, "region": server_region(s, geo)})
    return out


# ---------------------------------------------------------------- server regions
GEO_FILE = MAPS / "geo.json"          # server ip -> country / continent (looked up once per ip)
REGIONS = ["EU", "NA", "SA", "OCE", "Asia", "Africa"]
_CONTINENT = {"EU": "EU", "NA": "NA", "SA": "SA", "OC": "OCE", "AS": "Asia", "AF": "Africa"}
# fallback when an ip couldn't be looked up: what the server name says
_NAME_REGION = [
    ("OCE", r"\b(AU|AUS|OCE|OCEANIA|AUSTRALIA|NZ)\b"),
    ("Asia", r"\b(SEA|ASIA|SG|JP|KR|HK|AE|CN)\b|[一-鿿]"),
    ("SA", r"\b(SA|BR|BRAZIL|LATAM)\b"),
    ("NA", r"\b(US|USA|NA|CA|CAN|CANADA)\b"),
    ("EU", r"\b(EU|EUROPE|UK)\b"),
]


def load_geo():
    try:
        return json.loads(GEO_FILE.read_text())
    except Exception:
        return {}


def server_region(s, geo=None):
    """EU / NA / SA / OCE / Asia / Africa: the region the server's name advertises (what players
    pick by; a few are hosted elsewhere), else where its ip is, else '?'."""
    name = s.get("name", "").upper()
    by_name = next((r for r, pat in _NAME_REGION if re.search(pat, name)), None)
    if by_name:
        return by_name
    g = (load_geo() if geo is None else geo).get(s.get("ip"))
    return _CONTINENT.get(g.get("cont"), "?") if g else "?"


def update_geo(log=print):
    """Look up the location of server ips not seen before (ip-api.com batch, free, no key;
    only the game servers' public ips are sent). Cached in maps/geo.json."""
    servers = json.loads(SERVERS.read_text()) if SERVERS.exists() else {}
    geo = load_geo()
    todo = sorted({s["ip"] for s in servers.values() if s.get("ip")} - set(geo))
    for i in range(0, len(todo), 100):
        body = json.dumps([{"query": ip, "fields": "status,countryCode,continentCode,query"} for ip in todo[i:i + 100]])
        try:
            rows = json.loads(http_get("http://ip-api.com/batch", {"Content-Type": "application/json"},
                                       body.encode(), "POST"))
        except Exception as e:
            log(f"server location lookup failed ({e}); using server names for regions")
            break
        for r in rows:
            if r.get("status") == "success":
                geo[r["query"]] = {"cc": r.get("countryCode"), "cont": r.get("continentCode")}
        if i + 100 < len(todo):
            time.sleep(4.5)                              # free tier: 15 batch requests a minute
    if todo:
        GEO_FILE.write_text(json.dumps(geo, indent=0))
        log(f"server locations: {len(todo)} new ips looked up")
    return len(todo)


def load_maps(ids=None):
    if ids is None:
        ids = [p.name for p in sorted(MAPS.iterdir()) if (p / "map.json").exists()] if MAPS.exists() else []
    dirs = [MAPS / i[:8] for i in ids]
    for p in dirs:
        d = json.loads((p / "map.json").read_text())
        if d.get("monuments"):
            yield d, p


# ---------------------------------------------------------------- geometry
def bearing(a, b):
    """Compass bearing from world point a to b (0 = N, clockwise)."""
    return math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) % 360


def ang_diff(a, b):
    return abs((a - b + 180) % 360 - 180)


def grid_ref(x, y, size):
    """Rust in-game grid label (A0 = top-left), ~146 m cells."""
    cells = int(size // 146.3)
    cell = size / cells
    # clamp: oil rigs, icebergs etc. can sit outside the grid
    col = min(max(int((x + size / 2) // cell), 0), cells - 1)
    row = min(max(int((size / 2 - y) // cell), 0), cells - 1)
    letters = ""
    col += 1
    while col:
        col, r = divmod(col - 1, 26)
        letters = chr(65 + r) + letters
    return f"{letters}{row}"


def triangulate(p1, b1, p2, b2):
    """Viewer position from which p1 is seen at bearing b1 and p2 at bearing b2.
    Returns (viewer, dist_to_p1, dist_to_p2), or None if the rays don't meet in front of the viewer."""
    d1 = np.array([math.sin(math.radians(b1)), math.cos(math.radians(b1))])
    d2 = np.array([math.sin(math.radians(b2)), math.cos(math.radians(b2))])
    try:
        t, u = np.linalg.solve(np.array([-d1, d2]).T, np.subtract(p2, p1).astype(float))
    except np.linalg.LinAlgError:
        return None
    if t <= 0 or u <= 0:
        return None
    v = (p1[0] - t * d1[0], p1[1] - t * d1[1])
    return (round(v[0]), round(v[1])), round(t), round(u)


def screen_bearing(heading, x, width=1920, height=1080, fov=75.0):
    """Compass bearing of a screen column. Rust's FOV setting is vertical."""
    tan_h = math.tan(math.radians(fov / 2)) * width / height
    off = (x - width / 2) / (width / 2) * tan_h
    return (heading + math.degrees(math.atan(off))) % 360


# ---------------------------------------------------------------- biome
BIOMES = list(BIOME_REFS) + ["water"]
WATER = len(BIOMES) - 1
_REF_IDX = np.array([i for i, cs in enumerate(BIOME_REFS.values()) for _ in cs])
_REFS = np.array([c for cs in BIOME_REFS.values() for c in cs], np.float32)
GRID = 480        # biome grid resolution (~9 m per cell on a 4250 map), cached per map
OCEAN_TYPES = {"ocean", "sea", "coast", "water"}


def classify(a):
    """Blurred RGB array -> biome index per pixel (nearest reference colour; blue-dominant = water)."""
    idx = _REF_IDX[((a[:, :, None, :] - _REFS[None, None]) ** 2).sum(-1).argmin(-1)]
    water = (a[:, :, 2] > a[:, :, 0] + 15) & (a[:, :, 2] > a[:, :, 1])
    idx[water] = WATER
    return idx.astype(np.uint8)


class MapImage:
    def __init__(self, d, path):
        self.d, self.path, self.size = d, path, d["size"]
        self._img = self._grid = None

    @property
    def img(self):
        if self._img is None:
            self._img = Image.open(self.path / "map.png").convert("RGB")
        return self._img

    @property
    def W(self):
        return self.img.width

    def px(self, x, y):
        return ((x + self.size / 2) / self.size * self.W, (self.size / 2 - y) / self.size * self.W)

    def m2px(self, m):
        return m / self.size * self.W

    @property
    def grid(self):
        """GRID x GRID biome indices, computed once from map.png and cached as biome.npy."""
        if self._grid is None:
            f = self.path / "biome.npy"
            if f.exists():
                self._grid = np.load(f)
            else:
                small = self.img.resize((GRID, GRID), Image.BOX)
                # blur ~35 m: removes roads, rocks and patchy vegetation
                blur = max(1, round(35 / self.size * GRID))
                a = np.asarray(small.filter(ImageFilter.BoxBlur(blur)), dtype=np.float32)
                self._grid = classify(a)
                np.save(f, self._grid)
        return self._grid

    def g(self, x, y):
        c = GRID / self.size
        return int((x + self.size / 2) * c), int((self.size / 2 - y) * c)

    def biome_at(self, x, y):
        gx, gy = self.g(x, y)
        if 0 <= gx < GRID and 0 <= gy < GRID:
            return BIOMES[self.grid[gy, gx]]
        return "water"

    def biome_mix(self, x, y, radius=200, inner=0):
        """Share of each biome between inner and radius metres of (x, y), ignoring water."""
        gx, gy = self.g(x, y)
        r = max(1, int(radius / self.size * GRID))
        ri = int(inner / self.size * GRID)
        yy, xx = np.ogrid[-r:r + 1, -r:r + 1]
        d2 = xx * xx + yy * yy
        mask = (d2 <= r * r) & (d2 >= ri * ri)
        y0, x0 = gy - r, gx - r
        win = self.grid[max(0, y0):gy + r + 1, max(0, x0):gx + r + 1]
        m = mask[max(0, -y0):max(0, -y0) + win.shape[0], max(0, -x0):max(0, -x0) + win.shape[1]]
        vals = win[m]
        vals = vals[vals != WATER]
        if not len(vals):
            return {}
        n = np.bincount(vals, minlength=len(BIOMES))
        return {BIOMES[i]: round(float(n[i]) / len(vals), 2) for i in np.argsort(-n) if n[i]}

    def ocean_hit(self, x, y, brg, lo, hi, run=150):
        """Distance to the first stretch of >= run metres of water along a bearing, or None."""
        step = max(10.0, self.size / GRID)
        need, streak, first = max(1, int(run / step)), 0, None
        s, c = math.sin(math.radians(brg)), math.cos(math.radians(brg))
        t = lo
        while t <= hi + run:
            if self.biome_at(x + s * t, y + c * t) == "water":
                if streak == 0:
                    first = t
                streak += 1
                if streak >= need:
                    return first if first <= hi else None
            else:
                streak = 0
            t += step
        return None

    def labels(self):
        return np.array(BIOMES)[self.grid]


# ---------------------------------------------------------------- matching
# Approximate monument radius (m): distances are measured to the edge, not the centre point.
RADIUS_BY_TYPE = {"Launch Site": 280, "Airfield": 250, "Powerplant": 190, "Trainyard": 190, "Excavator": 180,
                  "Large Harbor": 200, "Military Tunnels": 160, "Water Treatment": 170, "Ferry Terminal": 160,
                  "Nuclear Missile Silo": 150, "Arctic Research Base": 140, "Junkyard": 140, "Outpost": 130,
                  "Bandit Town": 130, "Satellite Dish": 110, "Sphere Tank": 110, "Radtown": 120}
RADIUS_BY_SIZE = {"LargeMonument": 140, "SmallMonument": 50, "UniqueEnvironment": 80}


def monument_radius(m):
    for k, r in RADIUS_BY_TYPE.items():
        if m["type"].startswith(k):
            return r
    return RADIUS_BY_SIZE.get(m.get("sizeCategory"), 0)


def edge_dist(a, m):
    """Distance from point a to the edge of monument m (0 if inside)."""
    return max(0.0, math.dist(a, (m["coordinates"]["x"], m["coordinates"]["y"])) - monument_radius(m))


def type_match(mtype, want):
    """Substring match; 'A|B' matches either."""
    return any(w.strip().lower() in mtype.lower() for w in want.split("|") if w.strip())


def biome_around(mi, m, ring=150):
    """Biome mix in a ring just outside a monument. Its own concrete, dirt and runways render
    grey/brown and would otherwise read as tundra or temperate even in the middle of snow."""
    r = monument_radius(m)
    return mi.biome_mix(m["coordinates"]["x"], m["coordinates"]["y"], radius=r + ring, inner=r)


def biome_score(mix, wanted):
    # 2/3 coverage counts as a full match: biome borders are fuzzy
    return min(1.0, sum(mix.get(b, 0) for b in wanted) * 1.5)


def biome_exclude_score(mix, excluded):
    # an excluded biome covering half the area or more rules the spot out
    return max(0.0, 1.0 - 2.0 * sum(mix.get(b, 0) for b in excluded))


def norm_biomes(bs):
    return [ALIASES.get(b.lower(), b.lower()) for b in (bs or [])]


def dir_score(err, tol):
    """1.0 within tol/2 of the wanted bearing (picking 'E' means roughly east), then falls off."""
    return math.exp(-((max(0.0, err - tol / 2) / (tol / 2)) ** 2))


# Monuments that carry their own train-tunnel entrance at a fixed offset (measured over 123 maps,
# spread ~1 m). The bearing from the monument to that entrance gives its rotation.
TUNNEL_OFFSET = {"Ferry Terminal": 20, "Large Harbor": 46, "Small Harbor": 99, "Airfield": 182,
                 "Trainyard": 71, "Military Tunnels": 91, "Excavator": 98, "Outpost": 102,
                 "Water Treatment": 139, "Apartments Complex": 109, "Launch Site": 204, "Powerplant": 86}


def tunnel_type(mtype):
    return next((k for k in TUNNEL_OFFSET if mtype.startswith(k)), None)


def facing(d, m):
    """Bearing from monument m to its own tunnel entrance, or None if it has none."""
    k = tunnel_type(m["type"])
    if not k:
        return None
    a = (m["coordinates"]["x"], m["coordinates"]["y"])
    for t in d["monuments"]:
        if t["type"].startswith("Tunnel Entrance"):
            p = (t["coordinates"]["x"], t["coordinates"]["y"])
            if abs(math.dist(a, p) - TUNNEL_OFFSET[k]) < 12:
                return round(bearing(a, p))
    return None


def facing_score(d, m, want, tol):
    f = facing(d, m)
    return (0.0, None) if f is None else (dir_score(ang_diff(f, want), tol), f)


def score_landmark(d, mi, anchor, a, lm):
    tol = lm.get("tolerance", 25)
    lo, hi = lm.get("min_dist", 0), lm.get("max_dist", 1e9)
    want_b, excl_b = norm_biomes(lm.get("biome")), norm_biomes(lm.get("biome_exclude"))

    if lm["type"].lower() in OCEAN_TYPES:
        hi = min(hi, 3000)
        if "bearing" in lm:
            angles = [lm["bearing"] + k for k in range(-int(2 * tol), int(2 * tol) + 1, 5)]
        else:
            angles = list(range(0, 360, 10))
        best, info = 0.0, None
        for ang in angles:
            dist = mi.ocean_hit(*a, ang, lo, hi)
            if dist is None:
                continue
            s = dir_score(ang_diff(ang, lm["bearing"]), tol) if "bearing" in lm else 1.0
            if s > best:
                p = (a[0] + math.sin(math.radians(ang)) * dist, a[1] + math.cos(math.radians(ang)) * dist)
                best, info = s, {"type": "ocean", "dist": round(dist), "edge": round(dist),
                                 "bearing": round(ang % 360),
                                 "err": round(ang_diff(ang, lm["bearing"])) if "bearing" in lm else None,
                                 "pos": (round(p[0]), round(p[1]))}
        return best, info

    best, info = 0.0, None
    for m in d["monuments"]:
        if m is anchor or not type_match(m["type"], lm["type"]):
            continue
        p = (m["coordinates"]["x"], m["coordinates"]["y"])
        dist, edge = math.dist(a, p), edge_dist(a, m)
        # people misjudge distance: past max_dist the score fades out over another 50%
        over = edge - hi
        if dist < lo or over > hi * 0.5:
            continue
        err, s = None, (1.0 if over <= 0 else math.exp(-((over / (0.25 * hi)) ** 2)))
        if "bearing" in lm:
            err = ang_diff(bearing(a, p), lm["bearing"])
            s *= dir_score(err, tol)
        f = None
        if "facing" in lm and s > best:
            fs, f = facing_score(d, m, lm["facing"], lm.get("facing_tol", 45))
            s *= fs
        if (want_b or excl_b) and s > best:
            mix = biome_around(mi, m, 150)
            if want_b:
                s *= biome_score(mix, want_b)
            if excl_b:
                s *= biome_exclude_score(mix, excl_b)
        if s > best:
            best, info = s, {"type": m["type"], "dist": round(dist), "edge": round(edge),
                             "bearing": round(bearing(a, p)), "err": None if err is None else round(err),
                             "facing": f, "pos": p}
    return best, info


def absent_hit(d, mi, anchor, a, ab):
    """The nearest thing that should NOT be there (within max_dist, in direction if given), or None."""
    if ab["type"].lower() in OCEAN_TYPES:
        s, info = score_landmark(d, mi, anchor, a, ab)
        return info if s >= 0.5 else None
    for m in d["monuments"]:
        if m is anchor or not type_match(m["type"], ab["type"]):
            continue
        if edge_dist(a, m) > ab.get("max_dist", 1e9):
            continue
        p = (m["coordinates"]["x"], m["coordinates"]["y"])
        if "bearing" in ab and dir_score(ang_diff(bearing(a, p), ab["bearing"]), ab.get("tolerance", 30)) < 0.5:
            continue
        return {"type": m["type"], "dist": round(math.dist(a, p)), "edge": round(edge_dist(a, m)),
                "bearing": round(bearing(a, p)), "pos": p}
    return None


def score_candidate(d, mi, anchor, clues):
    """Score one anchor monument against the clues. detail = {biome, facing, landmarks[], absent[]}."""
    a = (anchor["coordinates"]["x"], anchor["coordinates"]["y"])
    parts, detail = [], {"landmarks": [], "absent": []}

    wanted, excluded = norm_biomes(clues.get("biome")), norm_biomes(clues.get("biome_exclude"))
    if wanted or excluded:
        mix = biome_around(mi, anchor, clues.get("biome_radius", 200))
        detail["biome"] = mix
        if wanted:
            parts.append(biome_score(mix, wanted))
        if excluded:
            parts.append(biome_exclude_score(mix, excluded))

    if "at_facing" in clues:
        s, f = facing_score(d, anchor, clues["at_facing"], clues.get("at_facing_tol", 45))
        parts.append(s)
        detail["facing"] = f

    for lm in clues.get("landmarks", []):
        s, info = score_landmark(d, mi, anchor, a, lm)
        parts.append(s)
        detail["landmarks"].append(info)

    for ab in clues.get("absent", []):
        hit = absent_hit(d, mi, anchor, a, ab)
        parts.append(0.0 if hit else 1.0)
        detail["absent"].append(hit)

    score = sum(parts) / len(parts) if parts else 1.0
    return score, detail


def is_classic(d):
    """Standard procedural map (size + seed, vanilla monuments). Custom maps loaded from a map file
    report seed 1; edited procedural maps are flagged with custom monuments."""
    return d.get("type") == "Procedural" and not d.get("hasCustomMonuments") and d.get("seed", 0) > 1


def search(clues, map_ids=None, org=None, top=50, pool=None, classic_only=True, regions=None):
    """Rank every clues['at'] monument in every downloaded map. Returns a list of result dicts.
    pool: optional {map_id8: MapImage} of preloaded maps (the UI keeps one in memory).
    org / regions: only maps run by servers whose name contains org / that are in one of regions."""
    by_map = {k[:8]: v for k, v in servers_by_map().items()}
    if org or regions:
        def wanted(s):
            return (not org or org.lower() in s["name"].lower()) and (not regions or s["region"] in regions)
        by_map = {k: [s for s in v if wanted(s)] for k, v in by_map.items()}
        by_map = {k: v for k, v in by_map.items() if v}
        if map_ids is None:
            map_ids = list(by_map)
    if pool is None:
        maps = (MapImage(d, path) for d, path in load_maps(map_ids))
    elif map_ids is None:
        maps = list(pool.values())
    else:
        maps = [pool[i[:8]] for i in map_ids if i[:8] in pool]
    results = []
    for mi in maps:
        d = mi.d
        if classic_only and not is_classic(d):
            continue
        anchors = [m for m in d["monuments"] if type_match(m["type"], clues["at"])]
        if not anchors:
            continue
        for anc in anchors:
            score, detail = score_candidate(d, mi, anc, clues)
            c = anc["coordinates"]
            results.append({
                "score": round(score, 3), "map": d["id"], "seed": d["seed"], "size": d["size"],
                "type": anc["type"], "x": c["x"], "y": c["y"], "grid": grid_ref(c["x"], c["y"], d["size"]),
                "servers": by_map.get(d["id"][:8], []), "detail": detail,
                "_mi": mi, "_anchor": anc, "_d": d,
            })
    results.sort(key=lambda r: -r["score"])
    return results[:top] if top else results


def find(clues, map_ids=None, top=10, render=True, org=None, classic_only=True, regions=None):
    results = search(clues, map_ids, org, top=0, classic_only=classic_only, regions=regions)
    if not results:
        print(f"no '{clues['at']}' in any downloaded map")
        return []
    print(f"{len(results)} candidates in {len({r['map'] for r in results})} maps")
    for rank, r in enumerate(results[:top], 1):
        print(f"\n#{rank}  score {r['score']:.2f}  {r['type']} at ({r['x']}, {r['y']})  grid {r['grid']}")
        print(f"     map {r['map']}  seed {r['seed']}  size {r['size']}  https://rustmaps.com/map/{r['map']}")
        for s in r["servers"]:
            print(f"     server: [{s['region']}] {s['name']}  ({s['ip']}:{s['gamePort']}, wiped {s['lastWipeUtc'][:10]})")
        for k, v in r["detail"].items():
            print(f"     {k}: {v}")
    if render:
        r = results[0]
        out = render_candidate(r["_mi"], r["_d"], r["_anchor"], r["detail"], clues, ROOT / "result.png")
        print(f"\nbest match rendered -> {out}")
    return results


def render_candidate(mi, d, anc, detail, clues, out, span=900, scale=3, viewer=None, center=None, out_px=None):
    """Map crop around the match. viewer=(x, y): estimated player position, drawn with the view cone.
    center/span (m) pick the view (default: frame monument + player); out_px fixes the output size."""
    a = (anc["coordinates"]["x"], anc["coordinates"]["y"])
    centre = a
    if center:
        centre = center
    elif viewer:  # frame both the monument and the player
        centre = ((a[0] + viewer[0]) / 2, (a[1] + viewer[1]) / 2)
        span = max(span, math.dist(a, viewer) / 2 + 350)
    cx, cy = mi.px(*centre)
    r = mi.m2px(span)
    if out_px:
        scale = out_px / (2 * r)
    crop = mi.img.crop((int(cx - r), int(cy - r), int(cx + r), int(cy + r))).resize(
        (int(2 * r * scale), int(2 * r * scale)), Image.LANCZOS)
    dr = ImageDraw.Draw(crop)
    ox, oy = cx - r, cy - r

    def P(x, y):
        X, Y = mi.px(x, y)
        return ((X - ox) * scale, (Y - oy) * scale)

    # every monument in view, labelled (zoomed far out: only the bigger ones)
    for m in d["monuments"]:
        X, Y = P(m["coordinates"]["x"], m["coordinates"]["y"])
        if span > 1300 and m.get("sizeCategory") not in ("LargeMonument", "SmallMonument"):
            continue
        if 0 <= X < crop.width and 0 <= Y < crop.height:
            dr.ellipse([X - 4, Y - 4, X + 4, Y + 4], outline=(255, 255, 255), width=2)
            dr.text((X + 6, Y - 6), m["type"], fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))
    A = P(*a)
    V = P(*viewer) if viewer else A
    # view direction (camera heading) as a cone
    if "heading" in clues:
        fov_half = clues.get("hfov", 100) / 2
        L = crop.width
        for h in (clues["heading"] - fov_half, clues["heading"] + fov_half):
            e = (V[0] + L * math.sin(math.radians(h)), V[1] - L * math.cos(math.radians(h)))
            dr.line([V, e], fill=(255, 220, 0), width=2)
    # matched landmarks
    for info in detail.get("landmarks", []):
        if info:
            dr.line([A, P(*info["pos"])], fill=(0, 255, 120), width=3)
    for info in detail.get("absent", []):
        if info:
            dr.line([A, P(*info["pos"])], fill=(255, 60, 60), width=3)
    dr.ellipse([A[0] - 10, A[1] - 10, A[0] + 10, A[1] + 10], outline=(255, 40, 40), width=4)
    if viewer:
        dr.ellipse([V[0] - 9, V[1] - 9, V[0] + 9, V[1] + 9], fill=(0, 255, 120), outline=(0, 0, 0), width=2)
        dr.text((V[0] + 12, V[1] - 6), "you", fill=(0, 255, 120), stroke_width=2, stroke_fill=(0, 0, 0))
    dr.text((10, 10), f"{d['id']}  seed {d['seed']}  size {d['size']}  "
                      f"{anc['type']} {grid_ref(*a, d['size'])}", fill=(255, 255, 0), stroke_width=2, stroke_fill=(0, 0, 0))
    if out is None:
        return crop
    crop.save(out)
    return out


def biome_preview(map_id):
    for d, path in load_maps([map_id]):
        mi = MapImage(d, path)
        pal = {"snow": (255, 255, 255), "tundra": (150, 160, 190), "arid": (230, 190, 90),
               "temperate": (90, 170, 60), "jungle": (20, 90, 30), "water": (30, 60, 120)}
        lab = mi.labels()
        rgb = np.zeros(lab.shape + (3,), np.uint8)
        for k, c in pal.items():
            rgb[lab == k] = c
        out = path / "biomes.png"
        Image.fromarray(rgb).save(out)
        print(out)


# ---------------------------------------------------------------- cli
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch"); f.add_argument("maps", nargs="+")
    l = sub.add_parser("list"); l.add_argument("map"); l.add_argument("--type", default="")
    b = sub.add_parser("bearing")
    b.add_argument("--heading", type=float, required=True, help="compass value at screen centre")
    b.add_argument("--x", type=float, required=True, help="screen x of the object")
    b.add_argument("--width", type=float, default=1920); b.add_argument("--height", type=float, default=1080)
    b.add_argument("--fov", type=float, default=75, help="Rust graphics FOV setting (vertical)")
    fi = sub.add_parser("find"); fi.add_argument("clues"); fi.add_argument("--maps", nargs="*")
    fi.add_argument("--top", type=int, default=10)
    fi.add_argument("--org", help="only maps currently run by servers whose name contains this")
    fi.add_argument("--region", nargs="+", choices=REGIONS, help="only servers in these regions")
    fi.add_argument("--include-custom", action="store_true", help="also search custom (seed 1 / edited) maps")
    u = sub.add_parser("ui", help="open the clue-builder web UI")
    u.add_argument("--port", type=int, default=8765)
    u.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    sv = sub.add_parser("servers", help="find popular servers on rustmaps and download their current maps")
    sv.add_argument("terms", nargs="*", help=f"server-name search terms (default: {', '.join(DEFAULT_ORGS)})")
    sv.add_argument("--include-empty", action="store_true", help="also servers with no players online")
    sv.add_argument("--list", action="store_true", help="just print the saved server index")
    sy = sub.add_parser("sync", help="re-check tracked servers: download maps after wipes, delete old maps")
    sy.add_argument("--dry-run", action="store_true", help="show which old maps would be deleted, delete nothing")
    sy.add_argument("--no-prune", action="store_true", help="download new maps but keep old ones")
    bi = sub.add_parser("biome"); bi.add_argument("map")
    a = ap.parse_args()
    load_env()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # server names carry emoji

    if a.cmd == "fetch":
        for m in a.maps:
            fetch(m)
    elif a.cmd == "servers":
        if a.list:
            for mid, ss in servers_by_map().items():
                for s in ss:
                    print(f"{mid[:8]}  {s['lastWipeUtc'][:10]}  {s['region']:6} {s['name']}")
        else:
            update_servers(a.terms or DEFAULT_ORGS, with_players=not a.include_empty)
    elif a.cmd == "sync":
        sync(prune=not a.no_prune, dry_run=a.dry_run)
    elif a.cmd == "list":
        for d, _ in load_maps([a.map]):
            for m in d["monuments"]:
                if a.type.lower() in m["type"].lower():
                    c = m["coordinates"]
                    print(f"{m['type']:32} ({c['x']:6}, {c['y']:6})  {grid_ref(c['x'], c['y'], d['size'])}")
    elif a.cmd == "bearing":
        print(round(screen_bearing(a.heading, a.x, a.width, a.height, a.fov), 1))
    elif a.cmd == "find":
        find(json.loads(Path(a.clues).read_text()), a.maps, a.top, org=a.org, classic_only=not a.include_custom,
             regions=a.region)
    elif a.cmd == "biome":
        biome_preview(a.map)
    elif a.cmd == "ui":
        import ui
        ui.serve(a.port, open_browser=not a.no_browser)


if __name__ == "__main__":
    main()
