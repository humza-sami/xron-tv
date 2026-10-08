#!/usr/bin/env python3
"""Builds channels/channels.json from the public iptv-org database.

For every configured country it collects channels, drops closed / NSFW /
blocklisted ones, probes each stream URL and keeps only the ones that answer
with a valid HLS playlist. Entries in channels/manual.json are merged in on top
(use it to add your own links, hide channels, or list "featured" channels
that should appear first).

Usage:  python3 tools/build_channels.py [--no-probe] [--countries PK,IN]
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import json
import pathlib
import re
import shutil
import ssl
import subprocess
import time
import urllib.request

API = "https://iptv-org.github.io/api"
ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "channels" / "channels.json"
MANUAL = ROOT / "channels" / "manual.json"
ASSET = ROOT / "app" / "src" / "main" / "assets" / "channels.json"

DEFAULT_COUNTRIES = ["PK", "IN"]
MAX_STREAMS_PER_CHANNEL = 3
BROWSER_UA = ("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36")


def fetch_json(name):
    with urllib.request.urlopen(f"{API}/{name}.json", timeout=60) as r:
        return json.load(r)


_insecure = ssl.create_default_context()
_insecure.check_hostname = False
_insecure.verify_mode = ssl.CERT_NONE


FFPROBE = shutil.which("ffprobe")


def probe(stream):
    """Checks a stream. Returns {"latency": seconds, "height": video height or 0 if unknown},
    or None if it doesn't serve a playable HLS playlist / media stream."""
    headers = {"User-Agent": stream.get("user_agent") or BROWSER_UA}
    if stream.get("referrer"):
        headers["Referer"] = stream["referrer"]
    req = urllib.request.Request(stream["url"], headers=headers)
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=10, context=_insecure) as r:
            if r.status != 200:
                return None
            head = r.read(65536)
            ctype = (r.headers.get("Content-Type") or "").lower()
            ok = b"#EXTM3U" in head or ctype.startswith(("video/", "audio/")) \
                or "mpegurl" in ctype
            if not ok:
                return None
            latency = time.monotonic() - start
    except Exception:
        return None

    # A master playlist lists its quality levels; take the best one.
    heights = [int(h) for h in re.findall(rb"RESOLUTION=\d+x(\d+)", head)]
    height = max(heights) if heights else measure_height(stream, headers)
    return {"latency": latency, "height": height}


def measure_height(stream, headers):
    """Asks ffprobe for the real video height when the playlist doesn't say. 0 if unknown."""
    if not FFPROBE:
        return 0
    cmd = [FFPROBE, "-v", "error", "-rw_timeout", "10000000", "-user_agent", headers["User-Agent"]]
    if "Referer" in headers:
        cmd += ["-headers", f"Referer: {headers['Referer']}\r\n"]
    cmd += ["-select_streams", "v", "-show_entries", "stream=height", "-of", "csv=p=0", stream["url"]]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=25).stdout
        return max((int(x) for x in re.findall(r"\d+", out)), default=0)
    except Exception:
        return 0


def is_paid(channel, rules):
    """True for channels matching manual.json "hide_rules": paid networks, sports, movies, etc.
    Channels in an exempt category (news by default) are kept even if their network is listed."""
    cats = set(channel.get("categories") or [])
    if cats & set(rules.get("categories", [])):
        return True
    if cats & set(rules.get("exempt_categories", [])):
        return False
    owners = " ".join(channel.get("owners") or []) + " " + (channel.get("network") or "")
    if any(o.lower() in owners.lower() for o in rules.get("owners", [])):
        return True
    return any(re.search(p, channel["name"], re.IGNORECASE) for p in rules.get("name_patterns", []))


def stream_height(stream, measured):
    """Measured height, else the height iptv-org lists for the stream, else 0."""
    if measured.get(stream["url"]):
        return measured[stream["url"]]
    m = re.match(r"(\d+)", stream.get("quality") or "")
    return int(m.group(1)) if m else 0


def quality_tier(h):
    return 3 if h >= 1080 else 2 if h >= 720 else 1 if h >= 480 else 0


def pick_logo(logos):
    usable = [l for l in logos if l.get("format") in ("PNG", "JPEG", "WebP")]
    if not usable:
        return None
    usable.sort(key=lambda l: (bool(l.get("in_use")), l.get("feed") is None,
                               (l.get("width") or 0)), reverse=True)
    return usable[0]["url"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-probe", action="store_true", help="skip link checking")
    ap.add_argument("--countries", default=",".join(DEFAULT_COUNTRIES))
    args = ap.parse_args()
    wanted = [c.strip().upper() for c in args.countries.split(",") if c.strip()]

    print("Downloading iptv-org database...")
    channels = fetch_json("channels")
    streams = fetch_json("streams")
    logos = fetch_json("logos")
    countries = {c["code"]: c for c in fetch_json("countries")}
    blocked = {b["channel"] for b in fetch_json("blocklist")}

    chans = {c["id"]: c for c in channels
             if c.get("country") in wanted and not c.get("is_nsfw")
             and not c.get("closed") and c["id"] not in blocked}

    logos_by_ch = {}
    for l in logos:
        if l["channel"] in chans:
            logos_by_ch.setdefault(l["channel"], []).append(l)

    candidates = [s for s in streams if s.get("channel") in chans]
    print(f"{len(chans)} channels, {len(candidates)} candidate streams")

    latency, height = {}, {}
    if args.no_probe:
        alive = candidates
    else:
        print("Probing streams (this takes a few minutes)...")
        if not FFPROBE:
            print("ffprobe not found: video quality is only known for some streams")
        with cf.ThreadPoolExecutor(max_workers=48) as pool:
            results = list(pool.map(probe, candidates))
        alive = [s for s, r in zip(candidates, results) if r is not None]
        latency = {s["url"]: r["latency"] for s, r in zip(candidates, results) if r is not None}
        height = {s["url"]: r["height"] for s, r in zip(candidates, results) if r is not None}
        print(f"{len(alive)} streams responded")

    streams_by_ch = {}
    for s in alive:
        streams_by_ch.setdefault(s["channel"], []).append(s)

    manual = json.loads(MANUAL.read_text()) if MANUAL.exists() else {}
    hidden = set(manual.get("hide", []))
    rules = manual.get("hide_rules", {})
    featured = {cid: i for i, cid in enumerate(manual.get("featured", []))}

    out_countries = []
    for code in wanted:
        items = []
        for cid, c in chans.items():
            if c["country"] != code or cid in hidden or cid not in streams_by_ch:
                continue
            if is_paid(c, rules):
                continue
            # Reliable streams first, then the sharpest picture, then the fastest-responding one.
            ss = sorted(streams_by_ch[cid], key=lambda s: (
                "Geo-blocked" in s["labels"],
                "Not 24/7" in s["labels"],
                -quality_tier(stream_height(s, height)),
                round(latency.get(s["url"], 5.0), 1)))
            best_height = max(stream_height(s, height) for s in ss[:MAX_STREAMS_PER_CHANNEL])
            items.append({
                "id": cid,
                "name": c["name"],
                "logo": pick_logo(logos_by_ch.get(cid, [])),
                "category": (c.get("categories") or ["general"])[0],
                "height": best_height,
                "streams": [{k: v for k, v in {
                    "url": s["url"],
                    "referrer": s.get("referrer"),
                    "userAgent": s.get("user_agent"),
                }.items() if v} for s in ss[:MAX_STREAMS_PER_CHANNEL]],
            })
        for extra in manual.get("add", []):
            if extra.get("country") == code:
                items = [i for i in items if i["id"] != extra["id"]]
                items.append({k: v for k, v in extra.items() if k != "country"})
        # Featured (popular) channels first in the listed order, then the rest A to Z.
        items.sort(key=lambda i: (featured.get(i["id"], len(featured)), i["name"].lower()))
        info = countries.get(code, {"name": code, "flag": ""})
        out_countries.append({"code": code, "name": info["name"],
                              "flag": info.get("flag", ""), "channels": items})
        hd = sum(1 for i in items if i.get("height", 0) >= 720)
        fhd = sum(1 for i in items if i.get("height", 0) >= 1080)
        print(f"{code}: {len(items)} working channels, {hd} HD (720p+), {fhd} Full HD (1080p)")

    data = {"version": 1,
            "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "countries": out_countries}
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    OUT.write_text(text, encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)}")
    if ASSET.parent.parent.exists():
        ASSET.parent.mkdir(parents=True, exist_ok=True)
        ASSET.write_text(text, encoding="utf-8")
        print(f"Wrote {ASSET.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
