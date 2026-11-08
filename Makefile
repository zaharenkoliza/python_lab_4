CFG ?= configs/experiment.yaml
.PHONY: build smoke experiment analyze all ci down
build:      ; docker compose build 2>/dev/null || docker-compose build
smoke:      ; ./scripts/smoke.sh
experiment: ; uv run python runner/run_experiment.py $(CFG)
analyze:    ; uv run python analysis/analyze.py $(CFG)
all:        build smoke experiment analyze
ci:         ; $(MAKE) experiment analyze CFG=configs/ci.yaml
down:       ; docker compose --profile monitoring down -v 2>/dev/null || docker-compose --profile monitoring down -v
