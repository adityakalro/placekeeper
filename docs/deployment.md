# Run Placekeeper as a Linux service

This guide installs Placekeeper on a Linux host with systemd, such as DietPi. For a regular-user installation in your home directory, follow the [README](../README.md#1-install-placekeeper). Run the commands below as root, or use `sudo` for the administrative commands.

## 1. Install the app and create its data directory

On Debian 13, install Git and Python 3.13 with its virtual environment module:

```sh
apt update
apt install git
```

For a fresh installation:

```sh
useradd --system --user-group --home-dir /var/lib/placekeeper --shell /usr/sbin/nologin placekeeper
install -d -o placekeeper -g placekeeper -m 700 /var/lib/placekeeper
git clone https://github.com/adityakalro/placekeeper.git /opt/placekeeper
cd /opt/placekeeper
ln -s /var/lib/placekeeper .private
bash install.sh
chown -R placekeeper:placekeeper /var/lib/placekeeper
```

The service runs as `placekeeper`. The checkout and virtual environment live under `/opt/placekeeper`. Credentials, settings, and book maps live under `/var/lib/placekeeper`. The `.private` symlink created above makes the installer and service use that directory.

## 2. Start the service and connect accounts

```sh
install -m 644 /opt/placekeeper/deploy/placekeeper-web.service /etc/systemd/system/placekeeper-web.service
systemctl daemon-reload
systemctl enable --now placekeeper-web.service
systemctl status placekeeper-web.service --no-pager
```

The example configuration listens on localhost. On your computer, keep this SSH tunnel open:

```sh
ssh -N -L 127.0.0.1:8780:127.0.0.1:8780 YOUR_USER@YOUR_PI_HOST
```

Open [Setup](http://127.0.0.1:8780/setup), [connect your accounts](../README.md#3-connect-your-accounts), and [sync a first book](../README.md#4-sync-your-first-book). The web page starts at boot. Use **Find new books** for discovery and mapping and **Sync now** for progress. To sync progress every 30 minutes, enable automatic progress sync in **Setup**. The web service saves the setting across restarts and starts a fresh interval on startup. A scheduled run skips its turn if another job is active or the necessary services are disconnected.

## 3. Optional LAN or WireGuard access

Edit `/var/lib/placekeeper/web-config.json`. Set `bind` to a private interface address, or use `0.0.0.0` for multiple interfaces. Keep the trusted client ranges in `networks` and list each address you actually visit in `hosts`, including its port. For example, replace the example LAN address and subnet below with your own. Add your WireGuard address and subnet if you use it.

```json
{
  "bind": "0.0.0.0",
  "port": 8780,
  "hosts": ["127.0.0.1:8780", "192.168.1.20:8780"],
  "networks": ["127.0.0.0/8", "192.168.1.0/24"]
}
```

Run `systemctl restart placekeeper-web.service` after changing the file. Use a trusted private LAN, SSH tunnel, or WireGuard. Keep port 8780 off public router forwards. The page shares the installation’s accounts with clients allowed by its network rules.

If the page fails to start, check `journalctl -u placekeeper-web.service -n 30 --no-pager`. A **Private network required** or access-denied response usually means the address or client subnet is missing from the configuration.

## Upgrading from Book Sync

Stop the old web service and, if installed, the optional onboarding service
before copying the saved data. Use the fresh installation commands above to
create the Placekeeper service account and checkout. Keep a copy of the old data
until you have verified the upgrade.

```sh
systemctl disable --now book-sync-web.service
# Run this if the old onboarding unit was installed.
systemctl disable --now book-sync-onboarding.service
```

Copy the old private data and update its saved paths before starting Placekeeper.
This also carries over the web configuration and progress schedule.

```sh
cp -a /var/lib/book-sync/. /var/lib/placekeeper/
chown -R placekeeper:placekeeper /var/lib/placekeeper
cd /opt/placekeeper
runuser -u placekeeper -- .venv/bin/python -m deploy.relocate \
  --data-root /var/lib/placekeeper --previous-root /var/lib/book-sync
```

Follow the service startup commands above and verify the Sync and Setup pages
using your existing address.

## Moving an existing installation

Transfer the saved private data over SSH, preserve file permissions, and ensure the service user owns it. Existing accounts can be reused. Relocate map paths and registry fingerprints before starting the service:

```sh
cd /opt/placekeeper
runuser -u placekeeper -- .venv/bin/python -m deploy.relocate \
  --data-root /var/lib/placekeeper --previous-root /path/to/old/private
```

Verify account access, a fresh download, and a manual sync on the new host before retiring the old installation. Keep personal SSH addresses and connection notes in the ignored local `DEPLOYMENT.md`.
