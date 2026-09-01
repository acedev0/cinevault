# Troubleshooting

Start with:

```bash
cinevaultctl status
cinevaultctl logs
sudo cinevaultctl doctor
```

## The bot does not return results

- Open the configured bot in the official Telegram app and make sure `/start`, membership, or other required access steps are already complete.
- Confirm the bot sends inline callback buttons and returns the selected file into the same private bot conversation.
- CineVault intentionally ignores URL, payment, phone-sharing, and other non-callback buttons.
- Telegram may rate-limit repeated searches. Wait for the reported interval rather than restarting repeatedly.
- Run a direct diagnostic while the website service is stopped:

```bash
sudo systemctl stop cinevault
sudo -u cinevault /opt/cinevault/.venv/bin/python -m cinevault --data-dir /var/lib/cinevault search-test "Spider Man"
sudo systemctl start cinevault
```

## Search results expired

Bot result tokens live in memory for 15 minutes and are tied to the exact Telegram message layout. Search again after a restart, timeout, or bot-side edit. This prevents stale callbacks from triggering the wrong title.

## A file was deleted but a card remains

Deletion updates normally remove it immediately. Force a reconciliation batch:

```bash
sudo cinevaultctl prune
```

Opening or streaming the card also performs an authoritative Telegram check and removes it with HTTP 410 if the source is gone.

## MKV does not play

The file is still being range-delivered correctly; the browser may not support its container or codecs. Try Firefox, or choose **Original file** and open it in VLC. MP4 with H.264 video and AAC audio has the widest browser support.

## Seeking is slow

- Test VPS-to-Telegram network quality.
- Avoid reverse-proxy response buffering.
- Keep one Uvicorn worker.
- A video with sparse keyframes may seek slowly even when byte ranges work correctly.
- Large simultaneous streams consume VPS bandwidth even though they consume no original-file disk space.

## Telegram session is unauthorised

```bash
sudo cinevaultctl configure
```

This repeats Telegram login and website setup. Keep a backup of `/var/lib/cinevault` before reconfiguration.

## Port is unreachable

Check the selected port in `/var/lib/cinevault/runtime.env`, then inspect both UFW and the VPS provider's cloud firewall. The default is TCP `8765`.

## Service starts as degraded

Read recent logs:

```bash
journalctl -u cinevault -n 100 --no-pager
```

Common causes are revoked Telegram sessions, renamed/deleted bot accounts, outbound Telegram connectivity blocks, invalid encrypted configuration, or a data directory permission change.
