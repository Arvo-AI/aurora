#!/bin/bash
# GCP VM Setup for Aurora Demo
# Creates a VM with Docker pre-installed and ready to run make prod-local

set -e

PROJECT_ID=$(gcloud config get-value project)
REGION="us-central1"
ZONE="us-central1-a"
VM_NAME="aurora-demo-vm"
MACHINE_TYPE="e2-standard-4"  # 4 vCPUs, 16GB RAM
BOOT_DISK_SIZE="50GB"

echo "🚀 Creating Aurora Demo VM on GCP"
echo "Project: $PROJECT_ID"
echo "Region: $REGION"
echo "VM Name: $VM_NAME"
echo ""

# Create startup script that installs Docker and docker-compose
cat > /tmp/startup-script.sh << 'STARTUP_EOF'
#!/bin/bash
# VM Startup Script - Installs Docker

set -e

echo "Installing Docker..."
curl -fsSL https://get.docker.com -o get-docker.sh
sh get-docker.sh

# Add default user to docker group
usermod -aG docker $(logname) || true

# Install docker-compose
echo "Installing docker-compose..."
curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
chmod +x /usr/local/bin/docker-compose

# Install make
apt-get update
apt-get install -y make unzip

# Create deployment directory
mkdir -p /opt/aurora-demo
chown -R $(logname):$(logname) /opt/aurora-demo

echo "✅ VM Setup Complete!"
echo "Docker version: $(docker --version)"
echo "Docker Compose version: $(docker-compose --version)"
STARTUP_EOF

echo "📦 Creating VM with Docker pre-installed..."

gcloud compute instances create $VM_NAME \
  --project=$PROJECT_ID \
  --zone=$ZONE \
  --machine-type=$MACHINE_TYPE \
  --boot-disk-size=$BOOT_DISK_SIZE \
  --boot-disk-type=pd-balanced \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --metadata-from-file startup-script=/tmp/startup-script.sh \
  --tags=aurora-demo \
  --scopes=https://www.googleapis.com/auth/cloud-platform

echo ""
echo "🔒 Setting up firewall rules..."

# Create firewall rule to allow port 3000 (frontend only)
gcloud compute firewall-rules create aurora-demo-allow-3000 \
  --project=$PROJECT_ID \
  --direction=INGRESS \
  --priority=1000 \
  --network=default \
  --action=ALLOW \
  --rules=tcp:3000 \
  --source-ranges=0.0.0.0/0 \
  --target-tags=aurora-demo \
  --description="Allow port 3000 for Aurora frontend" \
  2>/dev/null || echo "Firewall rule already exists"

# Get external IP
EXTERNAL_IP=$(gcloud compute instances describe $VM_NAME \
  --zone=$ZONE \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)')

echo ""
echo "✅ VM Created Successfully!"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "VM Details:"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Name: $VM_NAME"
echo "Zone: $ZONE"
echo "External IP: $EXTERNAL_IP"
echo "SSH: gcloud compute ssh $VM_NAME --zone=$ZONE"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Next Steps:"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "1. Wait 2-3 minutes for Docker to install"
echo ""
echo "2. SSH into VM:"
echo "   gcloud compute ssh $VM_NAME --zone=$ZONE"
echo ""
echo "3. Upload your demo package:"
echo "   gcloud compute scp aurora-demo.zip $VM_NAME:/opt/aurora-demo/ --zone=$ZONE"
echo ""
echo "4. In the VM, run:"
echo "   cd /opt/aurora-demo"
echo "   unzip aurora-demo.zip"
echo "   make prod-local"
echo ""
echo "5. Access demo at:"
echo "   http://$EXTERNAL_IP:3000"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "💰 Cost: ~\$50-70/month (stop when not in use to save costs)"
echo ""
echo "To stop VM:  gcloud compute instances stop $VM_NAME --zone=$ZONE"
echo "To start VM: gcloud compute instances start $VM_NAME --zone=$ZONE"
echo "To delete VM: gcloud compute instances delete $VM_NAME --zone=$ZONE"
echo ""

# Clean up temp file
rm /tmp/startup-script.sh
