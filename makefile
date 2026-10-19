SOURCES := $(CURDIR)/cop_ard_processor
TESTS := $(CURDIR)/tests
NOTEBOOKS := $(CURDIR)/notebooks
DOCS := $(CURDIR)/docs

# Deferred evaluation so Python runs only when calling `make proj`
PROJ_DATA_DIR = $(shell uv run python -c "import pyproj; print(pyproj.datadir.get_data_dir())")
GRID_URL := https://cdn.proj.org/us_nga_egm08_25.tif
GRID_FILE := us_nga_egm08_25.tif
GRID_ALIAS := Und_min1x1_egm2008_isw=82_WGS84_TideFree

# Code block visibility in the compiled Typst PDF: make pdf CODE=hide|show
CODE := hide
CODEFLAG = $(if $(filter hide,$(CODE)),--hide-code,)
.PHONY: all install proj format lint security complexity typecheck check html pdf prep restore

all: format check

install: proj
	uv sync

proj:
	@echo "PyPROJ target directory: $(PROJ_DATA_DIR)"
	@mkdir -p "$(PROJ_DATA_DIR)"
	@if [ ! -f "$(PROJ_DATA_DIR)/$(GRID_FILE)" ]; then \
		echo "Downloading $(GRID_FILE)..."; \
		curl -fsSL "$(GRID_URL)" -o "$(PROJ_DATA_DIR)/$(GRID_FILE)"; \
	else \
		echo "$(GRID_FILE) already present."; \
	fi
	@echo "Creating symlink for EPSG grid alias..."
	@cd "$(PROJ_DATA_DIR)" && ln -sf "$(GRID_FILE)" "$(GRID_ALIAS)"
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


cslc-qualification-notebook:
	uv venv --python 3.12 .venv-qual --clear
	uv pip install --python .venv-qual -r notebooks/scripts/cslc_qualification.txt
	. .venv-qual/bin/activate
	
html:
	@echo "Rendering Quarto reports..."
	cd $(NOTEBOOKS) && quarto render --to html

pdf: prep
	@printf "\n=== Starting PDF compilation with Typst ===\n"
	@(cd $(NOTEBOOKS) && quarto render --to typst) || ( $(MAKE) -C $(CURDIR) restore && exit 1 )
	@$(MAKE) restore
	@echo "=== Compilation completed ==="

prep:
	@printf "\n=== Preparing the environment ===\n"
	@echo "0. Clearing stale render cache..."
	@rm -rf $(NOTEBOOKS)/.quarto
	@echo "1. Creating backups..."
	@for nb in $(NOTEBOOKS)/*.ipynb; do \
		[ -f "$$nb" ] || continue; \
		cp "$$nb" "$$nb.bak"; \
	done
	@echo "3. Converting HTML tables for Typst PDF output..."
	@uv run python scripts/convert_tables.py $(CODEFLAG) $(NOTEBOOKS)/*.ipynb

restore:
	@printf "\n=== Restoring original state ===\n"
	@# 1. Restore from backup if .bak files exist
	@find $(NOTEBOOKS) -name "*.ipynb.bak" -exec sh -c 'mv "$$0" "$${0%.bak}"' {} \;
