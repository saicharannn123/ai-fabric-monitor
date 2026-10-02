.PHONY: demo demo-down lab-up lab-down verify traffic test lint

demo:            ## monitoring stack + simulated fabric (no containerlab needed)
	cd monitoring && docker compose up -d --build
	@echo "Grafana: http://localhost:3000  Prometheus: http://localhost:9090  Alertmanager: http://localhost:9093"

demo-down:
	cd monitoring && docker compose down

lab-up:          ## real FRR fabric in containerlab + monitoring in docker mode
	sudo containerlab deploy -t topology/fabric.clab.yml --reconfigure
	cd monitoring && EXPORTER_MODE=docker docker compose up -d --build
	@sleep 20 && ./scripts/verify-lab.sh

lab-down:
	cd monitoring && docker compose down
	sudo containerlab destroy -t topology/fabric.clab.yml --cleanup

verify:
	./scripts/verify-lab.sh

traffic:
	./scripts/traffic.sh 120 8

test:            ## unit tests + alert rule tests
	cd exporter && python -m pytest -q
	promtool check rules monitoring/prometheus/rules/*.yml
	cd monitoring/prometheus/tests && promtool test rules alerts_test.yml

lint:
	cd exporter && ruff check . && ruff format --check .
