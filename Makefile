version ?= 0.9.0-pre.0

ci: clean deps lint test package

clean:
	rm -rf stage

define python_venv
	. .venv/bin/activate && $(1)
endef

deps:
	python3 -m venv .venv
	$(call python_venv,python3 -m pip install -r requirements.txt)

deps-upgrade:
	python3 -m venv .venv
	$(call python_venv,python3 -m pip install -r requirements-dev.txt)
	$(call python_venv,pip-compile --upgrade)

rmdeps:
	rm -rf .venv

lint:
	$(call python_venv,pylint lambda/*.py test/*.py)
	$(call python_venv,yaml-lint .github/workflows/*.yaml)
	$(call python_venv,actionlint -shellcheck= .github/workflows/*.yaml)
	markdownlint-cli2 README.md docs/*.md

test:
	$(call python_venv,pytest --log-cli-level=info)

# Only the Lambda source is packaged; boto3 is provided by the python3.12 Lambda runtime.
# The .sha256 file can be checked with: sha256sum -c asg-chaos-fis-<version>.zip.sha256
package:
	mkdir -p stage
	zip --junk-paths stage/asg-chaos-fis-$(version).zip lambda/asg_chaos_fis.py
	cd stage && shasum -a 256 asg-chaos-fis-$(version).zip > asg-chaos-fis-$(version).zip.sha256

release:
	rtk release

.PHONY: ci clean deps dpes-upgrade rmdeps lint test package release
