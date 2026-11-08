# Run Placekeeper on Android with Termux

Install [Termux](https://github.com/termux/termux-app#installation), then open its terminal. Keep the checkout in Termux’s private home directory so it can run its virtual environment and store credentials there.

```sh
pkg update && pkg upgrade
pkg install git
cd "$HOME"
git clone https://github.com/adityakalro/placekeeper.git
cd placekeeper
bash install.sh
.venv/bin/python -m placekeeper.web.app
```

The installer uses the Android builds of Pillow and lxml from Termux. Open [Setup](http://127.0.0.1:8780/setup) in your Android browser, [connect your accounts](../README.md#3-connect-your-accounts), and [sync a first book](../README.md#4-sync-your-first-book). Press Ctrl+C in Termux to stop the foreground server before starting the background service.

## Run in the background

Install [termux-services](https://github.com/termux/termux-services), then close and reopen the Termux shell to start its service supervisor.

```sh
pkg install termux-services
```

Create and start the Placekeeper service. These commands assume you cloned it to `$HOME/placekeeper`.

```sh
service_dir="$PREFIX/var/service/placekeeper"
mkdir -p "$service_dir/log"
touch "$service_dir/down"
cat > "$service_dir/run" <<RUN
#!$PREFIX/bin/sh
exec 2>&1
cd "$HOME/placekeeper" || exit 1
exec .venv/bin/python -u -m placekeeper.web.app
RUN
ln -sf "$PREFIX/share/termux-services/svlogger" "$service_dir/log/run"
chmod +x "$service_dir/run"
sv-enable placekeeper
sv status placekeeper
```

Use `sv down placekeeper` to stop it for the current session, `sv up placekeeper` to start it again, or `sv-disable placekeeper` to disable automatic starts. Logs are in `$PREFIX/var/log/sv/placekeeper/current`.

## Start after an Android reboot

Install [Termux:Boot](https://github.com/termux/termux-boot) from the same source as Termux and launch it once. Create the following boot script to start the supervisor, which in turn starts enabled services:

```sh
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/start-placekeeper" <<BOOT
#!$PREFIX/bin/sh
termux-wake-lock
. "$PREFIX/etc/profile.d/start-services.sh"
BOOT
chmod +x "$HOME/.termux/boot/start-placekeeper"
```

The wake lock keeps the device awake and can increase battery use. Set Termux’s Android battery setting to unrestricted if your phone offers that option. After rebooting and unlocking the phone, open the local Sync page to confirm it came up. Android can still stop background processes, so check that scheduled sync works with the screen off if you rely on it.

To run progress sync every 30 minutes, enable it in Placekeeper’s **Setup** page. Use **Find new books** to discover and map new titles.
