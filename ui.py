"""Local web UI for rustloc: build clues ("Lighthouse in temperate, Ferry Terminal to the west...")
and search every downloaded server map. Start with:  python rustloc.py ui"""
import io
import json
import os
import re
import threading
import time
import urllib.parse
import webbrowser
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

import rustloc as R

HERE = Path(__file__).resolve().parent
# Pictures are downloaded once into assets/ and served from there (works offline afterwards).
ASSETS = HERE / os.environ.get("RUSTLOC_ASSETS", "assets")
PHOTOS = json.loads((HERE / "monument_images.json").read_text()) if (HERE / "monument_images.json").exists() else {}
# rustmaps' single preview images for monuments that have no photo gallery
for _n in ("Airfield", "Arctic Research Base", "Excavator", "Gas Station", "Junkyard", "Large Harbor",
           "Large Oilrig", "Launch Site", "Lighthouse", "Military Tunnels", "Nuclear Missile Silo", "Powerplant",
           "Satellite Dish", "Sewer Branch", "Small Harbor", "Small Oilrig", "Sphere Tank", "Supermarket",
           "Trainyard", "Warehouse", "Water Treatment"):
    PHOTOS.setdefault(_n, [f"https://content.rustmaps.com/assets/monuments/img/{_n.replace(' ', '')}.png"])
ICON_PATHS = {}  # base type -> rustmaps icon path
POOL = {}        # map_id8 -> MapImage (monument JSON + lazily loaded biome grid / image)
META = {}
LOCK = threading.Lock()


def base_type(t):
    """'Power Substation Big 1' -> 'Power Substation Big', 'Military Base D' -> 'Military Base'."""
    return re.sub(r"(\s+([A-Da-d]|\d+))+$", "", t)


def category(name, size_cat):
    n = name.lower()
    if "rock" in n:
        return "Rocks & cliffs"
    if "power" in n and "plant" not in n:
        return "Power lines"
    if n.startswith("cave"):
        return "Caves"
    if "tunnel" in n or "bunker" in n:
        return "Tunnel entrances"
    if any(k in n for k in ("ice", "lake", "oasis", "swamp", "canyon", "well")):
        return "Water, ice & terrain"
    if size_cat == "LargeMonument":
        return "Large monuments"
    if size_cat == "SmallMonument":
        return "Small monuments"
    return "Other"


def photo_key(base):
    """rustmaps photo set for a base type (photos are keyed by full names like 'Water Well A')."""
    if base in PHOTOS:
        return base
    return next((k for k in sorted(PHOTOS) if base_type(k) == base), None)


def load_pool():
    POOL.clear()
    types, icons, sizes = Counter(), {}, defaultdict(Counter)
    for d, path in R.load_maps():
        POOL[d["id"][:8]] = R.MapImage(d, path)
        for m in d["monuments"]:
            b = base_type(m["type"])
            icons.setdefault(b, m.get("iconPath"))
            sizes[b][m.get("sizeCategory")] += 1
        for t in {base_type(m["type"]) for m in d["monuments"]}:
            types[t] += 1
    ICON_PATHS.update({t: p for t, p in icons.items() if p})
    servers = R.servers_by_map()
    classic = {k for k, mi in POOL.items() if R.is_classic(mi.d)}
    tlist = []
    for t, n in sorted(types.items(), key=lambda kv: (-kv[1], kv[0])):
        cat = category(t, sizes[t].most_common(1)[0][0])
        pk = photo_key(t)
        tlist.append({
            "name": t, "maps": n, "cat": cat,
            "photo": f"/api/photo?name={urllib.parse.quote(pk)}" if pk else None,
            "photos": len(PHOTOS.get(pk, [])) if pk else 0,
            "icon": f"/api/icon?type={urllib.parse.quote(t)}" if icons.get(t) and not icon_missing(t) else None,
            "thumb": f"/api/thumb?type={urllib.parse.quote(t)}" if cat in ("Large monuments", "Small monuments") else None,
            "tunnel": bool(R.tunnel_type(t)),
            "radius": R.monument_radius({"type": t, "sizeCategory": sizes[t].most_common(1)[0][0]}),
        })
    META.update({
        "maps": len(POOL),
        "classic_maps": len(classic),
        "servers": sum(len(v) for k, v in servers.items() if k[:8] in POOL),
        "classic_servers": sum(len(v) for k, v in servers.items() if k[:8] in classic),
        "types": tlist,
        "biomes": [b for b in R.BIOMES if b != "water"],
        "tunnel_types": list(R.TUNNEL_OFFSET),
        "regions": dict(Counter(sv["region"] for k, v in servers.items() if k[:8] in classic for sv in v)),
    })


# ---------------------------------------------------------------- background sync after wipes
SYNC_HOURS = float(os.environ.get("RUSTLOC_SYNC_HOURS", 6))   # 0 = only when you press "Sync now"
SYNC = {"running": False, "log": [], "last": R.last_sync()}
SYNC_LOCK = threading.Lock()


def run_sync():
    if not SYNC_LOCK.acquire(blocking=False):
        return                                          # one already running
    SYNC.update(running=True, log=[])

    def log(msg):
        SYNC["log"] = (SYNC["log"] + [msg])[-200:]
    try:
        known = json.loads(R.SERVERS.read_text()) if R.SERVERS.exists() else {}
        terms = sorted(set(R.DEFAULT_ORGS) | {v.get("query") for v in known.values() if v.get("query")})
        res = R.update_servers(terms, log=log)          # network + downloads: no lock needed
        with LOCK:                                      # searches read map folders: swap under the lock
            res["removed"] = R.prune_maps(log=log)
            load_pool()
        prefetch_assets(log=log)                        # pictures for any new monument types
        R.SYNC_FILE.write_text(json.dumps({"time": time.time(), **res}, indent=1))
        SYNC["last"] = R.last_sync()
        log("done")
    except Exception as e:
        log(f"sync failed: {type(e).__name__}: {e}")
    finally:
        SYNC["running"] = False
        SYNC_LOCK.release()


def sync_loop():
    while SYNC_HOURS > 0:
        last = SYNC["last"] or {}
        # right after a wipe rustmaps is often still rendering the new map: retry those sooner
        hours = min(SYNC_HOURS, 0.5) if last.get("failed") else SYNC_HOURS
        wait = last.get("time", 0) + hours * 3600 - time.time()
        if wait <= 0:
            run_sync()
        else:
            time.sleep(min(wait, 600))


def sync_status():
    last = SYNC["last"] or {}
    # servers whose rustmaps wipe date is within the last 24 h
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 86400))
    recent = sorted(({"name": s["name"], "at": s.get("lastWipeUtc")} for ss in R.servers_by_map().values() for s in ss
                     if (s.get("lastWipeUtc") or "") >= since), key=lambda s: s["at"], reverse=True)
    return {"running": SYNC["running"], "log": SYNC["log"][-12:], "every_hours": SYNC_HOURS, "wiped_24h": recent,
            "last": {k: last.get(k) for k in ("time", "removed", "downloaded", "failed")} if last else None}


def public(r):
    out = {k: v for k, v in r.items() if not k.startswith("_")}
    out["map8"] = r["map"][:8]
    return out


def do_search(body):
    clues = body["clues"]
    with LOCK:  # biome grids are built lazily on first use; keep that single-threaded
        res = R.search(clues, org=body.get("org") or None, top=0, pool=POOL,
                       classic_only=not body.get("include_custom"), regions=body.get("regions") or None)
    total = len(res)
    view = body.get("view")
    keep = 400 if view else int(body.get("top", 60))
    res = res[:keep]
    for r in res:
        r["viewer"] = None
        if view:
            # Triangulate where the player stood from the two landmarks' on-screen bearings
            info = next((v for v in r["detail"]["landmarks"] if v), None)
            if info and view.get("at_bearing") is not None and view.get("lm_bearing") is not None:
                tri = R.triangulate((r["x"], r["y"]), view["at_bearing"], info["pos"], view["lm_bearing"])
                if tri:
                    v, d1, d2 = tri
                    r["viewer"] = {"x": v[0], "y": v[1], "grid": R.grid_ref(*v, r["size"]),
                                   "dist_at": d1, "dist_lm": d2, "biome": r["_mi"].biome_at(*v)}
    if view:
        # Halve the score where the bearings put the player in the sea, or (if given) too far away
        for r in res:
            v = r["viewer"]
            too_far = view.get("max_dist") and max(v["dist_at"], v["dist_lm"]) > view["max_dist"] if v else True
            if not v or v["biome"] == "water" or too_far:
                r["score"] = round(r["score"] * 0.5, 3)
        res.sort(key=lambda r: -r["score"])
    return {"total": total, "results": [public(r) for r in res[: int(body.get("top", 60))]]}


def do_render(body):
    mi = POOL[body["map8"]]
    d = mi.d
    anchor = next(m for m in d["monuments"]
                  if m["coordinates"]["x"] == body["x"] and m["coordinates"]["y"] == body["y"])
    v, c = body.get("viewer"), body.get("center")
    img = R.render_candidate(mi, d, anchor, body.get("detail") or {}, body.get("clues") or {}, None,
                             span=float(body.get("span", 700)), viewer=(v["x"], v["y"]) if v else None,
                             center=tuple(c) if c else None, out_px=int(body.get("px", 900)))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def map_objects(map8):
    """Everything rustmaps lists on one map (monuments, rocks, power lines, ...) for the live map."""
    mi = POOL[map8]
    cats = {t["name"]: t["cat"] for t in META.get("types", [])}
    out = []
    for m in mi.d["monuments"]:
        b = base_type(m["type"])
        cat = cats.get(b) or category(b, m.get("sizeCategory"))
        x, y = m["coordinates"]["x"], m["coordinates"]["y"]
        out.append({"t": m["type"], "b": b, "x": x, "y": y, "cat": cat, "g": R.grid_ref(x, y, mi.size),
                    "tier": 0 if cat == "Large monuments" else 1 if cat == "Small monuments" else 2})
    return {"size": mi.size, "objects": out}


def map_tile(map8, z, x, y):
    """rustmaps' sharper map tiles (256 px; at zoom z a tile is 256/2^z metres, tile (x, y) starts at
    world x*m, y -y*m). Fetched on demand and cached in the map's folder, so they go with the map."""
    mi = POOL[map8]
    base = mi.d.get("tileBaseUrl")
    if not base or not -2 <= z <= 2 or max(abs(x), abs(y)) > 64:
        return None
    return cached(mi.path / "tiles" / str(z) / f"{x}_{y}.webp",
                  lambda: R.http_get(base.format(z=z, x=x, y=y)))


def cached(path, make):
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(make())
    return path.read_bytes()


def photo(name, i=0):
    """First rustmaps photo of an object type, cached as a 360 px JPEG."""
    urls = PHOTOS.get(name) or []
    if not urls:
        return None

    def make():
        # some gallery entries are gone (404): fall through to the next photo
        start, err = min(i, len(urls) - 1), None
        for u in urls[start:] + urls[:start]:
            try:
                im = Image.open(io.BytesIO(R.http_get(u))).convert("RGB")
                break
            except Exception as e:
                err = e
        else:
            raise err
        im.thumbnail((360, 360))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82)
        return buf.getvalue()
    slug = re.sub(r"\W+", "_", name).lower()
    return cached(ASSETS / "photos" / f"{slug}_{i}.jpg", make)


def icon_missing(base):
    """rustmaps has no icon for a few types (404); remembered so they aren't asked for again."""
    return (ASSETS / "icons" / (re.sub(r"\W+", "_", base).lower() + ".missing")).exists()


def icon(base):
    """rustmaps' map icon for a type (SVG)."""
    path = ICON_PATHS.get(base)
    if not path or icon_missing(base):
        return None
    slug = re.sub(r"\W+", "_", base).lower()
    try:
        return cached(ASSETS / "icons" / f"{slug}.svg",
                      lambda: R.http_get(f"https://content.rustmaps.com/assets/{path}.svg"))
    except R.urllib.error.HTTPError as e:
        if e.code == 404:
            (ASSETS / "icons" / f"{slug}.missing").touch()
            return None
        raise


def thumb(base):
    """Top-down crop of a real instance from the map renders. Monuments with their own tunnel
    entrance are rotated so that entrance points up (north), i.e. this is 'facing 0'."""
    def make():
        tunnel = R.tunnel_type(base)
        for mi in POOL.values():
            if not R.is_classic(mi.d):
                continue
            for m in mi.d["monuments"]:
                if base_type(m["type"]) != base:
                    continue
                f = R.facing(mi.d, m) if tunnel else 0
                if f is None:
                    continue
                r_m = max(70, R.monument_radius(m) * 1.3)
                cx, cy = mi.px(m["coordinates"]["x"], m["coordinates"]["y"])
                r = mi.m2px(r_m * 1.45)
                im = mi.img.crop((int(cx - r), int(cy - r), int(cx + r), int(cy + r)))
                im = im.resize((420, 420), Image.LANCZOS).rotate(f, Image.BICUBIC)
                k = 420 * (1 - 1 / 1.45) / 2
                im = im.crop((int(k), int(k), int(420 - k), int(420 - k))).resize((220, 220), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, "PNG")
                return buf.getvalue()
        raise KeyError(base)
    slug = re.sub(r"\W+", "_", base).lower()
    return cached(ASSETS / "thumbs" / f"{slug}.png", make)


def prefetch_assets(log=print):
    """Download every picture the UI shows (photo + icon per type) and build the top-down
    thumbnails, so the picker never waits on the network. Already-present files are skipped.
    Also locates servers whose region isn't known yet (servers.json from before regions existed)."""
    try:
        if R.update_geo(log=log):
            with LOCK:
                load_pool()
    except Exception as e:
        log(f"server locations: {e}")
    done = failed = 0
    for t in list(META.get("types", [])):
        jobs = []
        pk = photo_key(t["name"])
        if pk:
            jobs.append(lambda pk=pk: photo(pk))
        if t.get("icon"):
            jobs.append(lambda n=t["name"]: icon(n))
        if t.get("thumb"):
            jobs.append(lambda n=t["name"]: thumb(n))
        for job in jobs:
            try:
                job()
                done += 1
            except Exception:
                failed += 1
    log(f"pictures: {done} ready in {ASSETS}" + (f", {failed} failed (retried next start)" if failed else ""))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype, cache=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if cache:
            self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            if url.path in ("/", "/index.html"):
                self.send(200, (HERE / "ui.html").read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/meta":
                self.send(200, json.dumps(META).encode(), "application/json")
            elif url.path == "/api/objects" and q.get("map8") in POOL:
                self.send(200, json.dumps(map_objects(q["map8"])).encode(), "application/json")
            elif url.path == "/api/mapimg" and q.get("map8") in POOL:
                self.send(200, (POOL[q["map8"]].path / "map.png").read_bytes(), "image/png", cache=True)
            elif url.path == "/api/tile" and q.get("map8") in POOL:
                try:
                    b = map_tile(q["map8"], int(q["z"]), int(q["x"]), int(q["y"]))
                except Exception:
                    b = None                                     # off the map edge (404) or offline
                self.send(200, b, "image/webp", cache=True) if b else self.send(404, b"", "text/plain")
            elif url.path == "/api/sync":
                self.send(200, json.dumps(sync_status()).encode(), "application/json")
            elif url.path == "/api/photos":
                self.send(200, json.dumps(PHOTOS.get(q.get("name"), [])).encode(), "application/json")
            elif url.path == "/api/photo":
                b = photo(q.get("name", ""), int(q.get("i", 0)))
                self.send(200, b, "image/jpeg", cache=True) if b else self.send(404, b"", "text/plain")
            elif url.path == "/api/icon":
                b = icon(q.get("type", ""))
                self.send(200, b, "image/svg+xml", cache=True) if b else self.send(404, b"", "text/plain")
            elif url.path == "/api/thumb":
                self.send(200, thumb(q.get("type", "")), "image/png", cache=True)
            else:
                self.send(404, b"not found", "text/plain")
        except Exception as e:
            self.send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode(), "application/json")

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.path == "/api/search":
                self.send(200, json.dumps(do_search(body)).encode(), "application/json")
            elif self.path == "/api/render":
                self.send(200, do_render(body), "image/png")
            elif self.path == "/api/sync":
                threading.Thread(target=run_sync, daemon=True).start()
                self.send(200, json.dumps({"started": True}).encode(), "application/json")
            elif self.path == "/api/reload":
                load_pool()
                self.send(200, json.dumps(META).encode(), "application/json")
            else:
                self.send(404, b"not found", "text/plain")
        except Exception as e:
            self.send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode(), "application/json")


def serve(port=8765, open_browser=True):
    load_pool()
    print(f"{META['maps']} maps, {META['servers']} servers loaded")
    threading.Thread(target=prefetch_assets, daemon=True).start()
    threading.Thread(target=sync_loop, daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"Rust-Snipe UI on {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    httpd.serve_forever()


if __name__ == "__main__":
    serve(open_browser=False)
