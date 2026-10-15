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
    "env:PROVIDER_EPG_URL",   # your provider's xmltv.php link (secret)
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
OUTPUT = "epg.xml.gz"
PLAIN_OUTPUT = "epg.xml"     # same guide, unzipped, for apps that prefer it
DAYS_BACK_KEPT = 1          # drop listings that ended more than this many days ago


def wanted_ids():
    with open(CHANNELS_FILE, encoding="utf-8") as fh:
        return {l.strip() for l in fh if l.strip() and not l.startswith("#")}


def open_source(src, tmpdir):
    """Download a source to a temp file and return a readable (un-gzipped) stream."""
    if src.startswith("env:"):
        src = os.environ.get(src[4:], "")
        if not src:
            return None, "skipped (secret not set)"
        label = "provider guide"
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
