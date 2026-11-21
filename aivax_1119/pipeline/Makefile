PYTHON ?= /usr/local/bin/python3.13
UV ?= /usr/local/bin/uv
VENV := /home1/aivax/aivax-venv
DEV_VENV := .venv-dev
PYTHON_VENV := $(VENV)/bin/python
PYTHON_DEV_VENV := $(DEV_VENV)/bin/python
WHEEL_DIR ?= offline-wheel
OUT ?= dist

SMOKE_ENV_FILE ?= .env.development
INTEGRATION_TEST_ARGS ?=
UNIT_TEST_ARGS ?=
TEST_ARGS ?=
PIPELINE_CPUSET ?=
OPENBLAS_NUM_THREADS ?= 1
OMP_NUM_THREADS ?= 1
MKL_NUM_THREADS ?= 1
NUMEXPR_NUM_THREADS ?= 1

NATIVE_THREAD_ENV := \
	OPENBLAS_NUM_THREADS=$(OPENBLAS_NUM_THREADS) \
	OMP_NUM_THREADS=$(OMP_NUM_THREADS) \
	MKL_NUM_THREADS=$(MKL_NUM_THREADS) \
	NUMEXPR_NUM_THREADS=$(NUMEXPR_NUM_THREADS)
RUN_PREFIX := env $(NATIVE_THREAD_ENV)
ifneq ($(strip $(PIPELINE_CPUSET)),)
RUN_PREFIX := taskset -c "$(PIPELINE_CPUSET)" env $(NATIVE_THREAD_ENV)
endif

.PHONY: help setup setup-prod setup-dev setup-offline setup-dev-offline clean clean-dev run dev prod check test test-unit test-integration lint format wheels wheel package api-docs api-docs-check

help:
	@printf '%s\n' \
	  'make setup                 Create $(VENV) with runtime dependencies' \
	  'make setup-dev             Create .venv-dev with runtime and development dependencies' \
	  'make setup-dev-offline     Create .venv-dev from locally cached dependencies' \
	  'make setup-offline         Create $(VENV) with runtime and development dependencies from offline-wheel/' \
	  'make clean                 Remove the production virtual environment' \
	  'make clean-dev             Remove project virtual environments and Python caches' \
	  'make dev                   Run Pipeline with .env.development' \
	  'make prod                  Run Pipeline with .env' \
	  'make run                   Alias for make dev' \
	  'make check                 Verify the local runtime imports' \
	  'make test                  Run all unit, API, Fluent Bit and Docker/CSO tests' \
	  'make test-unit             Run tests without external API or Docker integration' \
	  'make test-integration      Run real Pipeline/CSO with Docker Fluent Bit, OpenSearch and Valkey' \
	  'make smoke                 Check a running development Pipeline API' \
	  'make lint                  Run Ruff lint checks' \
	  'make format                Format Pipeline Python with Ruff' \
	  'make api-docs              Preview docs/swagger at http://127.0.0.1:8080' \
	  'make api-docs-check        Check Swagger references, fields and request contracts' \
	  'make wheels                Build runtime and development package wheelhouse in offline-wheel/' \
	  'make package               Build wheels, then dist/pipeline-<arch>.tar.gz'

setup:
	AIVAX_VENV="$(VENV)" INSTALL_DEV=1 PYTHON_BIN="$(PYTHON)" UV_BIN="$(UV)" ./scripts/bootstrap_venv.sh

setup-prod: setup

setup-dev:
	AIVAX_VENV="$(DEV_VENV)" INSTALL_DEV=1 PYTHON_BIN="$(PYTHON)" UV_BIN="$(UV)" ./scripts/bootstrap_venv.sh

setup-dev-offline:
	@test -d "$(WHEEL_DIR)" || { echo 'Wheel directory not found. Copy the make wheel output or set WHEEL_DIR.' >&2; exit 1; }
	$(UV) --offline --no-cache venv --python "$(PYTHON)" --allow-existing "$(DEV_VENV)"
	$(UV) --offline --no-cache pip install --python "$(PYTHON_DEV_VENV)" --no-index --find-links "$(WHEEL_DIR)" --only-binary :all: --requirement requirements.txt --requirement requirements-dev.txt

setup-offline:
	@test -d "$(WHEEL_DIR)" || { echo 'Wheel directory not found. Copy the make wheel output or set WHEEL_DIR.' >&2; exit 1; }
	$(UV) --offline --no-cache venv --python "$(PYTHON)" --allow-existing "$(VENV)"
	$(UV) --offline --no-cache pip install --python "$(PYTHON_VENV)" --no-index --find-links "$(WHEEL_DIR)" --only-binary :all: --requirement requirements.txt --requirement requirements-dev.txt


clean:
	@test "$(VENV)" = "/home1/aivax/aivax-venv" || { echo 'clean only removes the production virtual environment.' >&2; exit 1; }
	@rm -rf -- "$(VENV)"

clean-dev:
	@test "$(DEV_VENV)" = ".venv-dev" || { echo 'clean-dev only removes the development virtual environment.' >&2; exit 1; }
	@rm -rf -- "$(DEV_VENV)"
	@find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .mypy_cache -o -name .ruff_cache \) -prune -exec rm -rf -- {} +
	@find . -type f \( -name '*.pyc' -o -name '*.pyo' -o -name '.coverage' -o -name '.coverage.*' \) -delete

run: dev

dev:
	@test -x "$(PYTHON_DEV_VENV)" || { echo 'Run make setup-dev first.' >&2; exit 1; }
	@test -f .env.development || { echo '.env.development is required.' >&2; exit 1; }
	$(RUN_PREFIX) AIVAX_PIPELINE_ENV_FILE=.env.development $(PYTHON_DEV_VENV) main.py

prod:
	@test -x "$(PYTHON_VENV)" || { echo 'Run make setup first.' >&2; exit 1; }
	@test -f .env || { echo '.env is required.' >&2; exit 1; }
	$(RUN_PREFIX) AIVAX_PIPELINE_ENV_FILE=.env $(PYTHON_VENV) main.py

check:
	@test -x "$(PYTHON_VENV)" || { echo 'Run make setup first.' >&2; exit 1; }
	$(PYTHON_VENV) scripts/check_environment.py

test:
	@test -x "$(PYTHON_DEV_VENV)" || { echo 'Run make setup-dev first.' >&2; exit 1; }
	$(RUN_PREFIX) $(PYTHON_DEV_VENV) -m pytest --run-all $(TEST_ARGS)

test-unit:
	@test -x "$(PYTHON_DEV_VENV)" || { echo 'Run make setup-dev first.' >&2; exit 1; }
	$(RUN_PREFIX) $(PYTHON_DEV_VENV) -m pytest tests \
		--ignore=tests/integration \
		--ignore=tests/e2e \
		$(UNIT_TEST_ARGS)

test-integration:
	@test -x "$(PYTHON_DEV_VENV)" || { echo 'Run make setup-dev first.' >&2; exit 1; }
	$(PYTHON_DEV_VENV) -m pytest tests/integration --run-container-integration $(INTEGRATION_TEST_ARGS)

smoke:
	@test -x "$(PYTHON_DEV_VENV)" || { echo 'Run make setup-dev first.' >&2; exit 1; }
	@test -f "$(SMOKE_ENV_FILE)" || { echo '$(SMOKE_ENV_FILE) is required.' >&2; exit 1; }
	AIVAX_PIPELINE_ENV_FILE="$(SMOKE_ENV_FILE)" $(PYTHON_DEV_VENV) scripts/smoke_pipeline.py

lint:
	@test -x "$(PYTHON_DEV_VENV)" || { echo 'Run make setup-dev first.' >&2; exit 1; }
	$(PYTHON_DEV_VENV) -m ruff check main.py src

format:
	@test -x "$(PYTHON_DEV_VENV)" || { echo 'Run make setup-dev first.' >&2; exit 1; }
	$(PYTHON_DEV_VENV) -m ruff format main.py src tests scripts

api-docs:
	$(PYTHON_DEV_VENV) -m http.server 8080 --bind 127.0.0.1 --directory docs/swagger

api-docs-check:
	$(PYTHON_DEV_VENV) scripts/check_openapi.py

wheels:
	@test -x "$(PYTHON_VENV)" || { echo 'Run make setup first.' >&2; exit 1; }
	mkdir -p "$(WHEEL_DIR)"
	$(PYTHON_VENV) -m pip wheel --requirement requirements.txt --requirement requirements-dev.txt --wheel-dir "$(WHEEL_DIR)"

wheel: wheels

package: 
	OUT="$(OUT)" ./scripts/pack.sh
