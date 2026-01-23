# Export all GCP related blueprints
from .auth import gcp_auth_bp as bp  # Main auth blueprint for backward compatibility

# Additional GCP blueprints can be imported directly
from .projects import gcp_projects_bp
from .billing import gcp_billing_bp

__all__ = ['bp', 'gcp_projects_bp', 'gcp_billing_bp']
