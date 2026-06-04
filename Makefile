.PHONY: help sync server client repl chrome clean clean-chrome

CHROME ?= $(shell command -v google-chrome 2>/dev/null || command -v google-chrome-stable 2>/dev/null || command -v chromium 2>/dev/null || command -v chromium-browser 2>/dev/null || command -v chrome 2>/dev/null)
EXT_DIR := $(CURDIR)/extension

# Optional session name (by id or name for the client; a label for chrome).
#   make client SESSION=work   -> REPL targets the "work" browser
#   make chrome SESSION=work   -> launch a Chrome whose session is named "work"
SESSION ?=

# Per-session Chrome profile + a staged extension copy that carries the name.
# With no SESSION, fall back to the single default profile and the raw extension.
ifeq ($(strip $(SESSION)),)
PROFILE_DIR := $(CURDIR)/.chrome-profile
CHROME_EXT_DIR := $(EXT_DIR)
else
PROFILE_DIR := $(CURDIR)/.chrome-profile-$(SESSION)
CHROME_EXT_DIR := $(CURDIR)/.chrome-stage-$(SESSION)
endif

help:
	@echo "Targets:"
	@echo "  make sync     - uv sync both projects"
	@echo "  make server   - run the bridge server (foreground)"
	@echo "  make client   - launch the client REPL"
	@echo "                  target a session with SESSION=<id|name> (e.g. make client SESSION=work)"
	@echo "  make repl     - alias for 'client'"
	@echo "  make chrome   - launch a clean Chrome window with the extension loaded"
	@echo "                  (isolated profile at $(PROFILE_DIR), no overlap with your real Chrome)"
	@echo "                  override with CHROME=/path/to/chrome"
	@echo "                  SESSION=<name> launches a separate, named browser session"
	@echo "                  (own profile .chrome-profile-<name>; run several at once)"
	@echo "  make clean    - remove .venv dirs and dumps/"
	@echo ""
	@echo "Typical use: 'make server' in one terminal, 'make client' in another."

sync:
	cd server && uv sync
	cd client && uv sync

server:
	cd server && uv run chrome-dumper-server

client:
	cd client && uv run dumper $(if $(strip $(SESSION)),--session "$(SESSION)")

repl: client

chrome:
	@test -n "$(CHROME)" || { echo "no chrome binary found; set CHROME=/path/to/chrome (or chromium)"; exit 1; }
	@mkdir -p "$(PROFILE_DIR)"
ifneq ($(strip $(SESSION)),)
	@rm -rf "$(CHROME_EXT_DIR)"
	@cp -r "$(EXT_DIR)" "$(CHROME_EXT_DIR)"
	@printf '{"name":"%s"}\n' '$(SESSION)' > "$(CHROME_EXT_DIR)/session.json"
	@echo "launching Chrome session '$(SESSION)'  (profile: $(PROFILE_DIR))"
endif
	@echo "if the badge stays OFF, this Chrome build may block --load-extension;"
	@echo "load $(CHROME_EXT_DIR) once via chrome://extensions > Load unpacked."
	"$(CHROME)" \
	    --user-data-dir="$(PROFILE_DIR)" \
	    --load-extension="$(CHROME_EXT_DIR)" \
	    --disable-features=DisableLoadExtensionCommandLineSwitch \
	    --no-first-run \
	    --no-default-browser-check \
	    --new-window \
	    "about:blank"

clean:
	rm -rf server/.venv client/.venv client/dumps

clean-chrome:
	rm -rf $(CURDIR)/.chrome-profile $(CURDIR)/.chrome-profile-* $(CURDIR)/.chrome-stage-*
