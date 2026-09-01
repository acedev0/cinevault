# CineVault user guide

## Find a title

Use the large search field in the top navigation to query the configured Telegram film bot. CineVault shows the bot's own language, resolution, category, result, and pagination buttons on the same page.

Bot searches are intentionally serialized. If several visitors search at once, later searches wait briefly so one person's Telegram response cannot be attached to another person's request.

## Build the saved library

Selecting a result asks the Telegram bot to send the file into its private chat with your user account. CineVault saves only the message metadata and a small preview, then adds the item to the library. The complete source remains in Telegram.

The library has one page. Keep **Infinite** enabled to load more cards while scrolling, or disable it to reveal a manual **Load more** button. **Shuffle** creates a deterministic randomized order across the complete filtered library.

## Watch and control playback

| Key | Action |
| --- | --- |
| `Space` or `K` | Play or pause |
| `←` / `→` | Seek backward or forward 5 seconds |
| `J` / `L` | Seek backward or forward 10 seconds |
| `↑` / `↓` | Raise or lower volume |
| `M` | Mute |
| `F` | Full screen |
| `T` | Theatre mode |
| `I` | Picture in picture |
| `C` | Toggle captions when available |
| `Shift` + `,` / `.` | Slower or faster playback |
| `Home` / `End` | Jump to the beginning or end |
| `0`–`9` | Jump to a percentage of the video |
| `N` / `P` | Next or previous item |

Keyboard shortcuts are ignored while typing in a field, select menu, or editable element.

## Open a title in VLC

On a watch page, choose **Copy VLC link**. The link is a signed bearer URL that works without browser cookies and supports seeking through HTTP Range requests for seven days.

```bash
vlc "PASTE-SIGNED-LINK-HERE"
```

Treat the URL like a temporary password: anyone who has it can stream that item until it expires. Deleting the Telegram source invalidates the link immediately.

## Public and private modes

```bash
sudo cinevaultctl mode public
sudo cinevaultctl mode private
```

Public mode removes the website login page. Private mode restores it. Both preserve the Telegram session, port, password hash, database, saved media, and previews.

## If Telegram deletes a file

CineVault removes the matching website card through live deletion events, background reconciliation, or the next watch/stream request. Run a manual sweep with:

```bash
sudo cinevaultctl prune
```

## Everyday management

```bash
cinevaultctl status
cinevaultctl logs
sudo cinevaultctl doctor
sudo cinevaultctl restart
sudo cinevaultctl update
```

See [Deployment](DEPLOYMENT.md) for installation and updates, and [Troubleshooting](TROUBLESHOOTING.md) for recovery steps.
