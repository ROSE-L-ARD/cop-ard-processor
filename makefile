SOURCES := $(CURDIR)/cop_ard_processor
TESTS := $(CURDIR)/tests
NOTEBOOKS := $(CURDIR)/notebooks
DOCS := $(CURDIR)/docs

GRID_FILE ?= us_nga_egm08_25.tif

# Code block visibility in the compiled Typst PDF: make pdf CODE=hide|show
CODE := hide
CODEFLAG = $(if $(filter hide,$(CODE)),--hide-code,)
.PHONY: all install proj format lint security complexity typecheck check html pdf prep restore check-secrets protect-secrets

all: format check

install:
	uv sync
	$(MAKE) proj

proj:
	@echo "Ensuring geoid grid '$(GRID_FILE)' is installed..."
	@uv run python -c "from cop_ard_app.utils import download_geoid_grid; download_geoid_grid(grid_name='$(GRID_FILE)')"
	@echo "Grid verification completed."

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

check-secrets:
	@echo "==> Scanning entire Git history for leaked secrets..."
	gitleaks detect --source . --verbose --redact

protect-secrets:
	@echo "==> Scanning uncommitted changes for secrets..."
	gitleaks protect --staged --verbose --redact

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
