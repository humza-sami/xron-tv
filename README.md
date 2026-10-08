# Xron TV: channel list and website

Public data for the Xron TV Android app.

- `channels/channels.json`: the channel list the app downloads at start-up. Rebuilt every day by
  `.github/workflows/update-channels.yml`, which checks every stream and keeps only working ones.
- `channels/manual.json`: featured channels, hidden channels and rules for hiding paid networks.
- `privacy.html`: the app's privacy policy (served by GitHub Pages).

Channel data comes from the public [iptv-org](https://github.com/iptv-org/iptv) database.
Channel owners can request removal at info@xron.ai.
