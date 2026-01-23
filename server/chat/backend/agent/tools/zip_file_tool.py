"""
Tool for accessing and analyzing zip files uploaded to the server.
This tool allows the agent to explore the contents of zip files and extract specific files.
"""

import os
import zipfile
import logging
import json
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

class ZipFileAnalyzer:
    """Handles analysis of zip files uploaded to the server."""
    
    @staticmethod
    def list_zip_contents(zip_path: str, user_id: Optional[str] = None) -> Dict[str, Any]:
        """
        List all files and directories in a zip file.
        
        Args:
            zip_path: Path to the zip file (can be storage URI or local path)
            user_id: Optional user ID for storage access
            
        Returns:
            Dictionary containing file listing and metadata
        """
        try:
            # Handle storage paths
            local_path = zip_path
            temp_file = None

            if zip_path.startswith('s3://'):
                # Download from storage to temporary file
                from utils.storage.storage import download_zip_from_storage
                local_path, original_filename = download_zip_from_storage(zip_path, user_id=user_id)
                temp_file = local_path
            
            # Analyze zip contents
            file_list = []
            total_size = 0
            file_count = 0
            
            with zipfile.ZipFile(local_path, 'r') as zip_ref:
                for info in zip_ref.infolist():
                    if not info.filename.startswith('__MACOSX/'):
                        file_list.append({
                            'path': info.filename,
                            'size': info.file_size,
                            'compressed_size': info.compress_size,
                            'is_directory': info.is_dir(),
                            'date_modified': info.date_time
                        })
                        if not info.is_dir():
                            total_size += info.file_size
                            file_count += 1
            
            # Organize files by directory
            tree_structure = {}
            for file_info in file_list:
                path_parts = file_info['path'].split('/')
                current = tree_structure
                
                for i, part in enumerate(path_parts[:-1]):
                    if part not in current:
                        current[part] = {}
                    current = current[part]
                
                if not file_info['is_directory']:
                    current[path_parts[-1]] = {
                        'type': 'file',
                        'size': file_info['size']
                    }
            
            # Clean up temp file if needed
            if temp_file and os.path.exists(temp_file):
                os.remove(temp_file)
            
            return {
                'success': True,
                'total_files': file_count,
                'total_size': total_size,
                'files': file_list,
                'tree_structure': tree_structure
            }
            
        except Exception as e:
            logger.error(f"Error analyzing zip file: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    @staticmethod
    def extract_file_from_zip(zip_path: str, file_path: str, user_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Extract and read a specific file from a zip archive.
        
        Args:
            zip_path: Path to the zip file
            file_path: Path of the file within the zip to extract
            user_id: Optional user ID for storage access
            
        Returns:
            Dictionary containing file content or error
        """
        try:
            # Handle storage paths
            local_path = zip_path
            temp_file = None

            if zip_path.startswith('s3://'):
                from utils.storage.storage import download_zip_from_storage
                local_path, _ = download_zip_from_storage(zip_path, user_id=user_id)
                temp_file = local_path

            content = None
            file_found = False
            
            with zipfile.ZipFile(local_path, 'r') as zip_ref:
                # Normalize the file path
                normalized_path = file_path.strip('/')
                
                for name in zip_ref.namelist():
                    if name.strip('/') == normalized_path:
                        file_found = True
                        file_bytes = zip_ref.read(name)
                        try:
                            content = file_bytes.decode('utf-8')
                        except UnicodeDecodeError:
                            try:
                                content = file_bytes.decode('latin-1')
                            except Exception:
                                # If it's binary, just show a summary
                                content = f"[Binary file: {name}, {len(file_bytes)} bytes]"
                        break
            
            # Clean up temp file if needed
            if temp_file and os.path.exists(temp_file):
                os.remove(temp_file)
            
            if not file_found:
                return {
                    'success': False,
                    'error': f'File "{file_path}" not found in zip archive'
                }
            
            return {
                'success': True,
                'content': content,
                'file_path': file_path
            }
            
        except Exception as e:
            logger.error(f"Error extracting file from zip: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    @staticmethod
    def analyze_project_structure(zip_path: str, user_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Analyze the project structure to determine project type, dependencies, etc.
        
        Args:
            zip_path: Path to the zip file
            user_id: Optional user ID for storage access
            
        Returns:
            Dictionary containing project analysis
        """
        try:
            # First get the file listing
            contents = ZipFileAnalyzer.list_zip_contents(zip_path, user_id=user_id)
            if not contents['success']:
                return contents
            
            # Analyze project type based on files
            project_info = {
                'type': 'unknown',
                'language': None,
                'framework': None,
                'has_dockerfile': False,
                'has_docker_compose': False,
                'dependencies': [],
                'entry_point': None,
                'configuration_files': []
            }
            
            # Check for various project indicators
            for file_info in contents['files']:
                filename = os.path.basename(file_info['path'])
                path = file_info['path']
                
                # Docker files
                if filename.lower() == 'dockerfile':
                    project_info['has_dockerfile'] = True
                elif filename.lower().startswith('docker-compose') and filename.endswith(('.yml', '.yaml')):
                    project_info['has_docker_compose'] = True
                    project_info['configuration_files'].append(path)
                
                # Python project
                elif filename == 'requirements.txt':
                    project_info['type'] = 'python'
                    project_info['language'] = 'python'
                    project_info['dependencies'].append(path)
                elif filename == 'setup.py' or filename == 'pyproject.toml':
                    project_info['type'] = 'python'
                    project_info['language'] = 'python'
                    project_info['configuration_files'].append(path)
                elif filename == 'app.py' or filename == 'main.py':
                    if not project_info['entry_point']:
                        project_info['entry_point'] = path
                
                # Node.js project
                elif filename == 'package.json':
                    project_info['type'] = 'nodejs'
                    project_info['language'] = 'javascript'
                    project_info['dependencies'].append(path)
                elif filename == 'index.js' or filename == 'app.js' or filename == 'server.js':
                    if not project_info['entry_point']:
                        project_info['entry_point'] = path
                
                # Go project
                elif filename == 'go.mod':
                    project_info['type'] = 'go'
                    project_info['language'] = 'go'
                    project_info['dependencies'].append(path)
                elif filename == 'main.go':
                    if not project_info['entry_point']:
                        project_info['entry_point'] = path
                
                # Java project
                elif filename == 'pom.xml':
                    project_info['type'] = 'maven'
                    project_info['language'] = 'java'
                    project_info['framework'] = 'maven'
                    project_info['dependencies'].append(path)
                elif filename == 'build.gradle' or filename == 'build.gradle.kts':
                    project_info['type'] = 'gradle'
                    project_info['language'] = 'java'
                    project_info['framework'] = 'gradle'
                    project_info['dependencies'].append(path)
                
                # Kubernetes files
                elif filename.endswith(('.yaml', '.yml')) and any(
                    keyword in path.lower() 
                    for keyword in ['k8s', 'kubernetes', 'deployment', 'service']
                ):
                    project_info['configuration_files'].append(path)
            
            # Try to detect framework for Node.js projects
            if project_info['type'] == 'nodejs':
                # Extract package.json to check for framework
                for dep_path in project_info['dependencies']:
                    if dep_path.endswith('package.json'):
                        pkg_result = ZipFileAnalyzer.extract_file_from_zip(zip_path, dep_path, user_id=user_id)
                        if pkg_result['success']:
                            try:
                                pkg_data = json.loads(pkg_result['content'])
                                deps = pkg_data.get('dependencies', {})
                                dev_deps = pkg_data.get('devDependencies', {})
                                all_deps = {**deps, **dev_deps}
                                
                                # Detect framework
                                if 'next' in all_deps:
                                    project_info['framework'] = 'next.js'
                                elif 'react' in all_deps:
                                    project_info['framework'] = 'react'
                                elif 'express' in all_deps:
                                    project_info['framework'] = 'express'
                                elif 'vue' in all_deps:
                                    project_info['framework'] = 'vue'
                                elif '@angular/core' in all_deps:
                                    project_info['framework'] = 'angular'
                            except:
                                pass
                        break
            
            return {
                'success': True,
                'project_info': project_info,
                'file_count': contents['total_files'],
                'total_size': contents['total_size']
            }
            
        except Exception as e:
            logger.error(f"Error analyzing project structure: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }


# Tool function for the agent
def analyze_zip_file(attachment_index: int = 0, operation: str = "list", file_path: Optional[str] = None, user_id: str = None, session_id: Optional[str] = None) -> str:
    """
    Analyze a zip file that was uploaded as an attachment.
    
    Args:
        attachment_index: Index of the attachment to analyze (default: 0 for first attachment)
        operation: Operation to perform - "list", "extract", or "analyze"
        file_path: For "extract" operation, the path of the file to extract from the zip
        user_id: (optional) The user ID, injected by the tool system to enable impersonation.
        session_id: Session ID for cancellation support (automatically injected by Aurora)
        
    Returns:
        String description of the analysis results
    """
    try:
        # Get state from context
        from chat.backend.agent.tools.cloud_tools import get_state_context
        state = get_state_context()
        
        # User ID is crucial for impersonation. Get it from state if not injected by the tool runner.
        if not user_id and hasattr(state, 'user_id'):
            user_id = state.user_id
        
        # Get session_id from state if not provided
        if not session_id and hasattr(state, 'session_id'):
            session_id = state.session_id
        
        logger.info(f"analyze_zip_file called with operation={operation}, attachment_index={attachment_index}")
        
        if not state:
            logger.error("analyze_zip_file: Unable to access conversation state")
            return "Unable to access conversation state. Please ensure the zip file was uploaded in this conversation."
        
        # Log state details for debugging
        logger.info(f"analyze_zip_file: State object found, user_id={getattr(state, 'user_id', 'None')}, session_id={getattr(state, 'session_id', 'None')}")
        
        # Check if there are attachments
        if not hasattr(state, 'attachments') or not state.attachments or len(state.attachments) == 0:
            logger.error("analyze_zip_file: No file attachments found in state")
            logger.error(f"analyze_zip_file: State has attachments attribute: {hasattr(state, 'attachments')}")
            if hasattr(state, 'attachments'):
                logger.error(f"analyze_zip_file: State.attachments value: {state.attachments}")
            return "No file attachments found in this conversation."
        
        logger.info(f"analyze_zip_file: Found {len(state.attachments)} attachments in state")
        
        # Get the specified attachment
        if attachment_index >= len(state.attachments):
            logger.error(f"analyze_zip_file: Attachment index {attachment_index} is out of range. There are {len(state.attachments)} attachments.")
            return f"Attachment index {attachment_index} is out of range. There are {len(state.attachments)} attachments."
        
        attachment = state.attachments[attachment_index]
        logger.info(f"analyze_zip_file: Using attachment {attachment_index}: {attachment.get('filename', 'unknown')}")
        
        # Check if it's a zip file with server path
        if not attachment.get('is_server_path') or not attachment.get('server_path'):
            logger.error(f"analyze_zip_file: Attachment is not a server-side zip file. is_server_path={attachment.get('is_server_path')}, server_path={attachment.get('server_path')}")
            return "The specified attachment is not a server-side zip file."
        
        server_path = attachment['server_path']
        filename = attachment.get('filename', 'unknown.zip')
        
        logger.info(f"Analyzing zip file: {filename} at {server_path}")
        
        # Perform the requested operation
        if operation == "list":
            result = ZipFileAnalyzer.list_zip_contents(server_path, user_id=user_id)
            if result['success']:
                # Format the output nicely
                output = f" Contents of {filename}:\n"
                output += f"Total files: {result['total_files']}\n"
                output += f"Total size: {result['total_size']:,} bytes\n\n"
                output += "File listing:\n"
                
                # Show first 20 files
                for i, file_info in enumerate(result['files'][:20]):
                    if not file_info['is_directory']:
                        output += f"  - {file_info['path']} ({file_info['size']:,} bytes)\n"
                
                if len(result['files']) > 20:
                    output += f"\n... and {len(result['files']) - 20} more files"
                
                return output
            else:
                return f"Error listing zip contents: {result['error']}"
        
        elif operation == "extract":
            if not file_path:
                return "Please specify a file_path to extract from the zip."
            
            result = ZipFileAnalyzer.extract_file_from_zip(server_path, file_path, user_id=user_id)
            if result['success']:
                content = result['content']
                
                # Check if this is binary content that could break database storage
                if content.startswith('[Binary file:'):
                    # For binary files, return a clean summary that won't break the database
                    return f" {content}\n\nThis is a binary file that cannot be displayed as text. You can download or extract it to view its contents."
                
                # For text files, truncate very long files to prevent database issues
                if len(content) > 10000:
                    content = content[:10000] + f"\n... (truncated, showing first 10000 characters of {len(content)} total)"
                
                return f" Contents of {file_path}:\n\n```\n{content}\n```"
            else:
                return f"Error extracting file: {result['error']}"
        
        elif operation == "analyze":
            result = ZipFileAnalyzer.analyze_project_structure(server_path, user_id=user_id)
            if result['success']:
                info = result['project_info']
                output = f" Project Analysis for {filename}:\n\n"
                output += f"Project Type: {info['type']}\n"
                output += f"Language: {info['language'] or 'Not detected'}\n"
                output += f"Framework: {info['framework'] or 'Not detected'}\n"
                output += f"Has Dockerfile: {'Yes' if info['has_dockerfile'] else 'No'}\n"
                output += f"Has Docker Compose: {'Yes' if info['has_docker_compose'] else 'No'}\n"
                
                if info['entry_point']:
                    output += f"Entry Point: {info['entry_point']}\n"
                
                if info['dependencies']:
                    output += f"\nDependency Files:\n"
                    for dep in info['dependencies']:
                        output += f"  - {dep}\n"
                
                if info['configuration_files']:
                    output += f"\nConfiguration Files:\n"
                    for config in info['configuration_files']:
                        output += f"  - {config}\n"
                
                output += f"\nTotal Files: {result['file_count']}"
                output += f"\nTotal Size: {result['total_size']:,} bytes"
                
                return output
            else:
                return f"Error analyzing project: {result['error']}"
        
        else:
            return f"Unknown operation: {operation}. Use 'list', 'extract', or 'analyze'."
            
    except Exception as e:
        logger.error(f"Error in analyze_zip_file: {str(e)}")
        return f"Error analyzing zip file: {str(e)}" 