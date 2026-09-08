.PHONY: setup run test evidence-offline evidence samples preflight preflight-synth pronunciation secrets demo clean

PY ?= .venv/bin/python

setup:
	python3 -m venv .venv && $(PY) -m pip install -q --upgrade pip && $(PY) -m pip install -q -e ".[dev]"
	@test -f .env || cp .env.example .env
	@echo "Now edit .env (RIME_API_KEY at minimum), then: make preflight-synth && make run"

run:
	$(PY) -m uvicorn forgedesk.server:app --host $${HOST:-127.0.0.1} --port $${PORT:-8080}

test:
	$(PY) -m pytest -q

# Logic-only run with the offline tone generator. NOT evidence.
evidence-offline:
	$(PY) -m eval.run --tts fake --runs 5

# Judged evidence: real Rime, 20 runs per scenario. Needs RIME_API_KEY.
evidence:
	$(PY) -m eval.run --tts rime --runs $${RUNS:-20}

preflight:
	$(PY) -m scripts.preflight

preflight-synth:
	$(PY) -m scripts.preflight --synth

pronunciation:
	$(PY) -m scripts.render_variants

# Curate a small committed sample: one "what the user heard" clip per scenario + pronunciation clips.
samples:
	$(PY) -m scripts.collect_samples

secrets:
	$(PY) -m scripts.secret_scan

# Record the demo video by driving the real product (needs RIME_API_KEY + playwright chromium).
demo:
	$(PY) -m scripts.demo.render_voice
	$(PY) -m scripts.demo.record
	$(PY) -m scripts.demo.compose

clean:
	rm -rf evidence/runs evidence/clips .pytest_cache demo/build demo/capture
