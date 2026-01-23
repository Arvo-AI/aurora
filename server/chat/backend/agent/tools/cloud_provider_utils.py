import logging
from typing import List, Optional
import re

logger = logging.getLogger(__name__)

def determine_target_provider_from_context(available_providers: List[str]) -> Optional[str]:
    """
    Determine the target provider from the current user context/prompt with comprehensive detection.
    
    This function analyzes multiple recent messages, deployment actions, provider-specific services,
    and conversation context to reliably detect which cloud provider the user intends to use.
    
    Args:
        available_providers: List of available providers (e.g., ['gcp', 'aws'])
    Returns:
        The target provider if found in context, None otherwise
    """
    try:
        # Try to get the current user context/prompt from the agent state
        from .cloud_tools import get_state_context
        state = get_state_context()

        if not state or not hasattr(state, 'messages') or not state.messages:
            logger.debug("No state or messages found for provider detection")
            return None

        # Extract user messages content (support both dict and object message types)
        user_texts: List[str] = []
        for msg in state.messages:
            if isinstance(msg, dict):
                role = msg.get('role')
                content = msg.get('content', '')
            else:
                role = getattr(msg, 'role', None)
                if role is None:
                    msg_type = getattr(msg, 'type', None)
                    if msg_type == 'human':
                        role = 'user'
                    elif msg_type == 'ai':
                        role = 'assistant'
                content = getattr(msg, 'content', '')
            
            if role == 'user':
                if isinstance(content, list):
                    content_str = " ".join(str(c) for c in content)
                else:
                    content_str = str(content)
                user_texts.append(content_str.lower())
        
        if not user_texts:
            logger.debug("No user messages found for provider detection")
            return None

        # Comprehensive provider keywords and patterns
        provider_patterns = {
            'gcp': {
                'primary_keywords': ['gcp', 'google cloud', 'google', 'gce'],
                'services': [
                    'cloud run', 'cloud functions', 'app engine', 'gke', 'kubernetes engine',
                    'compute engine', 'cloud storage', 'bigquery', 'cloud sql', 'firestore',
                    'cloud build', 'cloud scheduler', 'cloud tasks', 'pub/sub', 'cloud endpoints',
                    'cloud armor', 'cloud cdn', 'cloud dns', 'cloud iam', 'cloud kms',
                    'vertex ai', 'cloud ml', 'dataflow', 'dataproc', 'cloud spanner',
                    'cloud bigtable', 'cloud memorystore', 'cloud filestore'
                ],
                'cli_patterns': ['gcloud', 'gsutil', 'bq'],
                'deployment_patterns': [
                    'deploy to gcp', 'deploy on google', 'deploy to google cloud',
                    'create gcp', 'use gcp', 'with gcp', 'on gcp', 'in gcp'
                ],
                'high_confidence_queries': [
                    'list projects', 'show projects', 'get projects', 'projects list',
                    'list gcp projects', 'show gcp projects', 'get gcp projects',
                    'google projects', 'gcp projects', 'cloud projects'
                ]
            },
            'aws': {
                'primary_keywords': ['aws', 'amazon web services', 'amazon'],
                'services': [
                    'ec2', 'eks', 'ecs', 'fargate', 'lambda', 'elastic beanstalk',
                    's3', 'rds', 'dynamodb', 'redshift', 'elasticache', 'sqs', 'sns',
                    'cloudformation', 'cloudwatch', 'cloudtrail', 'route53', 'cloudfront',
                    'api gateway', 'cognito', 'iam', 'kms', 'secrets manager',
                    'step functions', 'eventbridge', 'kinesis', 'glue', 'athena',
                    'sagemaker', 'comprehend', 'rekognition', 'textract', 'lex'
                ],
                'cli_patterns': ['aws cli', 'aws'],
                'deployment_patterns': [
                    'deploy to aws', 'deploy on amazon', 'deploy to amazon web services',
                    'create aws', 'use aws', 'with aws', 'on aws', 'in aws'
                ],
                'high_confidence_queries': [
                    'list accounts', 'show accounts', 'get accounts', 'accounts list',
                    'list aws accounts', 'show aws accounts', 'get aws accounts',
                    'aws accounts', 'amazon accounts'
                ]
            },
            'azure': {
                'primary_keywords': ['azure', 'microsoft azure', 'microsoft'],
                'services': [
                    'aks', 'azure kubernetes service', 'azure container instances',
                    'azure functions', 'app service', 'azure sql', 'cosmos db',
                    'azure storage', 'azure blob', 'azure cache', 'service bus',
                    'azure monitor', 'application insights', 'azure ad', 'key vault',
                    'azure devops', 'azure pipelines', 'azure container registry',
                    'azure cognitive services', 'azure machine learning'
                ],
                'cli_patterns': ['az cli', 'azure cli'],
                'deployment_patterns': [
                    'deploy to azure', 'deploy on microsoft', 'deploy to microsoft azure',
                    'create azure', 'use azure', 'with azure', 'on azure', 'in azure'
                ],
                'high_confidence_queries': [
                    'list subscriptions', 'show subscriptions', 'get subscriptions', 'subscriptions list',
                    'list azure subscriptions', 'show azure subscriptions', 'get azure subscriptions',
                    'azure subscriptions', 'microsoft subscriptions', 'subscription list',
                    'list subs', 'show subs', 'get subs', 'subs list'
                ]
            },
            'ovh': {
                'primary_keywords': ['ovh', 'ovhcloud', 'ovh cloud'],
                'services': [
                    'ovh public cloud', 'ovh vps', 'ovh dedicated server', 'ovh baremetal',
                    'ovh object storage', 'ovh block storage', 'ovh managed kubernetes',
                    'ovh private cloud', 'ovh web hosting', 'ovh domain'
                ],
                'cli_patterns': ['ovhcloud', 'ovh cli'],
                'deployment_patterns': [
                    'deploy to ovh', 'deploy on ovhcloud', 'deploy to ovh cloud',
                    'create ovh', 'use ovh', 'with ovh', 'on ovh', 'in ovh'
                ],
                'high_confidence_queries': [
                    'list ovh projects', 'show ovh projects', 'get ovh projects',
                    'ovh projects', 'ovhcloud projects', 'list ovh vps', 'show ovh vps',
                    'list ovh instances', 'ovh instances', 'ovh vms'
                ]
            },
            'scaleway': {
                'primary_keywords': ['scaleway', 'scw', 'scaleway cloud'],
                'services': [
                    'scaleway instances', 'scaleway kubernetes', 'scaleway kapsule',
                    'scaleway object storage', 'scaleway block storage', 'scaleway database',
                    'scaleway serverless', 'scaleway container', 'scaleway functions',
                    'scaleway vpc', 'scaleway load balancer', 'scaleway elastic metal',
                    'scaleway registry', 'scaleway iot', 'scaleway transactional email'
                ],
                'cli_patterns': ['scw', 'scaleway cli'],
                'deployment_patterns': [
                    'deploy to scaleway', 'deploy on scaleway', 'deploy to scw',
                    'create scaleway', 'use scaleway', 'with scaleway', 'on scaleway', 'in scaleway'
                ],
                'high_confidence_queries': [
                    'list scaleway projects', 'show scaleway projects', 'get scaleway projects',
                    'scaleway projects', 'scw projects', 'list scaleway instances', 'show scaleway instances',
                    'scaleway instances', 'scaleway servers', 'scw instances'
                ]
            }
        }

        # Prioritize the most recent user message over historical context
        # This ensures explicit provider mentions in the latest prompt take precedence
        latest_message = user_texts[-1] if user_texts else ""
        recent_messages = user_texts[-3:] if len(user_texts) >= 3 else user_texts
        combined_context = " ".join(recent_messages)
        
        # Check if the latest message has explicit provider mentions
        latest_has_provider = False
        for provider in available_providers:
            if provider in provider_patterns:
                patterns = provider_patterns[provider]
                # Check primary keywords in latest message
                for keyword in patterns['primary_keywords']:
                    if keyword in latest_message:
                        latest_has_provider = True
                        break
                # Check deployment patterns in latest message
                if not latest_has_provider:
                    for pattern in patterns['deployment_patterns']:
                        if pattern in latest_message:
                            latest_has_provider = True
                            break
                # Check high-confidence queries in latest message
                if not latest_has_provider:
                    for query in patterns.get('high_confidence_queries', []):
                        if query in latest_message:
                            latest_has_provider = True
                            break
                if latest_has_provider:
                    break

        # Action-based context detection
        deployment_actions = [
            'deploy', 'create', 'provision', 'launch', 'start', 'build', 'setup',
            'configure', 'initialize', 'install', 'run', 'execute', 'generate',
            'terraform', 'infrastructure', 'cluster', 'instance', 'service',
            'application', 'app', 'container', 'vm', 'virtual machine'
        ]

        # Score providers based on multiple detection methods
        provider_scores = {}

        for provider in available_providers:
            if provider not in provider_patterns:
                continue
                
            score = 0
            patterns = provider_patterns[provider]
            
            # Method 1: Direct provider mentions with high weight
            for keyword in patterns['primary_keywords']:
                if keyword in combined_context:
                    score += 10
                    logger.debug(f"Found primary keyword '{keyword}' for {provider}")
            
            # Method 2: Provider-specific services with medium-high weight
            for service in patterns['services']:
                if service in combined_context:
                    score += 7
                    logger.debug(f"Found service '{service}' for {provider}")
            
            # Method 3: CLI patterns with medium weight
            for cli in patterns['cli_patterns']:
                if cli in combined_context:
                    score += 5
                    logger.debug(f"Found CLI pattern '{cli}' for {provider}")
            
            # Method 4: Deployment patterns with high weight
            for pattern in patterns['deployment_patterns']:
                if pattern in combined_context:
                    score += 8
                    logger.debug(f"Found deployment pattern '{pattern}' for {provider}")
            
            # Method 5: Action-based context detection
            for action in deployment_actions:
                # Look for patterns like "deploy to X", "create on Y", etc.
                for keyword in patterns['primary_keywords']:
                    action_pattern = f"{action}.*{keyword}|{keyword}.*{action}"
                    if re.search(action_pattern, combined_context):
                        score += 6
                        logger.debug(f"Found action pattern '{action}' with '{keyword}' for {provider}")
            
            # Method 6: Regional/zone hints
            regional_hints = {
                'gcp': ['us-central', 'us-east', 'us-west', 'europe-west', 'asia-southeast', 
                        'zone', 'region', 'northamerica-northeast'],
                'aws': ['us-east-1', 'us-west-2', 'eu-west-1', 'ap-southeast', 'region'],
                'azure': ['east us', 'west us', 'west europe', 'southeast asia', 'location']
            }
            
            if provider in regional_hints:
                for hint in regional_hints[provider]:
                    if hint in combined_context:
                        score += 3
                        logger.debug(f"Found regional hint '{hint}' for {provider}")
            
            # Method 7: High-confidence query patterns (highest priority)
            for query in patterns.get('high_confidence_queries', []):
                if query in combined_context:
                    score += 25  # Very high score for specific queries
                    logger.debug(f"Found high-confidence query '{query}' for {provider}")
                    break  # Only need to match one high-confidence query
            
            # Method 8: Latest message priority - override historical context
            if latest_has_provider:
                # Check if this provider is mentioned in the latest message
                latest_score = 0
                
                # Check primary keywords in latest message only
                for keyword in patterns['primary_keywords']:
                    if keyword in latest_message:
                        latest_score += 30  # Very high weight for explicit mentions in latest message
                        logger.debug(f"Found primary keyword '{keyword}' in latest message for {provider}")
                
                # Check high-confidence queries in latest message
                for query in patterns.get('high_confidence_queries', []):
                    if query in latest_message:
                        latest_score += 35  # Highest weight for specific queries in latest message
                        logger.debug(f"Found high-confidence query '{query}' in latest message for {provider}")
                        break
                
                # Check deployment patterns in latest message
                for pattern in patterns['deployment_patterns']:
                    if pattern in latest_message:
                        latest_score += 28
                        logger.debug(f"Found deployment pattern '{pattern}' in latest message for {provider}")
                
                # If latest message has provider-specific content, use that score instead
                if latest_score > 0:
                    score = latest_score
                    logger.info(f"Using latest message priority for {provider} with score {score}")
            
            if score > 0:
                provider_scores[provider] = score

        # Find the provider with the highest score
        if provider_scores:
            best_provider = max(provider_scores, key=provider_scores.get)
            best_score = provider_scores[best_provider]
            
            # Only return if we have a confident match (score >= 5)
            if best_score >= 5:
                # Validate against the passed-in available_providers parameter
                if best_provider in available_providers:
                    logger.info(f"Detected target provider '{best_provider}' with confidence score {best_score}")
                    return best_provider
                else:
                    logger.warning(f"Detected target provider '{best_provider}' with confidence score {best_score} - but provider is not in available providers: {available_providers}")
                    return None
            else:
                logger.debug(f"Best provider '{best_provider}' has low confidence score {best_score}")

        # Fallback strategies when no clear provider is detected
        
        # Fallback 1: Check for infrastructure/deployment context with single provider
        if len(available_providers) == 1:
            has_deployment_context = any(action in combined_context for action in deployment_actions)
            if has_deployment_context:
                logger.info(f"Single provider '{available_providers[0]}' with deployment context detected")
                return available_providers[0]
        
        # Fallback 2: Look for stored preference when only one provider is stored
        try:
            from utils.cloud.cloud_utils import get_provider_preference
            stored_preference = get_provider_preference()
            if stored_preference and isinstance(stored_preference, list) and len(stored_preference) == 1:
                preferred = stored_preference[0]
                # Validate that the stored preference is in the available providers
                if preferred in available_providers:
                    logger.info(f"Using stored single provider preference: {preferred}")
                    return preferred
            elif stored_preference and isinstance(stored_preference, str):
                # Validate that the stored preference is in the available providers
                if stored_preference in available_providers:
                    logger.info(f"Using stored provider preference: {stored_preference}")
                    return stored_preference
        except Exception as e:
            logger.debug(f"Error getting stored provider preference: {e}")
            pass

        

        logger.debug(f"No provider detected from context. Available: {available_providers}")
        return None

    except Exception as e:
        logger.warning(f"Error determining target provider from context: {e}")
        return None 