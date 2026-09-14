SOURCES := $(CURDIR)/cop_ard_processor
TESTS := $(CURDIR)/tests
NOTEBOOKS := $(CURDIR)/notebooks

# Deferred evaluation so Python runs only when calling `make proj`
PROJ_DATA_DIR = $(shell uv run python -c "import pyproj; print(pyproj.datadir.get_data_dir())")
GRID_URL := https://cdn.proj.org/us_nga_egm08_25.tif
GRID_FILE := us_nga_egm08_25.tif

.PHONY: all proj format lint security complexity typecheck check

all: format check

proj:
	@echo "PyPROJ target directory: $(PROJ_DATA_DIR)"
	@mkdir -p "$(PROJ_DATA_DIR)"
	@if [ ! -f "$(PROJ_DATA_DIR)/$(GRID_FILE)" ]; then \
		echo "Downloading $(GRID_FILE)..."; \
		curl -sSL "$(GRID_URL)" -o "$(PROJ_DATA_DIR)/$(GRID_FILE)"; \
	else \
		echo "$(GRID_FILE) already present."; \
	fi
	@echo "Creating symlink for EPSG grid alias..."
	@ln -sf "$(GRID_FILE)" "$(PROJ_DATA_DIR)/$(GRID_ALIAS)"
	@echo "Installation and configuration completed successfully!"

format:
	uv run --extra formatter --frozen ruff format $(SOURCES) $(TESTS) $(NOTEBOOKS)
	uv run --extra formatter --frozen ruff check --select I --fix $(SOURCES) $(TESTS)

lint:
	uv run --extra linter --frozen ruff check $(SOURCES) $(TESTS)

security:
	uv run --extra security --frozen python -m bandit -c $(CURDIR)/bandit.yml -r $(SOURCES)

complexity:
	uv run --extra complexity --frozen python -m xenon --max-average b --max-modules c --max-absolute d $(SOURCES)

typecheck:
	uv run --extra typing --frozen python -m mypy $(SOURCES) --allow-redefinition

check: lint security complexity typecheck