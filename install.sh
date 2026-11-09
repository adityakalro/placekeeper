#!/usr/bin/env bash
set -eo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

venv_args=()
pip_args=()
requirements=requirements-onboarding.txt
if command -v termux-info >/dev/null && command -v pkg >/dev/null; then
    echo 'Installing Placekeeper for Termux.'
    pkg install -y git python python-pip python-pillow python-lxml python-pycryptodomex
    # Use Termux’s packaged pip and native Python modules.
    venv_args+=(--system-site-packages --without-pip)
    pip_args+=(--only-binary=Pillow,lxml)
    requirements=requirements.txt
elif [[ "$(uname -s)" == Linux ]]; then
    echo 'Installing Placekeeper for Linux.'
    if (( EUID == 0 )) && command -v apt-get >/dev/null; then
        if ! dpkg-query -W -f='${Status}\n' git python3 python3-venv 2>/dev/null |
            awk '$0 == "install ok installed" { count++ } END { exit (count != 3) }'; then
            apt-get update
            apt-get install -y git python3 python3-venv
        fi
    fi
else
    echo 'Placekeeper installation is supported on Linux and Termux.' >&2
    exit 1
fi

if ! command -v git >/dev/null || ! command -v python3 >/dev/null; then
    echo 'Install Git and Python 3.13 or newer with venv using your package manager, then rerun.' >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 13))'; then
    echo 'Placekeeper requires Python 3.13 or newer. Update python3 and rerun.' >&2
    exit 1
fi
if ! python3 -m venv "${venv_args[@]}" .venv; then
    echo 'Install the venv and pip support for your Python version, then rerun.' >&2
    exit 1
fi
.venv/bin/python -m pip install "${pip_args[@]}" -r "$requirements"
.venv/bin/python -m pip check
.venv/bin/python -c 'import audible, httpx, PIL, lxml.etree, Cryptodome'

(
    umask 077
    data_dir=$(.venv/bin/python -c 'from placekeeper.storage import default_data_root; print(default_data_root())')
    mkdir -p "$data_dir/vendor"
    if [[ ! -e "$data_dir/web-config.json" && ! -L "$data_dir/web-config.json" ]]; then
        (set -o noclobber; cat deploy/web-config.example.json > "$data_dir/web-config.json")
    fi

    parser="$data_dir/vendor/kindle.koplugin"
    revision=501488fd99ace3ea143a8693b9fa7c6421101d51
    if [[ ! -d "$parser/.git" ]]; then
        git init "$parser"
    fi
    if [[ "$(git -C "$parser" rev-parse --verify HEAD 2>/dev/null || true)" != "$revision" ]]; then
        git -C "$parser" fetch --depth 1 https://github.com/kaikozlov/kindle.koplugin.git "$revision"
        git -C "$parser" checkout --detach "$revision"
    fi
    if [[ ! -e "$data_dir/vendor/python" && ! -L "$data_dir/vendor/python" ]]; then
        ln -s kindle.koplugin/python "$data_dir/vendor/python"
    fi
    if [[ ! -d "$data_dir/vendor/python/kfxlib" ]]; then
        echo "The KFX parser is missing from $data_dir/vendor/python. Check the existing path and rerun." >&2
        exit 1
    fi
)
echo 'Start Placekeeper with: .venv/bin/python -m placekeeper.web.app'
echo 'With the default configuration, open http://127.0.0.1:8780/setup'
