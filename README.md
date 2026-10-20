# epg-merge

One clean TV guide for the **tv-general-trex** playlist (Trex line) and the old **tv-general** playlist while it's still in use, rebuilt every hour by GitHub Actions.

## Guide links

| Format | Link |
| --- | --- |
| Zipped (smaller, faster) | `https://raw.githubusercontent.com/yogzoth/epg-merge/guide/epg.xml.gz` |
| Plain | `https://raw.githubusercontent.com/yogzoth/epg-merge/guide/epg.xml` |

The playlists already point at the guide, so most apps pick it up on their own.

## What each run does

1. Downloads both lines' guides and the epgshare01 country guides, keeping only the channels in `channels.txt`.
2. Repairs the provider guide: removes its late duplicate schedules and checks its clock against CNN.
3. Fills holes in patchy guides from a backup guide for the same channel (`fill.txt`) and removes overlapping listings.
4. Makes West feeds from their East guide, 3 hours later.
5. Covers any remaining gap with the previous run's guide, then adds placeholder and live-event listings for event slots.
6. Cleans titles for the grid: episode details go to the description, movies get their year.

`status.txt` on the `guide` branch shows when the guide was last built, what each source gave, and any channel with no listings.

## Files

| File | What it is |
| --- | --- |
| `merge_epg.py` | The guide builder. |
| `channels.txt` | Every guide ID the two playlists use. |
| `fill.txt` | Backup guides for channels whose main guide has holes (often the Trex line's own guide). |
| `placeholders.txt` | Simple listings for channels with no schedule anywhere. |
| `event_slots.txt` | Which Trex stream each live sports and PPV slot is, so the guide can show the event name. |
| `sources_extra.txt` | Extra country guide files used by the World channels. |
| `art/` | Logos for the Chill channels, event slots and a few channels the logo library is missing. |

## Upkeep

- **Login changes:** `PROVIDER_EPG_URL` is the Trex line's xmltv.php link; `PROVIDER2_EPG_URL` is the old tv-general provider (delete it when that ends) under Settings > Secrets and variables > Actions. Logins are never stored in the repo.
- **Lineup changes:** upload the new `channels.txt`, `placeholders.txt` and `event_slots.txt` together.
- **Manual run:** Actions > Build merged guide > Run workflow. Manual runs also refresh `index.txt`, a list of every channel in every guide source, used to match new channels.
