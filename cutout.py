"""Your own top-down pictures of monuments ("cut-outs") for the sketch board.

Take a straight-down screenshot of a monument and remove the background (transparent PNG). Then
line it up on the matching page of the UI (http://127.0.0.1:8765/cutouts), or from the command line:

    python rustloc.py cutout my_harbor.png "Large Harbor" --map <rustmaps url> --heading 320

The picture is matched against the same monument on rustmaps' map tiles (the very one it was taken
of, with --map) to find its scale, its centre and its rotation. It is saved to cutouts/<name>.png:
centred on the monument centre, SCALE px per metre, train-tunnel entrance pointing up. What was
saved, and how it was lined up, is logged in cutouts/cutouts.json.
"""
import io
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

import rustloc as R

CUTOUTS = R.ROOT / "cutouts"
LOG = CUTOUTS / "cutouts.json"
SCALE = 2.0          # px per metre of a saved cut-out
SEA = (31, 70, 94)   # what rustmaps' map looks like past its edge


def slug(name):
    return re.sub(r"\W+", "_", name).lower()


def path_for(name):
    p = CUTOUTS / f"{slug(name)}.png"
    return p if p.exists() else None


def half_metres(name):
    """Half the width of a saved cut-out in metres (it is centred on the monument), or None."""
    p = path_for(name)
    if not p:
        return None
    with Image.open(p) as im:
        return round(im.width / 2 / SCALE, 1)


def load_log():
    try:
        return json.loads(LOG.read_text())
    except Exception:
        return {}


def heading_from_name(filename):
    """'Large harbor heading 320.png' -> 320.0 (the compass reading when the screenshot was taken)."""
    m = re.search(r"(\d{1,3}(?:\.\d+)?)", re.sub(r"\.\w+$", "", filename))
    return float(m.group(1)) % 360 if m else None


# ---------------------------------------------------------------- reference crops from rustmaps tiles
_MAP_CACHE = {}


def map_id_of(text):
    return text.strip().rstrip("/").split("/")[-1].split("?")[0]


def _map(map_id):
    if map_id not in _MAP_CACHE:
        d = R.api(f"/internal/v1/maps/{map_id}").get("data")
        if not d or not d.get("monuments"):
            raise ValueError(f"rustmaps has no data for map {map_id}")
        _MAP_CACHE[map_id] = d
    return _MAP_CACHE[map_id]


def instances(name, map_id=None, n=4):
    """Up to n (map data, tile folder, monument) of `name`: on the given rustmaps map, else on
    downloaded classic maps. Tunnel monuments only where their rotation is known."""
    from ui import base_type
    tunnel = R.tunnel_type(name)
    if map_id:
        map_id = map_id_of(map_id)
        maps = [(_map(map_id), R.MAPS / "_cutout_refs" / map_id[:8])]
    else:
        maps = ((json.loads((p / "map.json").read_text()), p)
                for p in sorted(R.MAPS.iterdir()) if (p / "map.json").exists())
    out = []
    for d, folder in maps:
        if not d.get("tileBaseUrl") or not (map_id or R.is_classic(d)):
            continue
        for m in d["monuments"]:
            if base_type(m["type"]) == name and (not tunnel or R.facing(d, m) is not None):
                out.append((d, folder, m))
                break
        if len(out) >= n:
            break
    return out


def _tile(d, folder, z, x, y):
    """A map tile, cached with the map's other tiles (same layout as the UI's live map).
    None past the map's edge (rustmaps has no tiles there: open sea)."""
    p = folder / "tiles" / str(z) / f"{x}_{y}.webp"
    if not p.exists():
        try:
            b = R.http_get(d["tileBaseUrl"].format(z=z, x=x, y=y))
        except R.urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b)
    return Image.open(p).convert("RGB")


def half_for(m):
    return int(max(40, R.monument_radius(m)) * 1.5 + 60)


def crop(d, folder, m, res=1.0, half=None):
    """The map around monument m, 2*half metres square at res px/m (1 or 2), centred on it;
    tunnel monuments turned so their entrance points up."""
    half = half or half_for(m)
    z = 0 if res <= 1 else 1
    tm = 256 / 2 ** z                                   # metres per tile
    f = (R.facing(d, m) or 0) if R.tunnel_type(m["type"]) else 0
    cx, cy = m["coordinates"]["x"], -m["coordinates"]["y"]        # tile space: y down
    r = half * 1.45                                     # room to turn without empty corners
    x0, x1 = math.floor((cx - r) / tm), math.floor((cx + r) / tm)
    y0, y1 = math.floor((cy - r) / tm), math.floor((cy + r) / tm)
    big = Image.new("RGB", ((x1 - x0 + 1) * 256, (y1 - y0 + 1) * 256), SEA)
    jobs = [(tx, ty) for tx in range(x0, x1 + 1) for ty in range(y0, y1 + 1)]
    with ThreadPoolExecutor(8) as ex:
        for (tx, ty), t in zip(jobs, ex.map(lambda j: _tile(d, folder, z, *j), jobs)):
            if t:
                big.paste(t, ((tx - x0) * 256, (ty - y0) * 256))
    k = 256 / tm                                        # px per metre of the tiles
    px, py = (cx - x0 * tm) * k, (cy - y0 * tm) * k
    out = big.crop((round(px - r * k), round(py - r * k), round(px + r * k), round(py + r * k)))
    out = out.rotate(f, Image.BICUBIC, fillcolor=SEA)
    if k != res:
        out = out.resize((round(out.width * res / k),) * 2, Image.LANCZOS)
    c, h = out.width / 2, half * res
    return out.crop((round(c - h), round(c - h), round(c + h), round(c + h)))


# ---------------------------------------------------------------- automatic matching
def _monument_mask(a):
    """rustmaps paints monument ground in a tan colour: 1 there, 0 on grass / snow / sand / water."""
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    return ((r - b > 45) & (r > g) & (g - b > 15) & (r < 235)).astype(float)


def _edges(gray):
    """Blurred edge strength: buildings, roads and shore lines look alike in both renders."""
    g = np.asarray(gray.filter(ImageFilter.GaussianBlur(1))).astype(float)
    gy, gx = np.gradient(g)
    e = Image.fromarray(np.clip(np.hypot(gx, gy) * 4, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.5))
    return np.asarray(e).astype(float)


class _Ref:
    """FFTs of one reference (edges and monument-ground mask), at some px/m."""
    def __init__(self, edges, mask):
        self.n = n = edges.shape[0]
        F = np.fft.rfft2
        self.Fe, self.Fe2 = F(edges), F(edges ** 2)
        self.Ft = F(mask - mask.mean())
        self.nt = math.sqrt((mask ** 2).sum() + 1e-9)


def _refs_at(images, res, average):
    """_Ref(s) for reference crops (made at 1 px/m) scaled to res px/m."""
    packs = []
    for im in images:
        if res != 1:
            im = im.resize((round(im.width * res),) * 2, Image.BILINEAR)
        packs.append((_edges(im.convert("L")), _monument_mask(np.asarray(im).astype(float))))
    if average:
        packs = [(np.mean([p[0] for p in packs], 0), np.mean([p[1] for p in packs], 0))]
    return [_Ref(e, t) for e, t in packs]


def _score(ref, gray, alpha):
    """Best (score, shift) of a placed picture (gray + alpha at the reference's px/m) on the
    reference: edge likeness inside the picture + outline overlap with the monument ground. The
    monument's centre (middle of the reference) has to land on the picture itself."""
    n = ref.n
    if alpha.shape[0] >= n or alpha.shape[1] >= n:
        return -9, (0, 0)
    F, I = np.fft.rfft2, (lambda a: np.fft.irfft2(a, s=(n, n)))
    pad = lambda a: np.pad(a, ((0, n - a.shape[0]), (0, n - a.shape[1])))
    msk = (alpha > .5).astype(float)
    cnt = msk.sum()
    if cnt < 50:
        return -9, (0, 0)
    e = _edges(gray) * msk
    e = e - msk * (e.sum() / cnt)
    t, mk = pad(e), F(pad(msk))
    num = I(ref.Fe * np.conj(F(t)))
    s1, s2 = I(ref.Fe * np.conj(mk)), I(ref.Fe2 * np.conj(mk))
    edge = num / np.sqrt(np.maximum(s2 - s1 ** 2 / cnt, 1e-6) * ((e ** 2).sum() + 1e-9))
    a = pad(alpha)
    sil = I(ref.Ft * np.conj(F(a - a.mean()))) / (ref.nt * math.sqrt((alpha ** 2).sum() + 1e-9))
    c = edge + sil
    ys, xs = np.nonzero(msk)
    ok = np.zeros((n, n), bool)
    ok[(n // 2 - ys) % n, (n // 2 - xs) % n] = True
    c = np.where(ok, c, -9)
    k = np.unravel_index(np.argmax(c), c.shape)
    return c[k], k


def _search(refs, img, res, thetas, scales):
    gray = Image.new("RGBA", img.size, (128, 128, 128, 255))
    gray.alpha_composite(img)
    gray, alpha = gray.convert("L"), img.getchannel("A")
    best = (-9,)
    for s in scales:
        size = (max(1, round(img.width / s * res)), max(1, round(img.height / s * res)))
        g_s, a_s = gray.resize(size, Image.LANCZOS), alpha.resize(size, Image.BILINEAR)
        for th in thetas:
            g = g_s.rotate(th, Image.BILINEAR, expand=True, fillcolor=128)
            a = np.asarray(a_s.rotate(th, Image.BILINEAR, expand=True)).astype(float) / 255
            for i, ref in enumerate(refs):
                v, k = _score(ref, g, a)
                if v > best[0]:
                    best = (v, th, s, k, a.shape, i)
    return best


def predicted_theta(name, heading, map_id=None):
    """Rotation (deg anticlockwise) that makes a picture taken facing `heading` entrance-up (tunnel
    monuments, needs the map it was taken on) or north-up (others)."""
    if heading is None:
        return None
    if R.tunnel_type(name):
        if not map_id:
            return None
        inst = instances(name, map_id, n=1)
        return (R.facing(inst[0][0], inst[0][2]) - heading) % 360 if inst else None
    return (-heading) % 360


def fit(img, name, map_id=None, heading=None, log=print):
    """Line the picture up with the monument automatically. Returns {theta: degrees anticlockwise,
    s: picture px per metre, cx, cy: monument centre in picture px, score}."""
    box = img.getchannel("A").point(lambda a: 255 if a > 128 else 0).getbbox()
    if not box:
        raise ValueError("the picture has no transparent background (cut the ground and water away first)")
    pic = img.crop(box)
    inst = instances(name, map_id)
    if not inst:
        raise ValueError(f"no {'such' if map_id else 'downloaded classic'} map has a {name} (with tiles) to compare with")
    radius = max(40, R.monument_radius(inst[0][2]))
    images = [crop(d, f, m, 1.0) for d, f, m in inst]
    tunnel = bool(R.tunnel_type(name))
    # Tunnel monuments: all crops are entrance-up, so average them. Others (rotation unknown on
    # rustmaps): try each crop on its own.
    average = tunnel and len(images) > 1
    pred = predicted_theta(name, heading, map_id)
    L = max(pic.size)
    scales = np.geomspace(L / (2.8 * radius), L / (1.0 * radius), 16)
    thetas = np.arange(pred - 4, pred + 4.1, 2) if pred is not None else range(0, 360, 6)
    b = _search(_refs_at(images, 0.5, average), pic, 0.5, thetas, scales)          # coarse, 2 m/px
    _, th, s = b[:3]
    fine_t = np.arange(th - 2, th + 2.1, 0.5)
    refs1 = _refs_at(images, 1.0, average)
    if not average:
        refs1 = [refs1[b[5]]]
    score, th, s, (dy, dx), (mh, mw), _ = _search(refs1, pic, 1.0, fine_t, np.geomspace(s / 1.08, s * 1.08, 9))
    n = refs1[0].n
    dy, dx = (dy - n if dy > n / 2 else dy), (dx - n if dx > n / 2 else dx)
    # monument centre in the turned, scaled picture -> back into the picture's own pixels
    vx, vy = (n / 2 - dx) - mw / 2, (n / 2 - dy) - mh / 2
    t = math.radians(th)
    ux, uy = vx * math.cos(t) - vy * math.sin(t), vx * math.sin(t) + vy * math.cos(t)
    cx, cy = ux * s + pic.width / 2 + box[0], uy * s + pic.height / 2 + box[1]
    if not tunnel and not map_id:
        th = 0.0        # the crop's rotation means nothing: keep the picture as it was taken
    return {"theta": round(float(th) % 360, 2), "s": round(float(s), 4), "cx": round(float(cx), 1),
            "cy": round(float(cy), 1), "score": round(float(score), 3)}


# ---------------------------------------------------------------- saving
def normalise(img, theta, s, cx, cy):
    """Scale to SCALE px/m, turn theta degrees anticlockwise about the centre and crop to a square
    canvas centred on it."""
    f = SCALE / s
    img = img.resize((max(1, round(img.width * f)), max(1, round(img.height * f))), Image.LANCZOS)
    cx, cy = cx * f, cy * f
    half = math.ceil(max(cx, cy, img.width - cx, img.height - cy))
    canvas = Image.new("RGBA", (2 * half, 2 * half))
    canvas.paste(img, (round(half - cx), round(half - cy)))
    canvas = canvas.rotate(theta, Image.BICUBIC, expand=True)
    box = canvas.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    c = canvas.width / 2
    r = math.ceil(max(c - box[0], c - box[1], box[2] - c, box[3] - c)) + 2
    return canvas.crop((round(c - r), round(c - r), round(c + r), round(c + r)))


def save(img, name, theta, s, cx, cy, source="", map_id=None, heading=None, method="manual", score=None):
    """Write cutouts/<name>.png and log how it was lined up in cutouts/cutouts.json."""
    out = normalise(img, theta, s, cx, cy)
    CUTOUTS.mkdir(exist_ok=True)
    dst = CUTOUTS / f"{slug(name)}.png"
    out.save(dst, optimize=True)
    log = load_log()
    log[slug(name)] = {
        "monument": name, "source": source, "map": map_id_of(map_id) if map_id else None,
        "heading": heading, "method": method, "score": score,
        "rotation_ccw": round(theta % 360, 2), "px_per_m": round(s, 4),
        "centre_px": [round(cx, 1), round(cy, 1)], "picture_px": list(img.size),
        "size_m": round(out.width / SCALE, 1), "saved": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    LOG.write_text(json.dumps(log, indent=2))
    return dst, out


def make(src, name, map_id=None, heading=None, log=print):
    """Command line: match automatically, save, and write a check picture."""
    img = Image.open(src).convert("RGBA")
    if heading is None and "heading" in str(src).lower():
        heading = heading_from_name(str(src).replace("\\", "/").split("/")[-1])
    p = fit(img, name, map_id=map_id, heading=heading, log=log)
    dst, out = save(img, name, p["theta"], p["s"], p["cx"], p["cy"], source=str(src).replace("\\", "/").split("/")[-1],
                    map_id=map_id, heading=heading, method="auto", score=p["score"])
    d, folder, m = instances(name, map_id, n=1)[0]
    chk = crop(d, folder, m, SCALE).convert("RGBA")
    chk.paste(out, (round(chk.width / 2 - out.width / 2),) * 2, out.getchannel("A").point(lambda a: a * 3 // 4))
    ImageDraw.Draw(chk).ellipse((chk.width / 2 - 5, chk.height / 2 - 5, chk.width / 2 + 5, chk.height / 2 + 5), fill="red")
    chk_path = R.MAPS / "cutout_check.png"
    chk.convert("RGB").save(chk_path)
    th = p["theta"]
    turn = f"{th:.1f} deg anticlockwise" if th <= 180 else f"{360 - th:.1f} deg clockwise"
    log(f"{name}: {p['s']:.2f} px per metre, turned {turn}, match {p['score']:.2f}")
    log(f"saved {dst}  ({out.width} px = {out.width / SCALE:.0f} m across)")
    log(f"check: {chk_path}  (red dot = monument centre). Not right? Line it up by hand at /cutouts in the UI.")
    return dst
