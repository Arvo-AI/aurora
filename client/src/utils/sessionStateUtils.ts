export interface FileData {
  name: string;
  content: string;
  type: string;
  relativePath: string;
}

export interface EnvVar {
  key: string;
  value: string;
}

export interface CompleteUiState {
  // Chat and UI state
  input?: string;
  activeTab?: string;
  isSidePanelVisible?: boolean;
  isChatExpanded?: boolean;
  isCodeSectionExpanded?: boolean;
  
  // File and code state
  files?: FileData[];
  fileContents?: Record<string, string>;
  currentFilePath?: string;
  editorCode?: string;
  terraformCode?: string;
  editedFiles?: Record<string, string>;
  
  // Environment state
  envVars?: EnvVar[];
  envFilePath?: string;
  envFileContent?: string;
  envFileContentCleared?: boolean;
  
  // Provider and deployment type
  selectedProviders?: string[];
  deploymentType?: string | null;
  
  // Deployment progress state (now session-specific)
  deploymentSteps?: DeploymentStep[];
  showDeploymentProgress?: boolean;
  finalDeploymentMessage?: string;
  finalDeploymentUrl?: string;
  deploymentTaskId?: string | null;
  isDeploying?: boolean;
  isPolling?: boolean;
  deploymentTitle?: string | null;
}

// Default clean state for new chat sessions
export const getDefaultUiState = (): CompleteUiState => ({
  isSidePanelVisible: true,
  activeTab: "chat",
  isCodeSectionExpanded: true,
  isChatExpanded: false,
  files: [],
  fileContents: {},
  currentFilePath: '',
  editedFiles: {},
  editorCode: '',
  terraformCode: '',
  envVars: [],
  envFilePath: '',
  envFileContent: '',
  envFileContentCleared: false,
  selectedProviders: [],
  deploymentType: null,
  deploymentSteps: [],
  showDeploymentProgress: false,
  finalDeploymentMessage: '',
  finalDeploymentUrl: '',
  deploymentTaskId: null,
  input: '',
  isDeploying: false,
  isPolling: false,
  deploymentTitle: null,
});

// Clear all UI state - used when switching sessions
export const clearAllUiState = () => {
  // Return functions to clear each piece of state
  // This will be used by the Deploy component to reset all state
  return {
    clearMessages: () => [],
    clearInput: () => '',
    clearActiveTab: () => 'chat',
    clearSidePanelVisible: () => true,
    clearEditorCode: () => '',
    clearTerraformCode: () => '',
    clearCurrentFilePath: () => '',
    clearEnvVars: () => [],
    clearEnvFilePath: () => '',
    clearEnvFileContent: () => '',
    clearEnvFileContentCleared: () => false,
    clearSelectedProviders: () => [],
    clearEditedFiles: () => ({}),
    clearChatExpanded: () => false,
    clearCodeSectionExpanded: () => true,
    clearDeploymentType: () => null,
    clearFiles: () => [],
    clearFileContents: () => ({}),
    clearDeploymentSteps: () => [],
    clearShowDeploymentProgress: () => false,
    clearFinalDeploymentMessage: () => '',
    clearFinalDeploymentUrl: () => '',
    clearDeploymentTaskId: () => null,
    clearIsDeploying: () => false,
    clearIsPolling: () => false,
  };
};

// Apply UI state to component state setters
export const applyUiState = (
  uiState: CompleteUiState,
  setters: {
    setInput: (value: string) => void;
    setActiveTab: (value: string) => void;
    setIsSidePanelVisible: (value: boolean) => void;
    setEditorCode: (value: string) => void;
    setTerraformCode: (value: string) => void;
    setCurrentFilePath: (value: string) => void;
    setEnvVars: (value: EnvVar[]) => void;
    setEnvFilePath: (value: string) => void;
    setEnvFileContent: (value: string) => void;
    setEnvFileContentCleared: (value: boolean) => void;
    setSelectedProviders: (value: string[]) => void;
    setEditedFiles: (value: Record<string, string>) => void;
    setIsChatExpanded: (value: boolean) => void;
    setIsCodeSectionExpanded: (value: boolean) => void;
    setDeploymentType: (value: string | null) => void;
    setFiles: (value: FileData[]) => void;
    setFileContents: (value: Record<string, string>) => void;
    setDeploymentSteps: (value: DeploymentStep[]) => void;
    setShowDeploymentProgress: (value: boolean) => void;
    setFinalDeploymentMessage: (value: string) => void;
    setFinalDeploymentUrl: (value: string) => void;
    setDeploymentTaskId: (value: string | null) => void;
    setIsDeploying: (value: boolean) => void;
    setIsPolling: (value: boolean) => void;
  }
) => {
  // Apply each piece of state if it exists
  if (uiState.input !== undefined) setters.setInput(uiState.input);
  if (uiState.activeTab !== undefined) setters.setActiveTab(uiState.activeTab);
  if (uiState.isSidePanelVisible !== undefined) setters.setIsSidePanelVisible(uiState.isSidePanelVisible);
  if (uiState.editorCode !== undefined) setters.setEditorCode(uiState.editorCode);
  if (uiState.terraformCode !== undefined) setters.setTerraformCode(uiState.terraformCode);
  if (uiState.currentFilePath !== undefined) setters.setCurrentFilePath(uiState.currentFilePath);
  if (uiState.envVars !== undefined) setters.setEnvVars(uiState.envVars);
  if (uiState.envFilePath !== undefined) setters.setEnvFilePath(uiState.envFilePath);
  if (uiState.envFileContent !== undefined) setters.setEnvFileContent(uiState.envFileContent);
  if (uiState.envFileContentCleared !== undefined) setters.setEnvFileContentCleared(uiState.envFileContentCleared);
  if (uiState.selectedProviders !== undefined) setters.setSelectedProviders(uiState.selectedProviders);
  if (uiState.editedFiles !== undefined) setters.setEditedFiles(uiState.editedFiles);
  if (uiState.isChatExpanded !== undefined) setters.setIsChatExpanded(uiState.isChatExpanded);
  if (uiState.isCodeSectionExpanded !== undefined) setters.setIsCodeSectionExpanded(uiState.isCodeSectionExpanded);
  if (uiState.deploymentType !== undefined) setters.setDeploymentType(uiState.deploymentType);
  if (uiState.files !== undefined) setters.setFiles(uiState.files);
  if (uiState.fileContents !== undefined) setters.setFileContents(uiState.fileContents);
  if (uiState.deploymentSteps !== undefined) setters.setDeploymentSteps(uiState.deploymentSteps);
  if (uiState.showDeploymentProgress !== undefined) setters.setShowDeploymentProgress(uiState.showDeploymentProgress);
  if (uiState.finalDeploymentMessage !== undefined) setters.setFinalDeploymentMessage(uiState.finalDeploymentMessage);
  if (uiState.finalDeploymentUrl !== undefined) setters.setFinalDeploymentUrl(uiState.finalDeploymentUrl);
  if (uiState.deploymentTaskId !== undefined) setters.setDeploymentTaskId(uiState.deploymentTaskId);
  if (uiState.isDeploying !== undefined) setters.setIsDeploying(uiState.isDeploying);
  if (uiState.isPolling !== undefined) setters.setIsPolling(uiState.isPolling);
};

// Capture current UI state from component state
export const captureCurrentUiState = (currentState: {
  input: string;
  activeTab: string;
  isSidePanelVisible: boolean;
  editorCode: string;
  terraformCode: string;
  currentFilePath: string;
  envVars: EnvVar[];
  envFilePath: string;
  envFileContent: string;
  envFileContentCleared: boolean;
  selectedProviders: string[];
  editedFiles: Record<string, string>;
  isChatExpanded: boolean;
  isCodeSectionExpanded: boolean;
  deploymentType: string | null;
  files: FileData[];
  fileContents: Record<string, string>;
  deploymentSteps: DeploymentStep[];
  showDeploymentProgress: boolean;
  finalDeploymentMessage: string;
  finalDeploymentUrl: string;
  deploymentTaskId: string | null;
  isDeploying: boolean;
  isPolling: boolean;
}): CompleteUiState => ({
  input: currentState.input,
  activeTab: currentState.activeTab,
  isSidePanelVisible: currentState.isSidePanelVisible,
  editorCode: currentState.editorCode,
  terraformCode: currentState.terraformCode,
  currentFilePath: currentState.currentFilePath,
  envVars: currentState.envVars,
  envFilePath: currentState.envFilePath,
  envFileContent: currentState.envFileContent,
  envFileContentCleared: currentState.envFileContentCleared,
  selectedProviders: currentState.selectedProviders,
  editedFiles: currentState.editedFiles,
  isChatExpanded: currentState.isChatExpanded,
  isCodeSectionExpanded: currentState.isCodeSectionExpanded,
  deploymentType: currentState.deploymentType,
  files: currentState.files,
  fileContents: currentState.fileContents,
  deploymentSteps: currentState.deploymentSteps,
  showDeploymentProgress: currentState.showDeploymentProgress,
  finalDeploymentMessage: currentState.finalDeploymentMessage,
  finalDeploymentUrl: currentState.finalDeploymentUrl,
  deploymentTaskId: currentState.deploymentTaskId,
  isDeploying: currentState.isDeploying,
  isPolling: currentState.isPolling,
});

// Check if UI state is substantially different (for optimized saving)
export const hasSignificantStateChange = (
  oldState: CompleteUiState,
  newState: CompleteUiState,
  threshold: number = 5 // minimum changes to trigger save
): boolean => {
  const changes = [
    oldState.input !== newState.input,
    oldState.activeTab !== newState.activeTab,
    oldState.isSidePanelVisible !== newState.isSidePanelVisible,
    oldState.editorCode !== newState.editorCode,
    oldState.terraformCode !== newState.terraformCode,
    oldState.currentFilePath !== newState.currentFilePath,
    JSON.stringify(oldState.envVars) !== JSON.stringify(newState.envVars),
    oldState.envFilePath !== newState.envFilePath,
    oldState.envFileContent !== newState.envFileContent,
    oldState.envFileContentCleared !== newState.envFileContentCleared,
    JSON.stringify(oldState.selectedProviders) !== JSON.stringify(newState.selectedProviders),
    JSON.stringify(oldState.editedFiles) !== JSON.stringify(newState.editedFiles),
    oldState.isChatExpanded !== newState.isChatExpanded,
    oldState.isCodeSectionExpanded !== newState.isCodeSectionExpanded,
    oldState.deploymentType !== newState.deploymentType,
    JSON.stringify(oldState.files) !== JSON.stringify(newState.files),
    JSON.stringify(oldState.fileContents) !== JSON.stringify(newState.fileContents),
    JSON.stringify(oldState.deploymentSteps) !== JSON.stringify(newState.deploymentSteps),
    oldState.showDeploymentProgress !== newState.showDeploymentProgress,
    oldState.finalDeploymentMessage !== newState.finalDeploymentMessage,
    oldState.finalDeploymentUrl !== newState.finalDeploymentUrl,
    oldState.deploymentTaskId !== newState.deploymentTaskId,
    oldState.isDeploying !== newState.isDeploying,
    oldState.isPolling !== newState.isPolling,
  ];
  
  return changes.filter(Boolean).length >= threshold;
}; 