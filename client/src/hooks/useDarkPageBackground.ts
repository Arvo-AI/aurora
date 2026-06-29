import { useEffect } from "react"

export function useDarkPageBackground() {
  useEffect(() => {
    document.documentElement.style.backgroundColor = '#0a0a0a'
    document.body.style.backgroundColor = '#0a0a0a'
    document.documentElement.style.overscrollBehavior = 'none'
    return () => {
      document.documentElement.style.backgroundColor = ''
      document.body.style.backgroundColor = ''
      document.documentElement.style.overscrollBehavior = ''
    }
  }, [])
}
