import React, { useState, useEffect } from "react"
import { useProviderPreference } from "@/context/ProviderPreferenceContext"

interface ProviderSelectorProps {
  connectedProviders: string[];
  showGCP: boolean;
  showAWS: boolean;
  showAzure: boolean;
  setShowGCP: (show: boolean) => void;
  setShowAWS: (show: boolean) => void;
  setShowAzure: (show: boolean) => void;
}

export default function ProviderSelector({
  connectedProviders,
  showGCP,
  showAWS,
  showAzure,
  setShowGCP,
  setShowAWS,
  setShowAzure
}: ProviderSelectorProps) {
  const { addProviderPreference } = useProviderPreference()
  
  const handleToggle = (setter: (show: boolean) => void, current: boolean, id: string) => {
    const newValue = !current
    setter(newValue)
    if (newValue) {
      addProviderPreference(id as 'gcp' | 'aws' | 'azure')
    }
  }
  
  return (
    <div className="flex items-center gap-2">
      {connectedProviders.includes('gcp') && (
        <button
          onClick={() => handleToggle(setShowGCP, showGCP, 'gcp')}
          className={`px-3 py-1.5 text-xs font-medium rounded-full transition-colors ${
            showGCP 
              ? 'bg-[#81C995]/10 text-[#81C995] border border-[#81C995]/30' 
              : 'bg-gray-100 text-gray-500 border border-transparent'
          }`}
        >
          <div className="flex items-center gap-1.5">
            <div className={`w-2 h-2 rounded-full ${showGCP ? 'bg-[#81C995]' : 'bg-gray-400'}`} />
            GCP
          </div>
        </button>
      )}
      
      {connectedProviders.includes('aws') && (
        <button
          onClick={() => handleToggle(setShowAWS, showAWS, 'aws')}
          className={`px-3 py-1.5 text-xs font-medium rounded-full transition-colors ${
            showAWS 
              ? 'bg-[#FF9900]/10 text-[#FF9900] border border-[#FF9900]/30' 
              : 'bg-gray-100 text-gray-500 border border-transparent'
          }`}
        >
          <div className="flex items-center gap-1.5">
            <div className={`w-2 h-2 rounded-full ${showAWS ? 'bg-[#FF9900]' : 'bg-gray-400'}`} />
            AWS
          </div>
        </button>
      )}
      
      {connectedProviders.includes('azure') && (
        <button
          onClick={() => handleToggle(setShowAzure, showAzure, 'azure')}
          className={`px-3 py-1.5 text-xs font-medium rounded-full transition-colors ${
            showAzure 
              ? 'bg-[#0078D4]/10 text-[#0078D4] border border-[#0078D4]/30' 
              : 'bg-gray-100 text-gray-500 border border-transparent'
          }`}
        >
          <div className="flex items-center gap-1.5">
            <div className={`w-2 h-2 rounded-full ${showAzure ? 'bg-[#0078D4]' : 'bg-gray-400'}`} />
            Azure
          </div>
        </button>
      )}
      
    </div>
  )
} 
