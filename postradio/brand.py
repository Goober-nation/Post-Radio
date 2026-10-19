"""Every user-visible name lives here. To rebrand: change these values, rename the package directory,
and update `name`/`[project.scripts]` in pyproject.toml (plus prose in README/docs)."""

NAME = "postradio"  # CLI command, log/error prefix, data-dir name, API title
DISPLAY_NAME = "Post-radio"
VERSION = "0.1.0"
CONTACT = "tim.13102006@gmail.com"  # MusicBrainz asks for a contact in the User-Agent
ENV_PREFIX = "POSTRADIO"

USER_AGENT = f"{NAME}/{VERSION} ( {CONTACT} )"
CONFIG_ENV = f"{ENV_PREFIX}_CONFIG"  # env var pointing at the YAML config
DEFAULT_SAVE_DIR = f"~/.local/share/{NAME}/playlists"
POOL_PREFIX = f"{NAME}-fetch"  # worker thread names
SUBSONIC_CLIENT = NAME  # 'c' parameter Navidrome logs for our requests
