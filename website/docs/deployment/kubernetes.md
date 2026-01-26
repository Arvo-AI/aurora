---
sidebar_position: 2
---

# Kubernetes Deployment

Deploy Aurora to a Kubernetes cluster for production workloads.

:::info Work in Progress
Official Helm charts for Aurora are under development. This guide covers manual Kubernetes deployment using the existing Docker images.
:::

## Overview

Aurora can be deployed to Kubernetes by:
1. Using the Docker images from Docker Compose
2. Creating Kubernetes manifests for each service
3. Configuring external dependencies (PostgreSQL, Redis, etc.)

## Prerequisites

- Kubernetes cluster 1.24+
- kubectl configured
- Helm 3.x (for dependencies)
- Container registry access
- Persistent volume provisioner

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Ingress Controller                        │
│              (nginx-ingress / traefik)                       │
└─────────────────────────────────────────────────────────────┘
                            │
         ┌──────────────────┼──────────────────┐
         ▼                  ▼                  ▼
   ┌───────────┐      ┌───────────┐      ┌───────────┐
   │ Frontend  │      │    API    │      │  Chatbot  │
   │ Deployment│      │Deployment │      │Deployment │
   │  (3000)   │      │  (5080)   │      │  (5006)   │
   └───────────┘      └───────────┘      └───────────┘
                            │
                            ▼
                    ┌───────────────┐
                    │ Celery Worker │
                    │  Deployment   │
                    └───────────────┘
                            │
    ┌───────────────────────┼───────────────────────┐
    │           │           │           │           │
    ▼           ▼           ▼           ▼           ▼
┌───────┐ ┌─────────┐ ┌─────────┐ ┌───────┐ ┌───────┐
│  PG   │ │  Redis  │ │Weaviate │ │ Vault │ │  S3   │
│(Cloud)│ │ (Cloud) │ │  (Helm) │ │(Helm) │ │(Cloud)│
└───────┘ └─────────┘ └─────────┘ └───────┘ └───────┘
```

## Recommended Setup

### External Dependencies

For production, use managed services:

| Service | Recommended Options |
|---------|---------------------|
| PostgreSQL | AWS RDS, GCP Cloud SQL, Azure Database |
| Redis | AWS ElastiCache, GCP Memorystore, Azure Cache |
| Object Storage | AWS S3, Cloudflare R2, GCP GCS |
| Vault | HashiCorp Cloud Platform, self-managed |

### Install Weaviate

```bash
helm repo add weaviate https://weaviate.github.io/weaviate-helm
helm install weaviate weaviate/weaviate \
  --namespace aurora \
  --create-namespace \
  --set replicas=1 \
  --set storage.size=10Gi
```

### Install Vault (Optional)

```bash
helm repo add hashicorp https://helm.releases.hashicorp.com
helm install vault hashicorp/vault \
  --namespace aurora \
  --set server.dev.enabled=false \
  --set server.ha.enabled=false
```

## Kubernetes Manifests

### Namespace

```yaml
# namespace.yaml
apiVersion: v1
kind: Namespace
metadata:
  name: aurora
```

### ConfigMap

```yaml
# configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: aurora-config
  namespace: aurora
data:
  AURORA_ENV: "production"
  POSTGRES_HOST: "your-postgres-host"
  POSTGRES_PORT: "5432"
  POSTGRES_DB: "aurora_db"
  REDIS_URL: "redis://your-redis-host:6379/0"
  WEAVIATE_HOST: "weaviate"
  WEAVIATE_PORT: "8080"
  FRONTEND_URL: "https://aurora.yourdomain.com"
  BACKEND_URL: "http://aurora-api:5080"
  NEXT_PUBLIC_BACKEND_URL: "https://api.aurora.yourdomain.com"
  NEXT_PUBLIC_WEBSOCKET_URL: "wss://ws.aurora.yourdomain.com"
```

### Secrets

```yaml
# secrets.yaml
apiVersion: v1
kind: Secret
metadata:
  name: aurora-secrets
  namespace: aurora
type: Opaque
stringData:
  POSTGRES_USER: "aurora"
  POSTGRES_PASSWORD: "your-secure-password"
  FLASK_SECRET_KEY: "your-64-char-secret"
  AUTH_SECRET: "your-64-char-secret"
  OPENROUTER_API_KEY: "sk-or-v1-your-key"
  VAULT_TOKEN: "hvs.your-token"
```

### API Deployment

```yaml
# api-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: aurora-api
  namespace: aurora
spec:
  replicas: 2
  selector:
    matchLabels:
      app: aurora-api
  template:
    metadata:
      labels:
        app: aurora-api
    spec:
      containers:
      - name: api
        image: your-registry/aurora-server:latest
        ports:
        - containerPort: 5080
        envFrom:
        - configMapRef:
            name: aurora-config
        - secretRef:
            name: aurora-secrets
        resources:
          requests:
            memory: "2Gi"
            cpu: "1"
          limits:
            memory: "4Gi"
            cpu: "2"
        livenessProbe:
          httpGet:
            path: /health
            port: 5080
          initialDelaySeconds: 30
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /health
            port: 5080
          initialDelaySeconds: 5
          periodSeconds: 5
---
apiVersion: v1
kind: Service
metadata:
  name: aurora-api
  namespace: aurora
spec:
  selector:
    app: aurora-api
  ports:
  - port: 5080
    targetPort: 5080
```

### Frontend Deployment

```yaml
# frontend-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: aurora-frontend
  namespace: aurora
spec:
  replicas: 2
  selector:
    matchLabels:
      app: aurora-frontend
  template:
    metadata:
      labels:
        app: aurora-frontend
    spec:
      containers:
      - name: frontend
        image: your-registry/aurora-frontend:latest
        ports:
        - containerPort: 3000
        envFrom:
        - configMapRef:
            name: aurora-config
        - secretRef:
            name: aurora-secrets
        resources:
          requests:
            memory: "512Mi"
            cpu: "250m"
          limits:
            memory: "1Gi"
            cpu: "500m"
---
apiVersion: v1
kind: Service
metadata:
  name: aurora-frontend
  namespace: aurora
spec:
  selector:
    app: aurora-frontend
  ports:
  - port: 3000
    targetPort: 3000
```

### Celery Worker Deployment

```yaml
# celery-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: aurora-celery
  namespace: aurora
spec:
  replicas: 2
  selector:
    matchLabels:
      app: aurora-celery
  template:
    metadata:
      labels:
        app: aurora-celery
    spec:
      containers:
      - name: celery
        image: your-registry/aurora-server:latest
        command: ["celery", "-A", "tasks", "worker", "--loglevel=info"]
        envFrom:
        - configMapRef:
            name: aurora-config
        - secretRef:
            name: aurora-secrets
        resources:
          requests:
            memory: "2Gi"
            cpu: "1"
          limits:
            memory: "4Gi"
            cpu: "2"
```

### Chatbot Deployment

```yaml
# chatbot-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: aurora-chatbot
  namespace: aurora
spec:
  replicas: 2
  selector:
    matchLabels:
      app: aurora-chatbot
  template:
    metadata:
      labels:
        app: aurora-chatbot
    spec:
      containers:
      - name: chatbot
        image: your-registry/aurora-chatbot:latest
        ports:
        - containerPort: 5006
        envFrom:
        - configMapRef:
            name: aurora-config
        - secretRef:
            name: aurora-secrets
        resources:
          requests:
            memory: "1Gi"
            cpu: "500m"
          limits:
            memory: "2Gi"
            cpu: "1"
---
apiVersion: v1
kind: Service
metadata:
  name: aurora-chatbot
  namespace: aurora
spec:
  selector:
    app: aurora-chatbot
  ports:
  - port: 5006
    targetPort: 5006
```

### Ingress

```yaml
# ingress.yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: aurora-ingress
  namespace: aurora
  annotations:
    kubernetes.io/ingress.class: nginx
    cert-manager.io/cluster-issuer: letsencrypt-prod
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
spec:
  tls:
  - hosts:
    - aurora.yourdomain.com
    - api.aurora.yourdomain.com
    - ws.aurora.yourdomain.com
    secretName: aurora-tls
  rules:
  - host: aurora.yourdomain.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: aurora-frontend
            port:
              number: 3000
  - host: api.aurora.yourdomain.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: aurora-api
            port:
              number: 5080
  - host: ws.aurora.yourdomain.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: aurora-chatbot
            port:
              number: 5006
```

## Deployment Steps

### 1. Build and Push Images

```bash
# Build images
docker build -t your-registry/aurora-server:latest ./server
docker build -t your-registry/aurora-frontend:latest ./client
docker build -t your-registry/aurora-chatbot:latest ./server -f ./server/Dockerfile.chatbot

# Push to registry
docker push your-registry/aurora-server:latest
docker push your-registry/aurora-frontend:latest
docker push your-registry/aurora-chatbot:latest
```

### 2. Apply Manifests

```bash
kubectl apply -f namespace.yaml
kubectl apply -f configmap.yaml
kubectl apply -f secrets.yaml
kubectl apply -f api-deployment.yaml
kubectl apply -f frontend-deployment.yaml
kubectl apply -f celery-deployment.yaml
kubectl apply -f chatbot-deployment.yaml
kubectl apply -f ingress.yaml
```

### 3. Verify Deployment

```bash
# Check pods
kubectl get pods -n aurora

# Check services
kubectl get svc -n aurora

# Check ingress
kubectl get ingress -n aurora

# View logs
kubectl logs -n aurora -l app=aurora-api --tail=50
```

## Scaling

### Horizontal Pod Autoscaler

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: aurora-api-hpa
  namespace: aurora
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: aurora-api
  minReplicas: 2
  maxReplicas: 10
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
```

## Monitoring

### Prometheus ServiceMonitor

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: aurora-api
  namespace: aurora
spec:
  selector:
    matchLabels:
      app: aurora-api
  endpoints:
  - port: http
    path: /metrics
```

## Troubleshooting

### Pod Not Starting

```bash
kubectl describe pod -n aurora <pod-name>
kubectl logs -n aurora <pod-name>
```

### Database Connection Issues

```bash
# Test from pod
kubectl exec -n aurora -it <api-pod> -- psql -h $POSTGRES_HOST -U $POSTGRES_USER -d $POSTGRES_DB
```

### Ingress Not Working

```bash
kubectl describe ingress -n aurora aurora-ingress
kubectl logs -n ingress-nginx -l app.kubernetes.io/component=controller
```
