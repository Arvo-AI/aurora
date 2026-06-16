"use client"

import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
  type ReactNode,
} from "react"

export interface OnboardingState {
  selections: {
    monitoring: string[]
    infrastructure: string[]
    alerting: string[]
    development: string[]
  }
}

interface OnboardingContextValue {
  state: OnboardingState
  step: number
  totalSteps: number
  goNext: () => void
  goBack: () => void
  addSelection: (page: keyof OnboardingState["selections"], id: string) => void
  removeSelection: (page: keyof OnboardingState["selections"], id: string) => void
  getSelectedConnectors: () => string[]
}

const STORAGE_KEY = "aurora_onboarding_state"
const TOTAL_STEPS = 7

const defaultState: OnboardingState = {
  selections: {
    monitoring: [],
    infrastructure: [],
    alerting: [],
    development: [],
  },
}

const OnboardingContext = createContext<OnboardingContextValue | null>(null)

export function OnboardingProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<OnboardingState>(defaultState)
  const [step, setStep] = useState(0)
  const [hydrated, setHydrated] = useState(false)

  useEffect(() => {
    try {
      const stored = sessionStorage.getItem(STORAGE_KEY)
      if (stored) {
        setState(JSON.parse(stored))
      }
    } catch {}
    setHydrated(true)
  }, [])

  useEffect(() => {
    if (hydrated) {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(state))
    }
  }, [state, hydrated])

  const goNext = useCallback(() => {
    setStep((s) => Math.min(s + 1, TOTAL_STEPS - 1))
  }, [])

  const goBack = useCallback(() => {
    setStep((s) => Math.max(s - 1, 0))
  }, [])

  const addSelection = useCallback(
    (page: keyof OnboardingState["selections"], id: string) => {
      setState((prev) => ({
        ...prev,
        selections: {
          ...prev.selections,
          [page]: prev.selections[page].includes(id)
            ? prev.selections[page]
            : [...prev.selections[page], id],
        },
      }))
    },
    []
  )

  const removeSelection = useCallback(
    (page: keyof OnboardingState["selections"], id: string) => {
      setState((prev) => ({
        ...prev,
        selections: {
          ...prev.selections,
          [page]: prev.selections[page].filter((s) => s !== id),
        },
      }))
    },
    []
  )

  const getSelectedConnectors = useCallback(() => {
    return [
      ...state.selections.monitoring,
      ...state.selections.infrastructure,
      ...state.selections.alerting,
      ...state.selections.development,
    ]
  }, [state.selections])

  if (!hydrated) return null

  return (
    <OnboardingContext.Provider
      value={{
        state,
        step,
        totalSteps: TOTAL_STEPS,
        goNext,
        goBack,
        addSelection,
        removeSelection,
        getSelectedConnectors,
      }}
    >
      {children}
    </OnboardingContext.Provider>
  )
}

export function useOnboarding() {
  const ctx = useContext(OnboardingContext)
  if (!ctx) throw new Error("useOnboarding must be used within OnboardingProvider")
  return ctx
}
