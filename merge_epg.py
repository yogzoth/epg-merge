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
import re
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
EXTRA_SOURCES_FILE = "sources_extra.txt"   # more guide files, one link per line (world channels)
EPGSHARE = "https://epgshare01.online/epgshare01/epg_ripper_{}.xml.gz"
# On manual runs, also list every channel in every country guide file, so new
# channels can be matched to a guide (published as index.txt on the guide branch).
DISCOVER = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch" or os.environ.get("DISCOVER") == "1"
DISCOVER_COUNTRIES = """AL AR AT AU BA BE BG CH CL CN CO CR CY CZ DE DK DO EC EG ES FI FR GR HK HR HU ID IL IN IS IT
JM JO JP KE KR LB LT LU MA MT MX MY NG NL NO NZ PA PE PH PK PL RO RS RU SA SE SG SI SK SV TH TR TW UA UY VN ZA AE
IR VE QA KW GH UG ET UK US IE CA""".split()
CHANNELS_FILE = "channels.txt"
PLACEHOLDERS_FILE = "placeholders.txt"   # simple fallback listings for channels with no schedule anywhere
PLACEHOLDER_DAYS = 4
OUTPUT = "epg.xml.gz"
PLAIN_OUTPUT = "epg.xml"     # same guide, unzipped, for apps that prefer it
DAYS_BACK_KEPT = 1          # drop listings that ended more than this many days ago


def wanted_ids():
    with open(CHANNELS_FILE, encoding="utf-8") as fh:
        return {l.strip() for l in fh if l.strip() and not l.startswith("#")}


def open_source(src, tmpdir, tries=3):
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
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=300) as r, open(path, "wb") as out:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
            break
        except Exception as e:  # network hiccup: retry, then give up on this source only
            if attempt == tries - 1:
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



def catchup_streams(n=2):
    """stream number -> days of catch-up, from provider n's channel list (login stays in the secret)."""
    sfx = "" if n == 1 else str(n)
    g = os.environ.get(f"PROVIDER{sfx}_EPG_URL", "")
    if "xmltv.php" not in g:
        return None
    url = g.replace("xmltv.php", "player_api.php") + "&action=get_live_streams"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (epg-merge)"})
    try:
        import json
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    out = {}
    for c in data if isinstance(data, list) else []:
        try:
            if int(c.get("tv_archive") or 0) == 1:
                out[str(c.get("stream_id"))] = int(c.get("tv_archive_duration") or 0)
        except (TypeError, ValueError):
            pass
    return out

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
    m = "\u2605" in raw and re.match(r"^\s*(?:US|CA|UK|TRILLERTV)?\s*(?:UFC|BOXING|EVENT|DAZN|SN\+|TSN\+|PPV|NFL|NHL GAME|NBA|MLB|PARAMOUNT)\s*0*\d+\s*(?:HD)?\s*:\s*(.*)$", s, re.I)
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
    m = re.search(r"\s+(\d{1,2}):(\d\d)\s*(AM|PM)\s*ET\s*$", s, re.I)
    if m and not start:                                              # league game slots: "8:00 PM ET", today
        from datetime import datetime
        from zoneinfo import ZoneInfo
        h = int(m.group(1)) % 12 + (12 if m.group(3).upper() == "PM" else 0)
        today = datetime.now(ZoneInfo("America/New_York"))
        start = _utc(today.year, today.month, today.day, h, int(m.group(2)), tz="America/New_York")
        if start - time.time() > 14 * 3600:                         # last night's game still in the name
            start -= 86400
        s = s[:m.start()]
        if s.isupper():                                              # "CANADIENS AT CAPITALS" -> "Canadiens at Capitals"
            s = " ".join(w if re.fullmatch(r"(UFC|PBR|NHL|NBA|MLB|NFL|WWE|II|III|\d+:?)", w) else
                         w.lower() if w.lower() in ("at", "vs", "vs.", "and", "of", "the", "@") and i else w.capitalize()
                         for i, w in enumerate(s.split()))
        s = s.replace("_IN", "-In").replace("_in", "-In").replace(" _ ", " / ").replace("@", "at")
    # strip slot labels and provider tags
    for pat in [r"^\s*next\s*\|", r"\|\s*8K EXCLUSIVE.*$", r"\|\s*(US|AR):\s*DAZN PPV \d+.*$", r"^\s*(US|UK|CA|TV|PRIME|NFL|UFC)\s*\|\s*",
                r"^\s*(CA|US) \([^)]*\)\s*\|\s*", r"^\s*UFC \d{1,2}\s*:\s*", r"^\s*Boxing\s+\d+\s*[|:]\s*",
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


# ---------------------------------------------------------------- clock fix
# Some providers write their guide in their own local time but label it UTC
# (+0000), which pushes every show 1 to 2 hours late. Each run compares the
# provider's CNN schedule with epgshare's and shifts the provider's listings by
# the difference. If CNN isn't there, provider 2 falls back to Central European
# time, which is what it used when this was set up.
ANCHORS = [("CNN.us", "CNN.HD.us2")]


def _epoch(stamp):
    import calendar, re as _re
    m = _re.match(r"(\d{14})\s*([+-])(\d\d)(\d\d)", stamp or "")
    if not m:
        return None
    t = calendar.timegm(time.strptime(m.group(1), "%Y%m%d%H%M%S"))
    off = (int(m.group(3)) * 60 + int(m.group(4))) * 60
    return t - off if m.group(2) == "+" else t + off


def _title_key(t):
    import re as _re
    return _re.sub(r"[^a-z0-9]", "", _re.sub(r"[^\x00-\x7F]+", "", (t or "").lower()))[:40]


def find_shift(prov, ref):
    """Try shifts from -5 to +5 hours and keep the one that lines up the most shows exactly.
    Lining up whole schedules (not single shows) is safe against reruns."""
    refset = {(t, s) for t, starts in ref.items() for s in starts if s}
    pairs = [(t, s) for t, starts in prov.items() for s in starts if s]
    if not refset or not pairs:
        return None
    score = {sh: sum((t, s - sh) in refset for t, s in pairs) for sh in range(-5 * 3600, 5 * 3600 + 1, 1800)}
    best = max(score, key=score.get)
    if score[best] < 5 or score[best] < 2 * max(1, score.get(0, 0) if best else 0):
        return 0 if score.get(0, 0) >= 5 else None
    return best


def europe_offset():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    return int(datetime.now(ZoneInfo("Europe/Berlin")).utcoffset().total_seconds())



def drop_late_copies(items):
    """Split each channel's listings into copies wherever the start time jumps
    backwards. With more than one copy, keep the one every other copy is a whole
    number of hours late against (the earliest), and drop the rest."""
    import re as _re
    order, per = [], {}
    for x in items:
        cid = _re.search(r'channel="([^"]+)"', x).group(1)
        if cid not in per:
            per[cid] = []
            order.append(cid)
        per[cid].append(x)
    out, n = [], 0
    for cid in order:
        rows = per[cid]
        blocks, last = [[]], None
        for x in rows:
            s = _epoch(_re.search(r'start="([^"]+)"', x).group(1)) or 0
            m = _re.search(r"<title[^>]*>([^<]*)", x)
            if last is not None and s < last:
                blocks.append([])
            blocks[-1].append((s, _title_key(m.group(1) if m else ""), x))
            last = s
        if len(blocks) == 1:
            out += rows
            continue
        sets = [{(t, s) for s, t, _ in b} for b in blocks]
        def late(i, j):        # seconds block i runs late against block j (0 if they don't line up)
            hits = {d: len({(t, s - d) for t, s in sets[i]} & sets[j]) for d in range(-5 * 3600, 5 * 3600 + 1, 1800)}
            best = max(hits, key=lambda d: (hits[d], -abs(d)))
            return best if hits[best] >= 3 else 0
        # earliest copy: the others are late against it; ties go to the biggest copy
        score = [(sum(late(j, i) > 0 for j in range(len(blocks)) if j != i), len(blocks[i]), -i) for i in range(len(blocks))]
        keep = max(range(len(blocks)), key=lambda i: score[i])
        kept = [r for i, b in enumerate(blocks) if i == keep or late(i, keep) <= 0 for r in b]
        out += [x for _, _, x in sorted(kept, key=lambda r: r[0])]
        n += len(kept) < len(rows)
    return out, n


# ---------------------------------------------------------------- clean titles
# The browsing guide shows only the title, so keep it to the show's name and move
# episode names, "Live"/"New" tags and season notes into the show info instead.
_TAGS = re.compile(r"\s*([\u1d2c-\u1d6a\u1d9b-\u1dbf\u02b0-\u02ff\u2070-\u209f]{2,})\s*$")
_MATCH = re.compile(r"\s(x|vs\.?|v)\s", re.I)


def clean_programme(el):
    t = el.find("title")
    if t is None or not t.text:
        return
    title, notes, sub = t.text.strip(), [], el.find("sub-title")
    subtext = (sub.text or "").strip() if sub is not None else ""
    m = _TAGS.search(title)                                   # "Show  ᴺᵉʷ" / "Show  ᴸᶦᵛᵉ"
    if m:
        tag = m.group(1)
        notes.append("Live" if "\u1d38" in tag else "New")
        title = title[:m.start()].rstrip()
    m = re.match(r"(?i)live:\s+(.+)", title)                  # "Live: NHL Hockey"
    if m:
        notes.append("Live"); title = m.group(1)
    m = re.search(r"\s*\((Direto|Season \d+)\)\s*$", title)  # "(Season 19)", "(Direto)"
    if m and m.start() > 0:
        notes.append("Live" if m.group(1) == "Direto" else m.group(1)); title = title[:m.start()]
    m = re.search(r"\s+(S\d{1,2}\s?E\d{1,3})$", title)        # "Show S02E05"
    if m and m.start() > 0:
        notes.append(m.group(1)); title = title[:m.start()]
    episode = ""
    if subtext and len(title) > len(subtext) + 3:              # "Show: Episode" when Episode is the sub-title
        for sep in (": ", " - ", " : "):
            if title.lower().endswith((sep + subtext).lower()):
                title = title[: -len(sep + subtext)]
                break
    for sep in (" : ", " - "):                                 # "Show : Episode", "Show - Episode"
        if sep in title:
            a, b = title.split(sep, 1)
            if len(a) >= 3 and b and not _MATCH.search(b) and not re.match(r"(?i)part\b", b):
                title, episode = a.strip(), b.strip()
            break
    t.text = title.strip()
    # Movies get their year in the title, so remakes and same-named films are easy to tell apart.
    cats = [(c.text or "").strip().lower() for c in el.findall("category")]
    year = (el.findtext("date") or "").strip()[:4]
    if any(c in ("movie", "movies", "film", "feature film") for c in cats) and re.fullmatch(r"(19|20)\d\d", year) \
            and not re.search(r"\(\d{4}\)$", t.text):
        t.text = f"{t.text} ({year})"
    # Some apps glue the sub-title and episode number onto the title in the grid
    # ("Gold Rush: The Weasel. S16 E6"), so fold them into the description instead.
    parts = []
    for p in ([episode] if episode else []) + ([subtext] if subtext else []):
        if p and p.lower() != t.text.lower() and p.lower() not in [q.lower() for q in parts]:
            parts.append(p)
    for en in el.findall("episode-num"):
        num = (en.text or "").strip()
        if en.get("system") == "xmltv_ns" and num:
            bits = [x.split("/")[0].strip() for x in num.split(".")]
            num = " ".join(f"{k}{int(v) + 1}" for k, v in zip("SE", bits[:2]) if v.isdigit())
        if num and not any(num in q for q in parts):
            parts.append(num)
        el.remove(en)
    if sub is not None:
        el.remove(sub)
    notes = list(dict.fromkeys(notes))
    if "New" in notes and el.find("new") is None:
        ET.SubElement(el, "new")
    d = el.find("desc")
    body = (d.text or "").strip() if d is not None else ""
    head = [p for p in notes + parts if p.lower() not in body.lower()[:200]]
    if head:
        if d is None:
            d = ET.Element("desc"); el.insert(list(el).index(t) + 1, d)
        d.text = (" · ".join(head) + ("\n" + body if body else "")).strip()


_PH_IDS = None


def ph_ids_cache():
    global _PH_IDS
    if _PH_IDS is None:
        _PH_IDS = set(placeholders())
    return _PH_IDS



def write_index(index, sources, tmpdir):
    """index.txt: every channel in every guide source (and every country file that exists)."""
    have = {s.rsplit("/", 1)[-1] for s in sources}
    for cc in DISCOVER_COUNTRIES:
        for n in (1, 2, 3):
            url = EPGSHARE.format(f"{cc}{n}")
            if url.rsplit("/", 1)[-1] in have:
                continue
            fh, label = open_source(url, tmpdir, tries=1)
            if fh is None:
                break                                  # no file with this number: stop for this country
            try:
                for _, el in ET.iterparse(fh, events=("end",)):
                    if el.tag == "channel":
                        index.setdefault(label, {}).setdefault(el.get("id"), ["", 0])[0] = el.findtext("display-name") or ""
                    elif el.tag == "programme":
                        index.setdefault(label, {}).setdefault(el.get("channel"), ["", 0])[1] += 1
                    el.clear()
            except ET.ParseError:
                pass
            finally:
                fh.close()
    with open("index.txt", "w", encoding="utf-8") as out:
        out.write(time.strftime("# Guide channel index, %Y-%m-%d %H:%M UTC. source<TAB>id<TAB>name<TAB>listings\n", time.gmtime()))
        for label, chans in index.items():
            for cid, (name, n) in chans.items():
                out.write(f"{label}\t{cid}\t{name}\t{n}\n")

def cutoff_stamp():
    t = time.gmtime(time.time() - DAYS_BACK_KEPT * 86400)
    return time.strftime("%Y%m%d%H%M%S", t)


def main():
    ids = wanted_ids()
    # "west.<id>" = a West feed: the East channel's guide, 3 hours later.
    west = {i: i[5:] for i in ids if i.startswith("west.")}
    ids = (ids - set(west)) | set(west.values())
    filled = set()
    channels = {}             # id -> <channel> xml string (first source wins)
    cutoff = cutoff_stamp()
    tmpdir = os.environ.get("RUNNER_TEMP", "/tmp")
    body = io.StringIO()
    report = []

    held = {}                                   # provider source -> list of programme elements
    anchor_p = {}                               # provider source -> {title: [start epochs]}
    anchor_r = {}                               # {title: [start epochs]} from epgshare
    p_anchor_ids = {a for a, _ in ANCHORS}
    r_anchor_ids = {b for _, b in ANCHORS}
    sources = list(SOURCES)
    if os.path.exists(EXTRA_SOURCES_FILE):
        for l in open(EXTRA_SOURCES_FILE, encoding="utf-8"):
            if l.strip() and not l.startswith("#") and l.strip() not in sources:
                sources.append(l.strip())
    index = {}                                  # label -> {channel id: [name, listings]} (discovery)
    for src in sources:
        fh, label = open_source(src, tmpdir)
        if fh is None:
            report.append(label)
            continue
        got = set()
        n_prog = 0
        is_prov = src.startswith("env:")
        try:
            for _, el in ET.iterparse(fh, events=("end",)):
                if el.tag == "channel":
                    cid = el.get("id")
                    if DISCOVER:
                        index.setdefault(label, {}).setdefault(cid, [el.findtext("display-name") or "", 0])[0] = el.findtext("display-name") or ""
                    if cid in ids and cid not in channels:
                        channels[cid] = ET.tostring(el, encoding="unicode")
                    el.clear()
                elif el.tag == "programme":
                    cid = el.get("channel")
                    if DISCOVER:
                        index.setdefault(label, {}).setdefault(cid, ["", 0])[1] += 1
                    if cid in ids or cid in p_anchor_ids or cid in r_anchor_ids:
                        clean_programme(el)
                    if cid in p_anchor_ids and is_prov:
                        anchor_p.setdefault(src, {}).setdefault(_title_key(el.findtext("title")), []).append(_epoch(el.get("start")))
                    if cid in r_anchor_ids and not is_prov:
                        anchor_r.setdefault(_title_key(el.findtext("title")), []).append(_epoch(el.get("start")))
                    if cid in ids and cid not in filled and el.get("stop", "")[:14] >= cutoff:
                        if is_prov:
                            held.setdefault(src, []).append(ET.tostring(el, encoding="unicode"))
                        else:
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

    # Provider 2 repeats each channel's schedule two or three times in one file,
    # with the extra copies 2 to 4 hours late. Keep only the copy the others are
    # late against, then measure what's left against CNN as a safety net.
    import re as _re
    for src, items in held.items():
        items, n_dupe = drop_late_copies(items)
        if n_dupe:
            report.append(f"{'provider 2' if '2' in src else 'provider'} guide: dropped late duplicate schedules on {n_dupe} channels")
        prov_cnn = {}
        for x in items:
            if f'channel="{ANCHORS[0][0]}"' in x:
                m = _re.search(r"<title[^>]*>([^<]*)", x)
                prov_cnn.setdefault(_title_key(m.group(1) if m else ""), []).append(_epoch(_re.search(r'start="([^"]+)"', x).group(1)))
        shift = find_shift(prov_cnn, anchor_r) or 0
        report.append(f"{'provider 2' if '2' in src else 'provider'} guide clock shift: {shift // 60:+d} min (measured against CNN)")
        def fix(m):
            t = _epoch(m.group(2))
            return f'{m.group(1)}="{time.strftime("%Y%m%d%H%M%S", time.gmtime(t - shift))} +0000"' if t is not None else m.group(0)
        for x in items:
            body.write(_re.sub(r'(start|stop)="([^"]+)"', fix, x) if shift else x)

    if west:
        import re as _re
        def later(m):
            t = _epoch(m.group(2))
            return f'{m.group(1)}="{time.strftime("%Y%m%d%H%M%S", time.gmtime(t + 3 * 3600))} +0000"' if t is not None else m.group(0)
        src_ids = set(west.values())
        copies = []
        for x in _re.findall(r"<programme\b.*?</programme>", body.getvalue(), _re.S):
            cid = _re.search(r'channel="([^"]+)"', x).group(1)
            if cid in src_ids:
                for w_id, e_id in west.items():
                    if e_id == cid:
                        y = x.replace(f'channel="{cid}"', f'channel={quoteattr(w_id)}', 1)
                        copies.append(_re.sub(r'(start|stop)="([^"]+)"', later, y))
        body.write("".join(copies))
        made = 0
        for w_id, e_id in west.items():
            if e_id in filled:
                filled.add(w_id); made += 1
                name = _re.search(r"<display-name[^>]*>([^<]*)", channels.get(e_id, "")) if e_id in channels else None
                channels[w_id] = f"<channel id={quoteattr(w_id)}><display-name>{name.group(1) if name else escape(e_id)} (West)</display-name></channel>"
        ids |= set(west)
        report.append(f"west feeds: {made} of {len(west)} made from their East guide, 3 hours later")

    # Fill holes from the guide this job published last time: the first hours of a
    # West feed (its East guide starts 3 hours too late), or a source that failed today.
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    prev_url = os.environ.get("PREVIOUS_GUIDE_URL") or (f"https://raw.githubusercontent.com/{repo}/guide/epg.xml.gz" if repo else "")
    if prev_url:
        first = {}
        for m in re.finditer(r"<programme\b([^>]*)>", body.getvalue()):
            a = m.group(1)
            c, st = re.search(r'channel="([^"]+)"', a), re.search(r'start="([^"]+)"', a)
            if c and st:
                t0 = _epoch(st.group(1))
                if t0 is not None and t0 < first.get(c.group(1), 1e18):
                    first[c.group(1)] = t0
        fh, label = open_source(prev_url, tmpdir)
        kept, revived = 0, set()
        now = time.time()
        if fh is not None:
            try:
                for _, el in ET.iterparse(fh, events=("end",)):
                    if el.tag == "channel":
                        cid = el.get("id")
                        if cid in ids and cid not in channels and not cid.startswith("ph."):
                            channels[cid] = ET.tostring(el, encoding="unicode")
                        el.clear()
                    elif el.tag == "programme":
                        cid = el.get("channel")
                        if cid in ids and not cid.startswith("ph.") and cid not in ph_ids_cache():
                            s0, e0 = _epoch(el.get("start")), _epoch(el.get("stop"))
                            lim = first.get(cid)
                            if s0 is not None and e0 is not None and e0 > now - 6 * 3600 and (lim is None or s0 < lim):
                                if lim is not None and e0 > lim:
                                    el.set("stop", time.strftime("%Y%m%d%H%M%S", time.gmtime(lim)) + " +0000")
                                body.write(ET.tostring(el, encoding="unicode"))
                                kept += 1
                                if lim is None:
                                    revived.add(cid)
                        el.clear()
            except ET.ParseError:
                pass
            finally:
                fh.close()
            filled |= revived
            report.append(f"last guide: {kept} listings carried over to fill gaps ({len(revived)} channels kept from last time)")
        else:
            report.append(f"last guide: not read ({label})")

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
        # Which provider 2 streams have catch-up (stream number:days). No login in here.
        cu = catchup_streams(2)
        if cu is not None:
            fh.write(f"Catch-up streams ({len(cu)}): " + ",".join(f"{k}:{v}" for k, v in sorted(cu.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0)) + "\n")
        else:
            fh.write("Catch-up streams: could not read the provider's channel list\n")
    if DISCOVER:
        write_index(index, sources, tmpdir)
    if not filled:
        sys.exit("Nothing merged; keeping the previous guide.")


if __name__ == "__main__":
    main()
