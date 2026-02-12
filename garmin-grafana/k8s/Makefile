.PHONY: test-minikube clean-minikube get-grafana-password

test-minikube:
	@command -v minikube >/dev/null || { echo "❌ Install minikube: https://minikube.sigs.k8s.io/docs/start/"; exit 1; }
	@command -v helm >/dev/null || { echo "❌ Install helm: https://helm.sh/docs/intro/install/"; exit 1; }
	@echo "✅ Dependencies OK"
	@echo "🚀 Starting minikube..."
	minikube delete || true
	minikube start
	@echo "⏳ Waiting for external DNS resolution..."
	@until kubectl run dns-test --rm -i --image=curlimages/curl --restart=Never -- curl -s -I https://grafana.com > /dev/null 2>&1; do \
		echo "DNS not ready, waiting..."; \
		sleep 5; \
	done
	@echo "✅ External DNS ready!"
	@echo "🧹 Cleaning charts and building dependencies..."
	@[ -f Chart.lock ] && rm Chart.lock; [ -d charts ] && rm -rf charts || true
	helm dependency build
	@echo "🚢 Deploying..."
	helm upgrade -i garmin-grafana . \
		--values values.yaml \
		--create-namespace \
		-n garmin-grafana
	@echo "⏳ Waiting for Grafana to be ready..."
	kubectl wait --for=condition=ready pod -l app.kubernetes.io/name=grafana -n garmin-grafana --timeout=300s
	@echo "🔑 Getting password..."
	@make get-grafana-password
	@echo "🌐 Opening dashboard..."
	minikube service garmin-grafana -n garmin-grafana

get-grafana-password:
	@echo "Grafana admin password:"
	@kubectl get secret garmin-grafana \
		-n garmin-grafana \
		-o jsonpath="{.data.admin-password}" | base64 --decode
	@echo

clean-minikube:
	@echo "🧹 Cleaning up..."
	minikube delete
	@[ -f Chart.lock ] && rm Chart.lock; [ -d charts ] && rm -rf charts || true
