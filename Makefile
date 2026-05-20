.PHONY: clean
clean: clean-build clean-pyc clean-test clean-docs

.PHONY: clean-build
clean-build:
	rm -fr build/
	rm -fr dist/
	rm -fr .eggs/
	rm -fr pip-wheel-metadata
	find . -name '*.egg-info' -exec rm -fr {} +
	find . -name '*.egg' -exec rm -fr {} +
	find . -name '*.svn' -exec rm -fr {} +
	find . -type d -name __pycache__ -exec rm -rv {} +
	rm -fr .idea .history
	rm -fr venv

.PHONY: clean-docs
clean-docs:
	rm -fr site/

.PHONY: clean-pyc
clean-pyc:
	find . -name '*.pyc' -exec rm -f {} +
	find . -name '*.pyo' -exec rm -f {} +
	find . -name '*~' -exec rm -f {} +
	find . -name '__pycache__' -exec rm -fr {} +
	find . -name '.DS_Store' -exec rm -fr {} +

.PHONY: clean-test
clean-test: clean-cache
	rm -fr .tox/
	rm -f .coverage
	find . -name ".coverage*" -not -name ".coveragerc" -exec rm -fr "{}" \;
	rm -fr coverage.xml
	rm -fr htmlcov/
	find . -name 'log.txt' -exec rm -fr {} +
	find . -name 'log.*.txt' -exec rm -fr {} +
	rm -rf leak_report

.PHONY: clean-cache
clean-cache:
	find . -type d -name .hypothesis -prune -exec rm -rf {} \;
	rm -fr .pytest_cache
	rm -fr .mypy_cache/

.PHONY: format
format:
	tomte format-code

.PHONY: code-checks
code-checks:
	tomte check-code

.PHONY: security
security:
	tomte check-security
	gitleaks detect --report-format json --report-path leak_report

.PHONY: common-checks-1
common-checks-1:
	tomte check-copyright --author valory
	tomte check-doc-links
	tomte tox -p -e check-hash -e check-packages -e check-doc-hashes

.PHONY: common-checks-2
common-checks-2:
	tomte tox -e check-abci-docstrings
	tomte tox -e check-abciapp-specs
	tomte tox -e check-dependencies
	tomte tox -e check-handlers

.PHONY: all-checks
all-checks: format code-checks security generators common-checks-1 common-checks-2

.PHONY: push-packages
push-packages:
	make clean && \
	autonomy push-all

.PHONY: abci-docstrings
abci-docstrings:
	tomte tox -e abci-docstrings

.PHONY: generators
generators: abci-docstrings
	tomte format-copyright --author valory
	autonomy packages lock
