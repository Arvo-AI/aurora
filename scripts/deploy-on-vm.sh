#!/bin/bash
# Deploy Aurora Demo on VM
# Run this inside the VM after uploading the zip

set -e

echo "🚀 Aurora Demo - VM Deployment Script"
echo ""

# Check if Docker is running
if ! docker info > /dev/null 2>&1; then
    echo "⏳ Docker is not ready yet. Starting Docker..."
    sudo systemctl start docker
    sleep 5
fi

# Check if we're in the right directory
if [ ! -f "docker-compose.prod-local.yml" ]; then
    echo "❌ Error: docker-compose.prod-local.yml not found"
    echo "   Make sure you're in the /opt/aurora-demo directory"
    echo "   and have extracted the zip file"
    exit 1
fi

echo "✅ Docker is running"
echo "✅ Found docker-compose.prod-local.yml"
echo ""

# Check if .env exists
if [ ! -f ".env" ]; then
    echo "⚠️  No .env file found"
    echo "   Please make sure .env is included in your zip"
    exit 1
fi

echo "✅ Found .env file"
echo ""
echo "📦 Starting Aurora Demo with make prod-local..."
echo ""

# Run make prod-local
make prod-local

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ Aurora Demo is starting!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "⏳ Waiting for services to be ready (this takes 2-3 minutes)..."
echo ""

# Wait for frontend to be ready
for i in {1..60}; do
    if curl -s http://localhost:3000 > /dev/null 2>&1; then
        echo "✅ Frontend is ready!"
        break
    fi
    echo -n "."
    sleep 5
done

echo ""
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🎉 Aurora Demo is LIVE!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Get external IP
EXTERNAL_IP=$(curl -s ifconfig.me)
echo "🌐 Access your demo at:"
echo "   http://$EXTERNAL_IP:3000"
echo ""
echo "👤 Demo Login:"
echo "   Email: demo@aurora-demo.local"
echo "   Password: demo"
echo ""
echo "📊 4 Demo Incidents pre-loaded and ready"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Useful commands:"
echo "  View logs:    docker compose logs -f"
echo "  Stop demo:    docker compose down"
echo "  Restart:      docker compose restart"
echo ""
