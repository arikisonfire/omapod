# Security

## Reporting a vulnerability

Please report security problems privately, not in a public issue: open the repository's **Security** tab and
choose **Report a vulnerability**
([direct link](https://github.com/arikisonfire/omapod/security/advisories/new)). Include the omaPOD version
(`omapod --version`), what you did and what happened.

Only the latest release receives fixes.

## What omaPOD stores

Everything stays in your home folder and is readable by your user only: folders are created with mode 0700,
files that hold personal data or secrets with mode 0600.

| What | Where |
|---|---|
| Library: subscriptions, episodes, positions, queue, favorites, history, statistics, download log, **passwords of protected podcasts** | `~/.local/share/omapod/omapod.db` |
| Database exports, automatic backups (the 5 most recent) | where you save them, `~/.local/share/omapod/backups/` |
| Settings | `~/.config/omapod/settings.json` |
| Secrets: sync login (Nextcloud app password or gpodder.net password, a trusted certificate's fingerprint), proxy password, Podcast Index API key | `~/.config/omapod/credentials.json` |
| Downloads | `~/.local/share/omapod/episodes/` or the folder you chose |
| Cover images (at most 300 MB, least recently used go first), mpv's log, Python's bytecode | `~/.cache/omapod/` |
| The program itself, from the plugin marketplace (a git clone that `omarchy plugin update` updates) | `~/.config/omarchy/plugins/io.github.arikisonfire.pod/` |
| Player state for the bar widget, control socket, lock file | `$XDG_RUNTIME_DIR/omapod/` |

Database exports and backups contain the passwords of protected podcasts, so a restored library keeps working.
Keep them as private as the passwords themselves. omaPOD creates every export (database, OPML, HTML) readable
by your user only (0600) before it writes the first byte, and puts it in place once it is complete.

**Importing a database** replaces the library with a file that may come from anywhere. omaPOD reads it with
SQLite's precautions for untrusted files and accepts it only if it passes the integrity check and holds nothing
but tables and indexes (no triggers, views or virtual tables). Downloads it lists that are not in your download
folder (or, for a local folder, not in that folder) are forgotten, so the import cannot point omaPOD at your
other files.

## Network connections

omaPOD has no telemetry, crash reporting or update check. Requests carry the user agent
`omaPOD/<version> (+https://github.com/arikisonfire/omapod)`.

| Host | Why | What it learns | When |
|---|---|---|---|
| Podcast feed hosts | Feeds, older pages of paged feeds | Your IP address; the podcast's username and password if it is protected | Refresh (your interval, or hourly with the optional timer), preview |
| Hosts that feeds point to, including statistics prefixes such as Podtrac or OP3 | Episodes (download and streaming), cover images, chapters, transcripts | Your IP address | Download, playback, display |
| itunes.apple.com | Search, top podcasts, feed lookup | Search terms, country | Searching; the top list whenever you open **Add podcast**, unless you hide suggestions there |
| api.fyyd.de, gpodder.net, api.podcastindex.org | Search | Search terms (Podcast Index: your API key) | Only when you choose that search provider |
| Hosts that search results name | Cover images of the results (the directory's image servers or the podcasts' own) | Your IP address | Showing results and the top list |
| Your sync server (Nextcloud with GPodder Sync, or gpodder.net) | Synchronization | Login, subscriptions, episode actions (episode address, position, time), a device name made from your computer's host name (`omapod-<hostname>`); with Nextcloud, optionally your queue and favorites as files in the hidden folder `.omapod` | Every 30 minutes, when the window gets focus, before playback, a few seconds after you pause, when you quit; with Nextcloud also every 3 minutes while idle |

A proxy set in **Settings → Downloads → Proxy** is used for all of these; playback through mpv uses the
system's proxy settings instead.

## How credentials are protected

- **TLS:** every https connection checks the server's certificate and host name against your system's
  certificate authorities: feeds, downloads, images, sync (or the one certificate you confirmed for it, see
  below), and playback, where mpv runs with `--tls-verify=yes` (mpv's own default is not to check).
- **Protected podcasts:** the username and password go only to the feed's own server (same scheme, host and
  port, or its https upgrade): for the feed, its older pages, downloads and streams. No other server receives
  them, not even one that asks for them (HTTP 401); a download from such a server fails with a message that
  names it. Redirects never carry them elsewhere, and a protected podcast never moves to another host on its own.
  If the address you subscribe to redirects to another server that asks for a password, the login dialog names
  that server; the podcast is then subscribed at its address, and the password goes to that server only.
- **Sync:** TLS certificates are checked. A certificate your system does not trust (self-signed, your own
  certificate authority) is accepted only after you have seen its SHA-256 fingerprint and confirmed it, and
  from then on only exactly that certificate. For an address typed without `http://` or `https://`, omaPOD uses
  https and never falls back to plain http on its own: it offers http only for addresses in your local network
  or VPN, after asking. Every login over http, also one you typed with `http://`, shows a lasting warning in the
  sync settings.
- **Nextcloud login:** the login page, the login token and the app password stay with the server you entered,
  over the connection you chose (https, or http after asking). Behind some proxies Nextcloud names other
  addresses for them (`http://`, another port or host name); omaPOD then keeps their path but uses the server
  you entered.

## Other programs on your desktop

- **MPRIS:** while something is loaded, every program on your session bus can read the episode title, the
  podcast's name, the first 300 characters of the show notes, a cover thumbnail from omaPOD's cache and, for a
  downloaded episode, its file path. This is how media keys and Omarchy's media widget work. Stream addresses
  are not shared, as those of private feeds often carry an access token.
- **Links** in show notes and podcast pages open only if they are web (http, https) or mail links; anything
  else is copied to the clipboard instead.
- **The bar widget** runs inside the Omarchy shell and reads omaPOD's state from a socket only your user can
  open. It shows titles as plain text.
- **The installer** makes omaPOD the app for `podcast://`, `itpc://`, `pcast://` and `antennapod-subscribe://`
  links unless you picked another app for them. `./install.sh --uninstall` takes that back and removes the
  cache; your library stays.

## Your files

omaPOD deletes and moves only its own downloads: files in a podcast's folder inside the download folder
(`<download folder>/<podcast>/<file>`). If an episode names a file anywhere else, deleting the download only
makes omaPOD forget it; the file stays where it is.

Episodes of a local folder you added are your own files. omaPOD never deletes them on its own: **Delete
download** asks first and moves them to the trash, **Delete played downloads** leaves them alone, and automatic
deletion skips them unless you switch on **Auto delete from local folders** (then they go to the trash too).
Only files inside that folder go to the trash.

## Limits by design

- **Parental control** prevents accidents, not a determined user: anyone who can edit omaPOD's files can turn
  it off. The password is stored as a salted scrypt hash.
- Feeds, search results and server answers are untrusted input. omaPOD caps what it accepts (feed and download
  sizes, compressed answers, image dimensions, length of show notes, titles and transcripts) and takes only web
  addresses from them. Names and addresses appear as written, never as formatting; show notes keep simple
  formatting and links only. A podcast host can still see your IP address and when you download, like with any
  podcast app.
