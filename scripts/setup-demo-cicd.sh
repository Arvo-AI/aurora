#!/bin/bash
# Setup script for Demo VM CI/CD

set -e

echo "================================================"
echo "🚀 Aurora Demo VM CI/CD Setup"
echo "================================================"
echo ""

# Check if key file exists
KEY_FILE="$HOME/aurora-demo-deployer-key.json"
if [ ! -f "$KEY_FILE" ]; then
    echo "❌ Service account key not found at: $KEY_FILE"
    echo ""
    echo "The key should have been created. Please check if it exists."
    exit 1
fi

echo "✅ Service account key found"
echo ""
echo "================================================"
echo "📋 Next Steps - Add Key to GitHub Secrets"
echo "================================================"
echo ""
echo "1. Copy the service account key to clipboard:"
echo ""
if command -v pbcopy &> /dev/null; then
    cat "$KEY_FILE" | pbcopy
    echo "   ✅ Key copied to clipboard!"
else
    echo "   Run: cat $KEY_FILE"
    echo "   Then copy the output manually"
fi
echo ""
echo "2. Go to your GitHub repository:"
echo "   https://github.com/Arvo-AI/aurora/settings/secrets/actions"
echo ""
echo "3. Click 'New repository secret'"
echo ""
echo "4. Name: GCP_SA_KEY"
echo ""
echo "5. Value: Paste the key from clipboard"
echo ""
echo "6. Click 'Add secret'"
echo ""
echo "================================================"
echo "🔐 Security Note"
echo "================================================"
echo ""
echo "After adding to GitHub, you should delete the local key file:"
echo "   rm $KEY_FILE"
echo ""
echo "The key is now stored securely in GitHub Secrets."
echo ""
echo "================================================"
echo "✅ Setup Complete!"
echo "================================================"
echo ""
echo "Once the GitHub secret is added, you can deploy by:"
echo ""
echo "  1. Push to demo branch:"
echo "     git checkout demo"
echo "     git merge main"
echo "     git push origin demo"
echo ""
echo "  2. Or trigger manually in GitHub Actions:"
echo "     https://github.com/Arvo-AI/aurora/actions/workflows/deploy-demo-vms.yml"
echo ""
echo "================================================"
echo "📚 Documentation"
echo "================================================"
echo ""
echo "Full setup guide: docs/deployment/demo-vm-cicd.md"
echo ""
