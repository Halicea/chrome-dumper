.PHONY: help sync server client repl chrome firefox clean clean-chrome clean-firefox

CHROME ?= $(shell command -v google-chrome 2>/dev/null || command -v google-chrome-stable 2>/dev/null || command -v chromium 2>/dev/null || command -v chromium-browser 2>/dev/null || command -v chrome 2>/dev/null)
FIREFOX ?= $(shell command -v firefox 2>/dev/null || command -v firefox-developer-edition 2>/dev/null || command -v firefox-nightly 2>/dev/null)
FIREFOX_PROFILE_DIR := $(CURDIR)/.firefox-profile
EXT_DIR := $(CURDIR)/extension
FIREFOX_EXT_DIR := $(CURDIR)/firefox-extension

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
	@echo "  make firefox  - launch Firefox with the firefox-extension/ loaded temporarily"
	@echo "                  (isolated profile at $(FIREFOX_PROFILE_DIR); override with FIREFOX=/path/to/firefox)"
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
	"$(CHROME)" \
	    --user-data-dir="$(PROFILE_DIR)" \
	    --load-extension="$(CHROME_EXT_DIR)" \
	    --no-first-run \
	    --no-default-browser-check \
	    --new-window \
	    "about:blank"

firefox:
	@test -n "$(FIREFOX)" || { echo "no firefox binary found; set FIREFOX=/path/to/firefox"; exit 1; }
	@mkdir -p $(FIREFOX_PROFILE_DIR)
	@if command -v web-ext >/dev/null 2>&1; then \
	  echo "launching via web-ext (recommended)"; \
	  cd $(FIREFOX_EXT_DIR) && web-ext run \
	    --firefox="$(FIREFOX)" \
	    --firefox-profile="$(FIREFOX_PROFILE_DIR)" \
	    --keep-profile-changes \
	    --no-reload \
	    --url about:blank; \
	else \
	  echo "web-ext not found (npm i -g web-ext). Falling back to manual install."; \
	  echo "Launching Firefox with an isolated profile; load the extension via about:debugging > 'Load Temporary Add-on' > pick $(FIREFOX_EXT_DIR)/manifest.json"; \
	  "$(FIREFOX)" -profile "$(FIREFOX_PROFILE_DIR)" -no-remote -new-instance about:debugging#/runtime/this-firefox; \
	fi

clean:
	rm -rf server/.venv client/.venv client/dumps

clean-chrome:
	rm -rf $(CURDIR)/.chrome-profile $(CURDIR)/.chrome-profile-* $(CURDIR)/.chrome-stage-*

clean-firefox:
	rm -rf $(FIREFOX_PROFILE_DIR)
