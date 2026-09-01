# Deployment

## Interactive systemd install

On a supported VPS, extract the release and run:

```bash
chmod +x install.sh
sudo ./install.sh
```

The installer creates:

- application: `/opt/cinevault/app`
- virtual environment: `/opt/cinevault/.venv`
- private data: `/var/lib/cinevault`
- service: `cinevault.service`
- management command: `/usr/local/bin/cinevaultctl`

The selected port defaults to `8765`. The service deliberately uses one Uvicorn worker because the Telegram session, callback queue, and transient bot-page tokens must share one process.

## GitHub one-line install

Run this as `root` on a supported VPS:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/acedev0/cinevault/main/install.sh)
```

For another public repository:

```bash
CINEVAULT_REPOSITORY=OWNER/REPOSITORY bash <(curl -Ls https://raw.githubusercontent.com/OWNER/REPOSITORY/main/install.sh)
```

For a private repository, export a narrowly scoped `GH_TOKEN` before running the installer. The token is used only by Git during the command and is not written into CineVault configuration.

## Firewall

The installer adds the selected TCP port when UFW is already active. Cloud-provider firewalls must be changed separately. Restrict the source IP range whenever possible.

## Safer remote HTTP access

HTTP does not protect the login password or streamed data in transit. A simple SSH tunnel keeps the service private:

```bash
ssh -L 8765:127.0.0.1:8765 root@YOUR-SERVER-IP
```

Then open `http://127.0.0.1:8765`. A private WireGuard/Tailscale network or an HTTPS reverse proxy is also suitable.

## Updates

Git-backed installs update without changing `/var/lib/cinevault`:

```bash
sudo cinevaultctl update
sudo cinevaultctl auto-update enable
```

An update fetches the configured branch, preserves the encrypted session/database/thumbnails, reinstalls pinned dependencies, and restarts the service. The command rolls the application checkout and service files back if installation fails.

## Access mode

```bash
sudo cinevaultctl mode public
sudo cinevaultctl mode private
```

Mode changes preserve the Telegram session, port, password hash, database, and cached previews, then restart only the CineVault service. Public mode bypasses the website login; signed VLC links work in either mode.

The installer also attempts to install Node.js, `puppeteer-core`, and a system Chromium build for best-effort poster enrichment. If Chromium is unavailable on a distribution, CineVault continues with Telegram thumbnails.

ZIP-only installations can be reinstalled from a newer extracted release. Automatic GitHub updates require the installed app to be a Git checkout.

### Update an existing ZIP-only installation

The private state lives in `/var/lib/cinevault`, outside the application checkout. For a default-path installation, upload and extract the new ZIP under `/root`, then run:

```bash
sudo systemctl stop cinevault
sudo cp -a /root/CineVault-1.1.0/. /opt/cinevault/app/
cd /opt/cinevault/app
sudo chmod +x install.sh
sudo ./install.sh
```

The installer detects `/var/lib/cinevault/secrets.enc`, keeps the Telegram session, API credentials, website login, port, database, and previews, then restarts the service. Never delete `/var/lib/cinevault` during an update.

## Docker

```bash
docker compose build
docker compose run --rm -it cinevault python -m cinevault --data-dir /data setup
docker compose up -d
```

The Compose file publishes `8765:8765` and stores configuration, metadata, and thumbnails in `./data`. Protect that directory and never commit it.

## Backup

Stop the service, then back up `/var/lib/cinevault` as one unit. The encrypted configuration cannot be decrypted without the matching `.master_key`.

```bash
sudo systemctl stop cinevault
sudo tar -C /var/lib -czf cinevault-backup.tgz cinevault
sudo systemctl start cinevault
```

## Uninstall

```bash
sudo cinevaultctl uninstall --yes
```

This removes the service, code, service account, firewall rule, encrypted Telegram session, database, and thumbnails. It never deletes messages or files from Telegram.
