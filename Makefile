# CI / deployment for fb-slideshow.
#
#   make test          tests on this machine (itaka), with the same Python and
#                      library versions as on hydra (requirements-ci.txt)
#   make deploy        copy the program to hydra (scp) and install/update the
#                      systemd user service (starts the show at boot)
#   make test-hydra    run the tests on hydra, check that versions match and
#                      verify the service file
#   make start|stop|restart|pause|resume|status
#                      control the show on hydra (ssh -> systemctl --user)
#   make config SHOW_ARGS="--shuffle --hold 20"
#                      set the show's options, on top of DEFAULT_ARGS in
#                      ci/fb-slideshow.service (kept across reboots and
#                      deploys); SHOW_ARGS= goes back to the defaults
#   make sequence CONFIG=config.yaml
#                      check the file on hydra and make it the sequence
#                      (/mnt/stick/config.yaml); the show picks it up
#   make scene         stop the show and play the "140" scene on hydra
#                      (SCENE_ARGS="--repeat 3", "--repeat 0" loops until
#                      Ctrl+C); the show stays stopped,
#                      "make start" brings it back
#   make scene-preview render the scene to scene140.gif on this machine
#   make ci            test + deploy + test-hydra + restart (what CI runs)
#   make hydra-host    show which address of hydra is used
#
# hydra is reached at the first of HYDRA_CANDIDATES that answers over ssh:
# the WireGuard VPN address (works from any network), then the name "hydra"
# (/etc/hosts, home LAN), then hydra's Wi-Fi address. Force one with
# "make deploy HYDRA=<address>".

HYDRA_CANDIDATES ?= 192.168.4.203 hydra 192.168.4.142
# fixed: ci/fb-slideshow.service runs %h/fb-slideshow
DEPLOY_DIR := fb-slideshow
SHOW_ARGS  ?=
PYTHON_VERSION ?= 3.13.5

UV   ?= $(shell command -v uv 2>/dev/null || echo $(HOME)/.local/bin/uv)
VENV := .venv-ci
SSH  := ssh -o BatchMode=yes -o ConnectTimeout=10
SCP  := scp -q -o BatchMode=yes -o ConnectTimeout=10

# Probed only when a target needs hydra, and only once per make run
# (the variable replaces itself with the result).
ifeq ($(origin HYDRA),undefined)
HYDRA = $(eval HYDRA := $(call find_hydra))$(HYDRA)
endif
find_hydra = $(or $(shell for h in $(HYDRA_CANDIDATES); do \
	ssh -o BatchMode=yes -o ConnectTimeout=4 "$$h" true 2>/dev/null && { echo "$$h"; break; }; \
	done),$(error hydra not reachable via any of: $(HYDRA_CANDIDATES)))

DEPLOY_FILES := fb-slideshow.py test_fb_slideshow.py requirements.txt \
	scene140.py test_scene140.py sequence.py test_sequence.py config.example.yaml
SCENE_ARGS ?=
VERSIONS := import sys, numpy, PIL; print(sys.version.split()[0], numpy.__version__, PIL.__version__)

.PHONY: test deploy test-hydra start stop restart pause resume status config sequence measure-fps scene scene-preview movie diashow-setup diashow-sync show-setup ci clean hydra-host

$(VENV)/.stamp: requirements-ci.txt
	$(UV) venv -q --clear --python $(PYTHON_VERSION) $(VENV)
	$(UV) pip install -q --python $(VENV)/bin/python -r requirements-ci.txt
	touch $@

test: $(VENV)/.stamp
	$(VENV)/bin/python -m pytest -q -p no:cacheprovider

deploy:
	$(SSH) $(HYDRA) 'mkdir -p $(DEPLOY_DIR)/ci $(DEPLOY_DIR)/fonts'
	$(SCP) $(DEPLOY_FILES) $(HYDRA):$(DEPLOY_DIR)/
	@# the font (1 MB) only when it changed, keeps SD-card writes down
	@$(SSH) $(HYDRA) 'md5sum $(DEPLOY_DIR)/fonts/Ubuntu.ttf 2>/dev/null' | \
		sed 's| .*||' | grep -qx "$$(md5sum fonts/Ubuntu.ttf | sed 's| .*||')" || \
		$(SCP) fonts/Ubuntu.ttf fonts/LICENCE.txt $(HYDRA):$(DEPLOY_DIR)/fonts/
	$(SCP) ci/show.sh ci/fb-slideshow.service ci/measure_fps.py \
		ci/diashow-sync.sh ci/diashow-sync.service ci/diashow-sync.timer \
		ci/diashow-setup.sh ci/show ci/setup-show.sh $(HYDRA):$(DEPLOY_DIR)/ci/
	$(SSH) $(HYDRA) 'chmod +x $(DEPLOY_DIR)/ci/*.sh $(DEPLOY_DIR)/ci/show && $(DEPLOY_DIR)/ci/show.sh install'
	@echo "deployed to $(HYDRA):$(DEPLOY_DIR)"

test-hydra: $(VENV)/.stamp
	@local="$$($(VENV)/bin/python -c '$(VERSIONS)')"; \
	remote="$$($(SSH) $(HYDRA) "python3 -c '$(VERSIONS)'")"; \
	echo "itaka: $$local"; echo "hydra: $$remote"; \
	if [ "$$local" != "$$remote" ]; then \
		echo "version mismatch: update requirements-ci.txt / PYTHON_VERSION" >&2; exit 1; fi
	$(SSH) $(HYDRA) 'cd $(DEPLOY_DIR) && PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider'
	$(SSH) $(HYDRA) 'cmp $(DEPLOY_DIR)/ci/fb-slideshow.service ~/.config/systemd/user/fb-slideshow.service && systemd-analyze --user verify ~/.config/systemd/user/fb-slideshow.service && echo "service file ok (installed = repo copy)"'

start stop restart pause resume status:
	@$(SSH) $(HYDRA) '$(DEPLOY_DIR)/ci/show.sh $@'

config:
	@$(SSH) $(HYDRA) "$(DEPLOY_DIR)/ci/show.sh config '$(SHOW_ARGS)'"

CONFIG ?= config.yaml
sequence:
	@test -f "$(CONFIG)" || { echo "no $(CONFIG) (start from config.example.yaml)" >&2; exit 1; }
	@$(SCP) "$(CONFIG)" $(HYDRA):/mnt/stick/config.yaml.new
	@$(SSH) $(HYDRA) '$(DEPLOY_DIR)/ci/show.sh sequence /mnt/stick/config.yaml.new'

# frame rate of transitions on the real panel (stops the show meanwhile)
measure-fps:
	$(SCP) ci/measure_fps.py $(HYDRA):$(DEPLOY_DIR)/ci/
	$(SSH) $(HYDRA) '$(DEPLOY_DIR)/ci/show.sh stop'
	-$(SSH) $(HYDRA) 'PYTHONDONTWRITEBYTECODE=1 python3 $(DEPLOY_DIR)/ci/measure_fps.py --transitions stack,cut,fade'
	$(SSH) $(HYDRA) '$(DEPLOY_DIR)/ci/show.sh start'

# the "140" scene (issue #11); the show stays stopped afterwards
scene:
	$(SSH) $(HYDRA) '$(DEPLOY_DIR)/ci/show.sh stop'
	@# -t: Ctrl+C reaches the scene on hydra
	-$(SSH) -t $(HYDRA) 'PYTHONDONTWRITEBYTECODE=1 python3 $(DEPLOY_DIR)/scene140.py $(SCENE_ARGS)'
	@echo "show stopped; bring it back with: make start"

scene-preview: $(VENV)/.stamp
	$(VENV)/bin/python scene140.py --preview scene140.gif $(SCENE_ARGS)

# pictures from Nextcloud (issue #25): set up the remote once (asks for the
# app password on hydra), fetch now
diashow-setup:
	$(SSH) -t $(HYDRA) '$(DEPLOY_DIR)/ci/diashow-setup.sh'

diashow-sync:
	@$(SSH) $(HYDRA) '$(DEPLOY_DIR)/ci/show.sh diashow-sync'

# "show <command>" for other users on hydra (asks for eduard's sudo password
# there); more users later: sudo usermod -aG slideshow <name>
USERS ?=
show-setup:
	$(SSH) -t $(HYDRA) 'sudo sh $(DEPLOY_DIR)/ci/setup-show.sh $(USERS)'

# the sequence of CONFIG as a movie, offline (issue #23); pictures and
# background from bilder/ and background/ here; SHOW_ARGS as on hydra
MOVIE ?= $(basename $(CONFIG)).mp4
MOVIE_ARGS ?=
movie: $(VENV)/.stamp
	$(VENV)/bin/python movie.py "$(CONFIG)" -o "$(MOVIE)" --show-args "$(SHOW_ARGS)" $(MOVIE_ARGS)

ci: test deploy test-hydra restart

clean:
	rm -rf $(VENV)

hydra-host:
	@echo $(HYDRA)
