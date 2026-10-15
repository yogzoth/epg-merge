# epg-merge

Builds one guide for the jordan-tv playlist every night at about 3:15 am
Pacific, from your provider's guide plus the epgshare01 guides.

Your guide links (replace YOUR-USERNAME):

- Zipped: `https://raw.githubusercontent.com/YOUR-USERNAME/epg-merge/guide/epg.xml.gz`
- Plain: `https://raw.githubusercontent.com/YOUR-USERNAME/epg-merge/guide/epg.xml`

`status.txt` on the guide branch shows the last run and which channels had no
listings anywhere.

When your provider login changes, update the secret `PROVIDER_EPG_URL` under
Settings > Secrets and variables > Actions.

When the lineup changes, upload the new `channels.txt` here.
