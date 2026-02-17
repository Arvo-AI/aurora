#!/bin/bash
# Create Aurora Demo Package for VM Deployment

set -e

echo "📦 Creating Aurora Demo Package..."
echo ""

# Check if we're in the right directory
if [ ! -f "docker-compose.prod-local.yml" ]; then
    echo "❌ Error: Must run from aurora root directory"
    exit 1
fi

# Check if .env exists
if [ ! -f ".env" ]; then
    echo "❌ Error: .env file not found"
    echo "   Create .env with your configuration first"
    exit 1
fi

OUTPUT_FILE="aurora-demo.zip"

echo "Packaging files..."
echo ""

# Create complete package (includes source code for building)
zip -r $OUTPUT_FILE \
  docker-compose.prod-local.yml \
  .env \
  Makefile \
  demo-data/ \
  config/ \
  scripts/deploy-on-vm.sh \
  scripts/vault-init-persistent.sh \
  server/ \
  client/ \
  -x "*.git*" \
  -x "*__pycache__*" \
  -x "*.pyc" \
  -x "*node_modules*" \
  -x "*/node_modules/*" \
  -x "*/.next/*" \
  -x "*/build/*" \
  -x "*/dist/*" \
  -q

echo "✅ Package created: $OUTPUT_FILE"
echo ""

# Show size
SIZE=$(du -h $OUTPUT_FILE | cut -f1)
echo "📊 Package size: $SIZE"
echo ""

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Next Steps:"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "1. Upload to VM:"
echo "   gcloud compute scp $OUTPUT_FILE aurora-demo-vm:/opt/aurora-demo/ --zone=us-central1-a"
echo ""
echo "2. SSH into VM and deploy:"
echo "   gcloud compute ssh aurora-demo-vm --zone=us-central1-a"
echo "   cd /opt/aurora-demo"
echo "   unzip $OUTPUT_FILE"
echo "   chmod +x scripts/deploy-on-vm.sh"
echo "   ./scripts/deploy-on-vm.sh"
echo ""
echo "3. Access demo at:"
echo "   http://34.57.27.90:3000"
echo ""
