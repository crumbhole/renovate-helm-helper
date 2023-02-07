.PHONY: docker lint
IMAGE:=renovate-helper

lint: renovate_helper
	pylint $<

docker: Dockerfile renovate_helper
	docker build -t ${IMAGE} .
