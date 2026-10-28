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
# The .sha256 file can be checked with: sha256sum -c asg-chaos-fis-<version>.zip.sha256
package:
	mkdir -p stage
	zip --junk-paths stage/asg-chaos-fis-$(version).zip lambda/asg_chaos_fis.py
	cd stage && shasum -a 256 asg-chaos-fis-$(version).zip > asg-chaos-fis-$(version).zip.sha256

release:
	rtk release

.PHONY: ci clean deps lint test package release
