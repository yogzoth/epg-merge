# epg-merge

Builds one guide for the jordan-tv playlist every 3 hours, from your provider's guide plus the epgshare01 guides.

Your guide links (replace YOUR-USERNAME):

- Zipped: `https://raw.githubusercontent.com/YOUR-USERNAME/epg-merge/guide/epg.xml.gz`
- Plain: `https://raw.githubusercontent.com/YOUR-USERNAME/epg-merge/guide/epg.xml`

`status.txt` on the guide branch shows the last run and which channels had no
listings anywhere.

When your provider login changes, update the secret `PROVIDER_EPG_URL` under
Settings > Secrets and variables > Actions.

When the lineup changes, upload the new `channels.txt` here.

PPV and event slots: `event_slots.txt` lists which provider stream each slot is.
Every run reads today's event names from your provider's playlist and puts them
in the guide. Upload a new `event_slots.txt` whenever the playlist is rebuilt.

art/ holds the CHILL icons and the section divider logos and clips the playlist points at.
