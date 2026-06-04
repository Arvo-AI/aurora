"use client"

import { useEffect, useRef } from "react"

export function AuroraShader() {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return

    const ctx = canvas.getContext("2d")
    if (!ctx) return

    let animationId: number
    let time = 0

    const resize = () => {
      canvas.width = window.innerWidth
      canvas.height = window.innerHeight
    }

    const draw = () => {
      time += 0.003
      const { width, height } = canvas

      ctx.clearRect(0, 0, width, height)

      // Draw aurora-like gradient blobs
      const drawBlob = (x: number, y: number, r: number, hue: number, alpha: number) => {
        const gradient = ctx.createRadialGradient(x, y, 0, x, y, r)
        gradient.addColorStop(0, `hsla(${hue}, 80%, 50%, ${alpha})`)
        gradient.addColorStop(1, `hsla(${hue}, 80%, 50%, 0)`)
        ctx.fillStyle = gradient
        ctx.fillRect(0, 0, width, height)
      }

      drawBlob(
        width * 0.3 + Math.sin(time) * width * 0.1,
        height * 0.4 + Math.cos(time * 0.7) * height * 0.1,
        Math.min(width, height) * 0.5,
        200 + Math.sin(time * 0.5) * 20,
        0.12
      )

      drawBlob(
        width * 0.7 + Math.cos(time * 0.8) * width * 0.1,
        height * 0.6 + Math.sin(time * 0.6) * height * 0.1,
        Math.min(width, height) * 0.4,
        280 + Math.cos(time * 0.4) * 20,
        0.1
      )

      drawBlob(
        width * 0.5 + Math.sin(time * 1.1) * width * 0.15,
        height * 0.3 + Math.cos(time * 0.9) * height * 0.1,
        Math.min(width, height) * 0.35,
        160 + Math.sin(time * 0.3) * 15,
        0.08
      )

      animationId = requestAnimationFrame(draw)
    }

    resize()
    draw()
    window.addEventListener("resize", resize)

    return () => {
      window.removeEventListener("resize", resize)
      cancelAnimationFrame(animationId)
    }
  }, [])

  return (
    <canvas
      ref={canvasRef}
      className="absolute inset-0 w-full h-full blur-[80px]"
      aria-hidden="true"
    />
  )
}
