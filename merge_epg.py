#!/usr/bin/env python3
"""
merge_epg.py  -  builds ONE small guide (epg.xml.gz) for your playlist.

It downloads your provider's guide plus the epgshare01 files, keeps only the
channels listed in channels.txt, and writes them into a single XMLTV file.

Precedence: sources are read in the order listed in SOURCES. The first source
that has listings for a channel wins; later sources only fill gaps.

The provider link contains your login, so it is never written in this file.
It comes from the environment variable PROVIDER_EPG_URL (a GitHub secret).
"""
import gzip
import io
import os
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

SOURCES = [
    "env:PROVIDER_EPG_URL",   # provider 1 (jordan-tv) xmltv.php link (secret)
    "env:PROVIDER2_EPG_URL",  # provider 2 (tv-general) xmltv.php link (secret)
    "https://epgshare01.online/epgshare01/epg_ripper_CA2.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_US2.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_US_LOCALS1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_US_SPORTS1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_UK1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_IE1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_AU1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_PT1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_PLEX1.xml.gz",
]
CHANNELS_FILE = "channels.txt"
PLACEHOLDERS_FILE = "placeholders.txt"   # simple fallback listings for channels with no schedule anywhere
PLACEHOLDER_DAYS = 4
OUTPUT = "epg.xml.gz"
PLAIN_OUTPUT = "epg.xml"     # same guide, unzipped, for apps that prefer it
DAYS_BACK_KEPT = 1          # drop listings that ended more than this many days ago


def wanted_ids():
    with open(CHANNELS_FILE, encoding="utf-8") as fh:
        return {l.strip() for l in fh if l.strip() and not l.startswith("#")}


def open_source(src, tmpdir):
    """Download a source to a temp file and return a readable (un-gzipped) stream."""
    if src.startswith("env:"):
        label = "provider guide" if src == "env:PROVIDER_EPG_URL" else "provider 2 guide"
        src = os.environ.get(src[4:], "")
        if not src:
            return None, f"{label}: skipped (secret not set)"
    else:
        label = src.rsplit("/", 1)[-1]
    path = os.path.join(tmpdir, "src.bin")
    req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0 (epg-merge)"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as r, open(path, "wb") as out:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
            break
        except Exception as e:  # network hiccup: retry, then give up on this source only
            if attempt == 2:
                return None, f"{label}: download failed ({e.__class__.__name__})"
            time.sleep(10)
    with open(path, "rb") as fh:
        magic = fh.read(2)
    fh = gzip.open(path, "rb") if magic == b"\x1f\x8b" else open(path, "rb")
    return fh, label


def placeholders():
    out = {}
    if os.path.exists(PLACEHOLDERS_FILE):
        with open(PLACEHOLDERS_FILE, encoding="utf-8") as fh:
            for line in fh:
                if line.strip() and not line.startswith("#"):
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) >= 2:
                        out[parts[0]] = (parts[1], parts[2] if len(parts) > 2 else "")
    return out


def placeholder_listings(cid, title, desc):
    """Hourly blocks from yesterday through PLACEHOLDER_DAYS ahead."""
    start = (int(time.time()) // 3600) * 3600 - 86400
    rows = []
    for h in range((PLACEHOLDER_DAYS + 1) * 24):
        a = time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(start + h * 3600))
        b = time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(start + (h + 1) * 3600))
        rows.append(f'<programme start="{a}" stop="{b}" channel={quoteattr(cid)}>'
                    f'<title>{escape(title)}</title><desc>{escape(desc)}</desc></programme>')
    return "\n".join(rows) + "\n"


# ---------------------------------------------------------------- event slots
# PPV and event channels have no schedule anywhere, but the provider writes
# today's event into the channel NAME in its playlist. We read those names and
# turn them into guide listings. event_slots.txt maps guide ID -> stream number.
EVENT_SLOTS_FILE = "event_slots.txt"
EVENT_HOURS = 3             # assumed length when the provider gives a start but no end
UNTIMED_MAX_HOURS = 36      # a name with no date is trusted this long; providers leave old names up
STATE_FILE = "slot_state.json"

GENERIC_WORDS = set("""hd fhd uhd sd d raw event events only fite tv netflix ppv nfl ufc us uk ca ar prime wwe
sky sports box office tnt live dazn now streaming exclusive 8k 4k 1080 720 next no""".split())


def provider_names(n=1):
    """stream number -> channel name, from provider n's playlist (login stays in the secret)."""
    sfx = "" if n == 1 else str(n)
    url = os.environ.get(f"PROVIDER{sfx}_M3U_URL", "")
    if not url:
        g = os.environ.get(f"PROVIDER{sfx}_EPG_URL", "")
        if "xmltv.php" not in g:
            return {}
        url = g.replace("xmltv.php", "get.php") + "&type=m3u_plus&output=ts"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (epg-merge)"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            text = r.read().decode("utf-8", "replace")
    except Exception:
        return {}
    names, last = {}, None
    for line in text.splitlines():
        if line.startswith("#EXTINF"):
            last = line.split('",', 1)[-1] if '",' in line else line.rsplit(",", 1)[-1]
        elif last is not None and line.strip() and not line.startswith("#"):
            names[line.strip().rstrip("/").rsplit("/", 1)[-1].split(".")[0]] = last.strip()
            last = None
    return names


def _utc(y, mo, d, h, mi, tz=None):
    import calendar
    if tz is None:
        return calendar.timegm((y, mo, d, h, mi, 0))
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return int(datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(tz)).timestamp())


def parse_event(raw):
    """Return (title, start, stop) from a provider slot name, or None when the slot is empty."""
    import re
    s = re.sub(r"[^\x00-\x7FÀ-ſ]+", " ", raw)            # drop superscript tags
    if re.search(r"no event", s, re.I):
        return None
    m = "\u2605" in raw and re.match(r"^\s*(?:US|CA|UK|TRILLERTV)?\s*(?:UFC|BOXING|EVENT|DAZN|SN\+|TSN\+|PPV|NFL)\s*0*\d+\s*(?:HD)?\s*:\s*(.*)$", s, re.I)
    if m:
        s = m.group(1)
        if not s.strip(" :-"):
            return None
    start = stop = None
    m = re.search(r"start:(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):\d\d(?:\s*stop:(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):\d\d)?", s)
    if m:                                                            # UFC slots, UTC
        g = [int(x) if x else None for x in m.groups()]
        start = _utc(*g[:5])
        if g[5]:
            stop = _utc(*g[5:10])
        s = s[:m.start()]
    m = re.search(r"\|\s*(\d{4})-(\d\d)-(\d\d)\s*\|\s*(\d\d):(\d\d)\s*\(GMT\)", s)
    if m:                                                            # DAZN slots
        start = _utc(*map(int, m.groups()))
        s = s[:m.start()]
    m = re.search(r"\((\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):\d\d\)", s)
    if m:                                                            # SN+ / TSN+ slots, Eastern time
        start = _utc(*map(int, m.groups()), tz="America/New_York")
        s = s[:m.start()]
    m = re.search(r"-\s*\w{3},\s*(\d+)\s+(\w{3})\s+(\d\d):(\d\d)\s*ET", s)
    if m:                                                            # TrillerTV slots
        mon = "jan feb mar apr may jun jul aug sep oct nov dec".split().index(m.group(2).lower()[:3]) + 1
        yr = time.gmtime().tm_year
        start = _utc(yr, mon, int(m.group(1)), int(m.group(3)), int(m.group(4)), tz="America/New_York")
        if start > time.time() + 180 * 86400:
            start = _utc(yr - 1, mon, int(m.group(1)), int(m.group(3)), int(m.group(4)), tz="America/New_York")
        s = s[:m.start()]
    # strip slot labels and provider tags
    for pat in [r"^\s*next\s*\|", r"\|\s*8K EXCLUSIVE.*$", r"\|\s*(US|AR):\s*DAZN PPV \d+.*$", r"^\s*(US|UK|CA|TV|PRIME|NFL|UFC)\s*\|\s*",
                r"^\s*(CA|US) \([^)]*\)\s*\|\s*", r"^\s*UFC \d+\s*:\s*", r"^\s*Boxing\s+\d+\s*[|:]\s*",
                r"^\s*Pay Per View \d+\s*", r"^\s*LIVE EVENT \d+\s*-\s*", r"^\s*\d+ HD \(D\):\s*(TrillerTV-)?",
                r"\(Backup\)", r"\s+-\s*$"]:
        s = re.sub(pat, " ", s, flags=re.I).strip()
    parts = [p.strip() for p in s.split("|") if p.strip()]
    if len(parts) >= 2 and start:                                    # DAZN: title | league
        s = f"{parts[0]} ({parts[1]})"
    else:
        s = " | ".join(parts)
    s = re.sub(r"\s{2,}", " ", s).strip(" -:|")
    words = [w for w in re.findall(r"[A-Za-z]+", s.lower()) if w not in GENERIC_WORDS]
    if len(" ".join(words)) < 4:
        return None
    now = time.time()
    if start and (stop or start + EVENT_HOURS * 3600) < now - 2 * 3600:
        return None                                                  # old event still in the name
    return s, start, stop


def event_listings(cid, event, fallback):
    """Fallback blocks all day, with the event in its time slot (or all day if no time given)."""
    title, start, stop = event
    if not start:
        return placeholder_listings(cid, title, "Today's event on this channel, per the provider. Times are not exact.")
    stop = stop or start + EVENT_HOURS * 3600
    t0 = (int(time.time()) // 3600) * 3600 - 86400
    t1 = t0 + (PLACEHOLDER_DAYS + 1) * 86400
    blocks, t = [], t0
    while t < t1:
        nxt = t + 3600
        if nxt <= start or t >= stop:
            blocks.append((t, nxt, fallback[0], fallback[1]))
        else:
            if t < start:
                blocks.append((t, start, fallback[0], fallback[1]))
            if t <= start < nxt:
                blocks.append((start, stop, title, "Live event. Time from the provider listing."))
            if t < stop < nxt:
                blocks.append((stop, nxt, fallback[0], fallback[1]))
        t = nxt
    if start < t0 or start >= t1:
        blocks.append((start, stop, title, "Live event. Time from the provider listing."))
    rows = []
    for a, b, ti, de in sorted(blocks):
        rows.append(f'<programme start="{time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(a))}" '
                    f'stop="{time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(b))}" channel={quoteattr(cid)}>'
                    f'<title>{escape(ti)}</title><desc>{escape(de)}</desc></programme>')
    return "\n".join(rows) + "\n"


def load_state():
    """Last run's slot names, so a name with no date can be aged out."""
    import json
    repo = os.environ.get("GITHUB_REPOSITORY")
    try:
        if repo:
            url = f"https://raw.githubusercontent.com/{repo}/guide/{STATE_FILE}"
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, encoding="utf-8") as fh:
                return json.load(fh)
    except Exception:
        pass
    return {}


def event_slots():
    out = {}
    if os.path.exists(EVENT_SLOTS_FILE):
        with open(EVENT_SLOTS_FILE, encoding="utf-8") as fh:
            for line in fh:
                if line.strip() and not line.startswith("#"):
                    parts = line.rstrip("\n").split("\t")
                    out[parts[0].strip()] = (parts[2].strip() if len(parts) > 2 else "1", parts[1].strip())
    return out


def cutoff_stamp():
    t = time.gmtime(time.time() - DAYS_BACK_KEPT * 86400)
    return time.strftime("%Y%m%d%H%M%S", t)


def main():
    ids = wanted_ids()
    filled = set()
    channels = {}             # id -> <channel> xml string (first source wins)
    cutoff = cutoff_stamp()
    tmpdir = os.environ.get("RUNNER_TEMP", "/tmp")
    body = io.StringIO()
    report = []

    for src in SOURCES:
        fh, label = open_source(src, tmpdir)
        if fh is None:
            report.append(label)
            continue
        got = set()
        n_prog = 0
        try:
            for _, el in ET.iterparse(fh, events=("end",)):
                if el.tag == "channel":
                    cid = el.get("id")
                    if cid in ids and cid not in channels:
                        channels[cid] = ET.tostring(el, encoding="unicode")
                    el.clear()
                elif el.tag == "programme":
                    cid = el.get("channel")
                    if cid in ids and cid not in filled and el.get("stop", "")[:14] >= cutoff:
                        body.write(ET.tostring(el, encoding="unicode"))
                        got.add(cid)
                        n_prog += 1
                    el.clear()
        except ET.ParseError as e:
            report.append(f"{label}: parse error ({e}); kept what was read")
        finally:
            fh.close()
        filled |= got
        report.append(f"{label}: {len(got)} channels, {n_prog} listings")

    ph = placeholders()
    slots = event_slots()
    names = {}
    for tag in sorted({t for t, _ in slots.values()}):
        names[tag] = provider_names(int(tag))
        report.append(f"event names: provider {tag}: read {len(names[tag])} channel names")
    old_state, new_state = (load_state() if slots else {}), {}
    ph_used = ev_used = 0
    for cid in sorted(ids - filled):
        if cid in ph:
            tag, num = slots.get(cid, ("1", None))
            event = parse_event(names.get(tag, {})[num]) if num in names.get(tag, {}) else None
            if event and not event[1]:
                prev = old_state.get(cid)
                first = prev[1] if prev and prev[0] == event[0] else int(time.time())
                new_state[cid] = [event[0], first]
                if time.time() - first > UNTIMED_MAX_HOURS * 3600:
                    event = None                     # same undated name for too long: stale
            if event:
                body.write(event_listings(cid, event, ph[cid]))
                ev_used += 1
            else:
                body.write(placeholder_listings(cid, *ph[cid]))
            channels.setdefault(cid, f"<channel id={quoteattr(cid)}><display-name>{escape(ph[cid][0])}</display-name></channel>")
            filled.add(cid)
            ph_used += 1
    report.append(f"placeholders: {ph_used} channels, {ev_used} of them showing a live event")
    if slots:
        import json
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(new_state, fh, ensure_ascii=False, indent=0)

    missing = sorted(ids - filled)
    with gzip.open(OUTPUT, "wt", encoding="utf-8") as out:
        out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        out.write('<tv generator-info-name="jordan-tv merged guide">\n')
        for cid in sorted(ids):
            if cid in channels:
                out.write(channels[cid])
            else:  # channel with no data anywhere: still declare it so apps match it
                out.write(f"<channel id={quoteattr(cid)}><display-name>{escape(cid)}</display-name></channel>")
            out.write("\n")
        out.write(body.getvalue())
        out.write("</tv>\n")

    with gzip.open(OUTPUT, "rb") as src, open(PLAIN_OUTPUT, "wb") as dst:
        dst.write(src.read())

    print("\n".join(report))
    print(f"\nChannels with listings: {len(filled)} of {len(ids)}")
    if missing:
        print("No listings anywhere for: " + ", ".join(missing))
    with open("status.txt", "w", encoding="utf-8") as fh:
        fh.write(time.strftime("Built %Y-%m-%d %H:%M UTC\n", time.gmtime()))
        fh.write("\n".join(report) + "\n")
        fh.write(f"Channels with listings: {len(filled)} of {len(ids)}\n")
        if missing:
            fh.write("No listings: " + ", ".join(missing) + "\n")
    if not filled:
        sys.exit("Nothing merged; keeping the previous guide.")


if __name__ == "__main__":
    main()
