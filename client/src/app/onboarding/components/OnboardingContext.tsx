"use client"

import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
  useMemo,
  type ReactNode,
} from "react"

export interface OnboardingState {
  selections: {
    monitoring: string[]
    infrastructure: string[]
    alerting: string[]
    communication: string[]
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
const TOTAL_STEPS = 8

const defaultState: OnboardingState = {
  selections: {
    monitoring: [],
    infrastructure: [],
    alerting: [],
    communication: [],
    development: [],
  },
}

const OnboardingContext = createContext<OnboardingContextValue | null>(null)

function normalizeState(input: unknown): OnboardingState {
  if (!input || typeof input !== "object") return defaultState
  const obj = input as Partial<OnboardingState>
  const s = (obj.selections ?? {}) as Partial<OnboardingState["selections"]>
  const asArray = (v: unknown) =>
    Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []
  return {
    selections: {
      monitoring: asArray(s.monitoring),
      infrastructure: asArray(s.infrastructure),
      alerting: asArray(s.alerting),
      communication: asArray(s.communication),
      development: asArray(s.development),
    },
  }
}

export function OnboardingProvider({ children }: Readonly<{ children: ReactNode }>) {
  const [state, setState] = useState<OnboardingState>(defaultState)
  const [step, setStep] = useState(0)
  const [hydrated, setHydrated] = useState(false)

  useEffect(() => {
    try {
      const stored = sessionStorage.getItem(STORAGE_KEY)
      if (stored) {
        setState(normalizeState(JSON.parse(stored)))
      }
    } catch {}
    setHydrated(true)
  }, [])

  useEffect(() => {
    if (hydrated) {
      try {
        sessionStorage.setItem(STORAGE_KEY, JSON.stringify(state))
      } catch {
        // Keep onboarding usable even when storage is unavailable
      }
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
      ...state.selections.communication,
      ...state.selections.development,
    ]
  }, [state.selections])

  const contextValue = useMemo(() => ({
    state,
    step,
    totalSteps: TOTAL_STEPS,
    goNext,
    goBack,
    addSelection,
    removeSelection,
    getSelectedConnectors,
  }), [state, step, goNext, goBack, addSelection, removeSelection, getSelectedConnectors])

  if (!hydrated) return null

  return (
    <OnboardingContext.Provider value={contextValue}>
      {children}
    </OnboardingContext.Provider>
  )
}

export function useOnboarding() {
  const ctx = useContext(OnboardingContext)
  if (!ctx) throw new Error("useOnboarding must be used within OnboardingProvider")
  return ctx
}
