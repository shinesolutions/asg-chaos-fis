version ?= 0.1.0-pre.0

ci: clean deps lint test package

clean:
	rm -rf stage

deps:
	pip install -r requirements-dev.txt

lint:
	pylint lambda/*.py test/*.py

test:
	pytest --log-cli-level=info

# Only the Lambda source is packaged; boto3 is provided by the python3.12 Lambda runtime.
package:
	mkdir -p stage
	zip --junk-paths stage/aem-chaos-engine-$(version).zip lambda/aem_chaos_engine.py

release:
	rtk release

.PHONY: ci clean deps lint test package release
