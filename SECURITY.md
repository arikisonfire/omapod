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
by your user only (0600) before it writes the first byte, and puts it in place once it is complete. The export
is written through the one file omaPOD created and holds open, never through a name it opens a second time: in
a folder that other accounts can write, a link could have taken the name by then (see
[Folders you share](#folders-you-share)). The database copy is made by SQLite inside omaPOD's own data folder
and goes from there into that file.

**Importing a database** replaces the library with a file that may come from anywhere. omaPOD first copies the
file into its own data folder (only a regular file, not a pipe or a device) and works on that copy, so the
file cannot change after the check, and SQLite neither trusts nor creates anything next to the file you chose.
It reads the copy with SQLite's precautions for untrusted files and accepts it only if it passes the integrity
check and holds nothing but tables and indexes (no triggers, views or virtual tables). Downloads it lists that
are not in your download folder are forgotten, so the import cannot point omaPOD at your other files. If you
chose a download folder of your own, which may hold other files too, the import keeps only the downloads your
current library already has. Podcasts of local folders come along with their history, but not the permission
to use their folders (see [Your files](#your-files)). What the imported library had waiting for its sync server,
and what it remembered about your other devices, stays behind: sent from here, it could unsubscribe podcasts and
move positions on those devices. A queue and favorites you share with other omaPOD computers are combined with
theirs at the next synchronization, as on the first one. Addresses in the imported library lose a login written
into them, like those of an older library (see [Logins in addresses](#logins-in-addresses)).

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
| Your sync server (Nextcloud with GPodder Sync, or gpodder.net) | Synchronization | Login, subscriptions, episode actions (podcast and episode address, position, time), a device name made from your computer's host name (`omapod-<hostname>`); with Nextcloud, optionally your queue and favorites as files in the hidden folder `.omapod`. Addresses go as the podcasts publish them, so the address of a private feed goes with the access token in it; a login never goes along (see [Logins in addresses](#logins-in-addresses)) | Every 30 minutes, when the window gets focus, before playback, a few seconds after you pause, when you quit; with Nextcloud also every 3 minutes while idle |

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
  that server; the podcast is then subscribed at its address, and the password goes to that server only. For an
  address with `http://` the login dialog and the podcast's settings say that the password would travel
  unencrypted.
- **Redirects:** a request that is redirected to another server takes along only what every server may see
  (what omaPOD accepts, its name, a byte range, cache validators). Anything else a request carries for one
  server, like the Podcast Index key, stays with that server.
- **Sync:** TLS certificates are checked. A certificate your system does not trust (self-signed, your own
  certificate authority) is accepted only after you have seen its SHA-256 fingerprint and confirmed it, and
  from then on only exactly that certificate. For an address typed without `http://` or `https://`, omaPOD uses
  https, whatever its port (`host:80` too), and never falls back to plain http on its own: it offers http only
  for addresses in your local network or VPN, after asking. Every login over http, also one you typed with
  `http://`, shows a lasting warning in the sync settings. A server stored with `http://` and no port of its
  own is tried over https once each time omaPOD runs; if it answers there, with a certificate your system trusts
  or the one you confirmed, the login uses https from then on (up to 0.1.1, the address a Nextcloud behind a
  proxy named for itself could be stored as `http://` although you had typed `https://`).
- **Nextcloud login:** the login page, the login token and the app password stay with the server you entered,
  over the connection you chose (https, or http after asking). Behind some proxies Nextcloud names other
  addresses for them (`http://`, another port or host name); omaPOD then keeps their path but uses the server
  you entered. Before anything is stored or sent, omaPOD names the account the login in the browser ended in
  and asks whether to synchronize with it: the address of the login page is handed to the browser as a program
  argument, which other accounts on the computer can read while the browser starts, and whoever logs in on that
  page first decides the account. If you cancel, omaPOD asks the Nextcloud to forget the app password.

### Logins in addresses

`https://user:password@host/feed` carries a login, and an address goes wherever addresses go: to the sync
server, into exports, the download log. So no address omaPOD keeps or passes on carries one:

- A login in a podcast's address becomes the podcast's login, and the address is kept without it. That holds
  however the address arrives: typed, from a link or an OPML file, from a sync server (another app may have
  sent it there that way), with an imported library. A password without a name (`https://:token@host/feed`)
  counts too.
- A library from omaPOD 0.1.4 or earlier is cleaned every time it opens, not once: podcast and episode
  addresses, what waits to go to the sync server, the download log, what omaPOD remembers of earlier syncs.
- Addresses a feed names (its episodes, pictures, links, further pages, a new address for itself) and addresses
  a server redirects to lose a login written into them. It is not used: the one login a podcast has is the one
  you entered for it.
- Whatever goes to the sync server is checked once more on its way out. Only plain web addresses leave:
  `http://` or `https://`, a host, no login, and nothing that different programs read differently (extra or
  missing slashes, a backslash or white space in front of the path). omaPOD never sends an address with a login
  to a sync server, not even one that server sent.

A podcast that another device subscribed to with the login in its address keeps that name on the server. omaPOD
takes both names for the same podcast. Unsubscribing here is told to the server under the plain address, so the
other device keeps its subscription until you unsubscribe there.

## Other programs on your desktop

- **MPRIS:** while something is loaded, every program on your session bus can read the episode title, the
  podcast's name, the first 300 characters of the show notes, a cover thumbnail from omaPOD's cache and, for a
  downloaded episode, its file path. This is how media keys and Omarchy's media widget work. Stream addresses
  are not shared, as those of private feeds often carry an access token. Programs on the bus can control
  playback, raise omaPOD's window and quit it, but not make omaPOD open an address (MPRIS `OpenUri` is not
  supported).
- **Notifications** (new episodes of podcasts you switched them on for, failed downloads and synchronizations,
  playback going on in the background) go to your desktop's notification service: titles, the reason of a
  failure and, for a failed synchronization, the sync server's address.
- **Links** in show notes and podcast pages open only if they are web (http, https) or mail links; anything
  else is copied to the clipboard instead. A mail link opens your mail program with the address and the subject
  only, whatever else the link asks for (a text, more recipients, files to attach).
- **The bar widget** runs inside the Omarchy shell and reads omaPOD's state from a socket only your user can
  open. omaPOD sends it names, positions and cover thumbnails from its own cache: no addresses, and nothing
  longer than a name. The widget shows every text as plain text, loads no picture from the network and has no
  field to type into.
- **mpv** plays for omaPOD. It gets a stream's address (with the podcast's login if the stream is on the
  podcast's own server) through a control socket only your user can open, never as a program argument. It reads
  what a server sends as a media file only (`--demuxer=lavf`), not as a playlist, an archive or a cue sheet:
  left to itself, mpv would go on to play whatever addresses such an answer lists.
- **The installer** makes omaPOD the app for `podcast://`, `itpc://`, `pcast://` and `antennapod-subscribe://`
  links unless you picked another app for them. `./install.sh --uninstall` takes that back and removes the
  cache; your library stays.

## Your files

omaPOD deletes and moves only its own downloads: files in a podcast's folder inside the download folder
(`<download folder>/<podcast>/<file>`). If an episode names a file anywhere else, deleting the download only
makes omaPOD forget it; the file stays where it is.

Episodes of a local folder you added are your own files. omaPOD reads a folder only after you have chosen it
under **Add podcast → Add local folder** on this computer. That choice is kept in `settings.json`, never in the
library, so an imported database cannot bring folders of its own: its local podcasts wait until you choose their
folder here. Removing the podcast withdraws the permission. omaPOD never deletes these files on its own:
**Delete download** asks first, naming the file, and moves it to the trash, **Delete played downloads** leaves
them alone, and automatic deletion skips them unless you switch on **Auto delete from local folders** (then they
go to the trash too). Only audio and video files inside a folder you chose go to the trash, and which files those
are omaPOD learns from the folder itself, not from its database. Pictures on your disk are read only as covers
of these folders.

The permission is for the folder's path. If another account can change a folder on the way to it (the folder
lies in a shared place, or it is writable for a group or for everyone), that account can make the path lead to
another of your folders. omaPOD still plays what it finds there, but it moves nothing to the trash in such a
place, whatever the settings say: it says so and leaves the file where it is.

## Folders you share

The folder you export to, a download folder you chose and the folder you move your downloads to may be folders
that other accounts on the computer can write as well (a shared media folder, say). Whoever can write a folder
can put a symbolic link under any name in it at any moment, so in these folders omaPOD does not trust names
(for folders you added as podcasts, see [Your files](#your-files)):

- It opens the folder once and creates, renames and deletes files relative to that open folder.
- It writes only through files it created or opened itself, and it opens them without following a link. The
  unfinished download `.<number>.part`, whose name can be guessed, must be a plain file with that one name;
  a link, a pipe or a second name of another file loses the name, and a new file takes it.
- A podcast's folder inside the download folder must be a real folder. If a link has its name, omaPOD does not
  download into it, and files reached through it are not its downloads (they are never deleted or moved).
- Moving downloads never replaces a file and never follows a link in the new folder: a download gets a free
  name there, and across file systems it is copied into a newly created file.

Another account that can write such a folder can still delete or replace the files in it; that is what sharing
the folder means. It cannot use omaPOD to reach your files outside of it.

## Limits by design

- **Parental control** prevents accidents, not a determined user: anyone who can edit omaPOD's files can turn
  it off. The password is stored as a salted scrypt hash.
- Feeds, search results and server answers are untrusted input. omaPOD caps what it accepts (feed and download
  sizes, compressed answers, image dimensions, length of show notes, titles and transcripts) and takes only web
  addresses from them. Names and addresses appear as written, never as formatting; show notes keep simple
  formatting and links only. A podcast host can still see your IP address and when you download, like with any
  podcast app. Where a feed's files are fetched from is the feed's choice, also when it names or redirects to
  an address in your own network.
- A request has a time limit as a whole (five minutes for a feed), not only for each wait: a server that sends
  a byte now and then cannot hold up the refresh of your other podcasts. A download that gets less than 64 KiB
  in a minute is given up and can go on later from where it stopped.
- Your sync server, and whoever has your sync login, can add and remove subscriptions and move positions, like
  your other devices do. Removing a subscription removes the podcast here with its downloads. A server adds and
  removes web addresses only (never a folder you added), at most 5,000 new ones in one run.
